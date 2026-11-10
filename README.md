# pg-migration-check

A migration that looks harmless can lock a busy table for seconds: an index built without `CONCURRENTLY`, a type
change that copies the table, a `NOT NULL` that scans every row while reads wait. pg-migration-check runs the
migration against a scratch Postgres in CI and reports, statement by statement, what it locked, what that blocks,
whether the table was rewritten, and for how long.

```
examples/0002_unsafe.sql
   1. ALTER TABLE "orders" ALTER COLUMN "qty" TYPE bigint  (379 ms)
      DANGER  rewrites public.orders: the whole table is copied while it is locked
      DANGER  ACCESS EXCLUSIVE on public.orders (200,000 rows) for 379 ms: blocks everything, reads included
   2. CREATE INDEX "orders_email_idx" ON "orders" ("email")  (196 ms)
      warn    SHARE on public.orders (200,000 rows) for 196 ms: blocks writes (INSERT, UPDATE, DELETE); reads go on
      warn    CREATE INDEX without CONCURRENTLY blocks writes for the whole build. Use CREATE INDEX CONCURRENTLY ...
   3. ALTER TABLE "orders" ALTER COLUMN "email" SET NOT NULL  (6 ms)
      warn    SET NOT NULL scans the whole table under ACCESS EXCLUSIVE unless a valid CHECK (col IS NOT NULL) ...
   4. ALTER TABLE "orders" RENAME COLUMN "created_at" TO "placed_at"  (0 ms)
      DANGER  rename: code still running with the old name breaks the moment this commits. ...
  warn    no lock_timeout: if a long query holds the table, this migration waits for it, and every query that ...
  If the whole file runs in one transaction (Django does this unless atomic = False), the locks are held until it commits:
      public.orders: reads and writes wait 581 ms
```

The exit code is 1 when there is a DANGER, so the pull request fails. The [safe version](examples/0003_safe.sql) of
the same change passes clean.

## How it knows

It does not guess from the SQL text. Each statement runs in its own transaction on the scratch database (in a
savepoint of one transaction with `--rollback`), and before it commits the tool asks Postgres:

- **which locks it holds**, from `pg_locks` for its own backend, mapped to what each mode blocks;
- **whether a table was rewritten**, because its `relfilenode` changed;
- **how long it took**, which is how long those locks were held, on the rows you put in the scratch database.

A few risks the locks cannot show are read from the SQL: renames and dropped columns (they break code that is still
running), `SET NOT NULL` without a validated `CHECK` (on Postgres 18 it suggests a `NOT NULL` constraint added
`NOT VALID` instead), constraints added without `NOT VALID` (`NOT NULL` ones too), and no `lock_timeout`.
Tables created earlier in the same file are skipped: nothing live uses them yet (with `--rollback`, only until
the file has locked a live table; after that the SQL rules still speak about the new tables too).

## Measured: lock time on a 1M row table

`python bench/bench.py`, Postgres 16.2 on an Intel i5-14400F, a fresh copy of the table for every case:

| change | unsafe: locks | unsafe: blocked | safe: locks | safe: blocked |
|---|---|---|---|---|
| add an index | SHARE | writes 1,211 ms | SHARE UPDATE EXCLUSIVE (`CONCURRENTLY`) | nothing (whole step 1,383 ms) |
| make a column NOT NULL | ACCESS EXCLUSIVE | reads and writes 39 ms | `CHECK ... NOT VALID`, `VALIDATE`, then `SET NOT NULL` | 0.1 ms (whole step 41 ms) |
| add a foreign key | SHARE ROW EXCLUSIVE | writes 92 ms | `NOT VALID`, then `VALIDATE` | 0.2 ms (whole step 93 ms) |
| add a column with a default | ACCESS EXCLUSIVE + rewrite (`random()`) | reads and writes 516 ms | a constant default: no rewrite | 0.2 ms |
| change int to bigint | ACCESS EXCLUSIVE + rewrite | reads and writes 495 ms | no one-step safe version: add a column, backfill in batches, swap | |

The safe versions do the same work, often a little more of it, while live traffic keeps going. Lock time grows
with the table: a 50M row table is roughly 50 times these numbers.

Postgres 18 has a shorter way to make a column NOT NULL: `ADD CONSTRAINT name NOT NULL col NOT VALID`, then
`VALIDATE CONSTRAINT`. On 18.6, same machine and table, that blocked for 0.1 ms (whole step 38 ms) against 41 ms
for `SET NOT NULL`, and leaves no `CHECK` behind to drop. pg-migration-check suggests it when the server is 18 or
newer, and flags a `NOT NULL` constraint added without `NOT VALID`, which reads every row under ACCESS EXCLUSIVE.

## Run it in under a minute

```sh
git clone https://github.com/Ahmedabied/pg-migration-check && cd pg-migration-check
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[local]'   # local adds a throwaway Postgres
python bench/bench.py --rows 100000
```

That took 4 seconds from a fresh clone (pip cache warm). The throwaway Postgres comes from
[pgserver](https://github.com/orm011/pgserver), which has wheels for Python 3.9 to 3.12; the checker itself runs on
3.10 and newer, and needs no local Postgres when you give it `--dsn`. No Python 3.12 on the machine? uv fetches one:
`uv venv --python 3.12 .venv && . .venv/bin/activate && uv pip install -e '.[local]'` (4.7 s from a fresh clone).

Against your own scratch database:

```sh
pg-migration-check migrations/0042.sql --dsn postgresql://postgres:ci@localhost:5432/scratch
python manage.py sqlmigrate shop 0042 > /tmp/0042.sql && pg-migration-check /tmp/0042.sql   # Django
```

**The database gets changed**: the migration is applied (unless `--rollback`, below). Point it at a CI service
container or a copy, never at production. Options: `--fail-on danger|warn|never`, `--slow-ms` (ACCESS EXCLUSIVE held this long is a danger,
default 1000), `--json`, `-v` to show brief locks too, `--rows N` and `--rollback` (below).

**`--rows N` gives an empty scratch database production's weight.** Before the migration runs, every empty table
it names, and every table those reference, gets N synthetic rows from one `INSERT ... SELECT` over
`generate_series`: a value per column type (numbers, text inside `varchar(n)`, `numeric(p, s)` inside its
precision, uuid, dates, json, enums, arrays), foreign keys pointing at real parent rows, sequences moved past them.
`--rows orders=5000000` sets one table. Tables that already hold rows are left alone; a table it cannot fill (a
CHECK its values break, a unique boolean) is reported and skipped, never half filled.
`--rows-from DSN` takes each table's count from another database's statistics instead (`pg_class.reltuples`,
what the planner already keeps), in a read-only session that scans no table: point it at production and the scratch
tables get production's row counts. Tables never analyzed there have no estimate and are left empty.
Give it a login role with no privileges at all (`CREATE ROLE stats LOGIN PASSWORD '...'`): every role can read
`pg_class`, so it sees the counts but cannot read a single row of your tables (tested). The session is read only
either way, but the credential sits in your CI, so make it one that cannot do harm.

**`--rollback` checks a staging copy and leaves it as it was.** Empty tables and synthetic rows only go so far;
a copy of production has the real row widths, indexes and bloat. With `--rollback` the whole run is one
transaction, each statement in a savepoint inside it, and the transaction is rolled back at the end, so the next
pull request can check its migration against the same copy. On a 1M row `orders` table (93 MB with indexes,
Postgres 16.2) the run of `examples/0002_unsafe.sql` took 1.8 s, with the type change rewriting the table in
594 to 782 ms and the index build at 924 to 998 ms, and afterwards the schema, the file behind every table and
index, and an md5 of every row were identical to before (`python bench/rollback.py`, two runs).

What changes in the report: each statement shows the locks it adds. A lock an earlier statement took is still
held, which is how one transaction works and how Django runs a migration, so a later statement on the same table
can show none (Postgres does not even record a weaker lock under an ACCESS EXCLUSIVE it already holds); the
summary line gives how long each table stayed locked. `CREATE INDEX CONCURRENTLY` (and the other forms that refuse a
transaction) runs in its plain form so it can be timed and later statements that use the index still work; the
report says so and shows the locks the real statement takes.

Never point it at production: every lock is held until the run ends. A rewrite writes a full copy of the table
(disk and WAL) before the rollback throws it away; sequences moved by `nextval` stay moved, as Postgres never rolls
them back; anything that reaches outside the transaction (dblink, `COPY ... TO PROGRAM`) is not undone either; two
runs on the same copy at once wait for each other. A file that ends or splits its own transaction (`ROLLBACK`,
`SAVEPOINT`, `PREPARE TRANSACTION`) is refused before anything runs. Each statement goes to the server on its own
over the extended protocol, which refuses two commands in one string, so a statement the splitter got wrong cannot
carry a `ROLLBACK` past that check. If the transaction ever ends early anyway, the run stops with `NOT ROLLED BACK`
and exit code 3. `--rows` is not allowed with it.

## In a Django project

```sh
pip install 'pg-migration-check[django] @ git+https://github.com/Ahmedabied/pg-migration-check'
```

Add `"pg_migration_check.djangoapp"` to `INSTALLED_APPS`, then:

```sh
python manage.py check_migrations --dsn postgresql://localhost/scratch          # what is not applied yet
python manage.py check_migrations shop --all --dsn postgresql://localhost/scratch  # every migration of one app
```

It builds the scratch database from nothing by applying every migration in order, so each one meets the schema it
will meet in production, with Django's own SQL (what `sqlmigrate` prints). The ones your `default` database has
already applied are applied silently; the rest are reported statement by statement, with the lock time a Django
migration really has: one transaction, so every lock is held until the last statement commits (unless the migration
says `atomic = False`). It exits 1 on a danger, like the command line tool. The scratch database must be empty.
CI tests it with Django 6.1 on Postgres 16, 17 and 18, and Django 5.2 on Postgres 14 (Django 6 needs Postgres 15)
and on Python 3.10 (Django 6 needs 3.12).

Run on every migration of Django's own contrib apps (admin, auth, contenttypes, sessions, sites, flatpages,
redirects; Django 6.1.3): 23 migrations, 63 statements, checked in 0.14 s. No dangers on these empty tables, and 20
warnings worth knowing before you copy their patterns onto a big table: 13 migrations with no `lock_timeout`, 5 foreign
keys or checks added without `NOT VALID` (checked against every row while writes wait), 1 column drop, and 1 index
built without `CONCURRENTLY` (`sites.0002`).

The same command on this repository's test app with `--rows 1000000` (Postgres 16.2): `shop_order` filled in 1.7 s,
then `shop.0002` (an int to bigint `AlterField` and an `AddIndex`) shows what an empty scratch database hid:

| | empty tables (two runs) | `--rows 1000000` (one run) |
|---|---|---|
| rewrite, ACCESS EXCLUSIVE (reads and writes wait) | 4 to 17 ms | 613 ms |
| index build, SHARE (writes wait) | 1 to 20 ms | 1,369 ms |
| held until Django's one transaction commits | 18 to 24 ms | 1,982 ms |

Run with `python manage.py check_migrations --rows 1000000 --dsn ...`. 3.8 s from start to report.

**`--rollback` on a copy of production.** `python manage.py check_migrations --rollback --dsn
postgresql://.../staging_copy`: the copy's own `django_migrations` table says what is pending (not the `default`
database), the pending migrations run on its real rows in one transaction, and it is rolled back. The test app on a
copy with `shop.0001` applied and 1M rows in `shop_order` (Postgres 16.2): `shop.0002` rewrote the table under
ACCESS EXCLUSIVE for 453 to 460 ms and kept it locked 1,657 to 1,673 ms until the migration commits, 1.7 s for the
whole command, and the copy's files, rows and `django_migrations` were unchanged afterwards (two runs). The rules
of the command line `--rollback` above apply, and one more: the pending migrations share that one transaction, so a
later migration on a table an earlier one locked shows only the locks it adds (rewrites and the SQL rules are still
reported).

## In GitHub Actions

```yaml
jobs:
  migrations:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: ci }
        ports: ["5432:5432"]
    env:
      DSN: postgresql://postgres:ci@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - run: psql "$DSN" -f schema.sql      # your schema and some rows: lock time depends on them
      - uses: Ahmedabied/pg-migration-check@v0.2.0
        with:
          files: migrations/new/*.sql
          dsn: ${{ env.DSN }}
```

The pull request fails on a danger, and the report lands in the job summary. Against a staging copy instead, give
its address as a secret and set `rollback: "true"`. Other inputs: `rows`, `fail-on`, `slow-ms`. When no file
matches, the step passes and says so. The action installs the version of the checker it is pinned to, in its own
virtualenv, and passes the inputs through environment variables, so a file name cannot run a command.

This repository's own CI runs the tests and both examples on Postgres 14, 16, 17 and 18, and runs the action itself
on both examples with `rollback`.

## Limits

- **Lock time is measured on your scratch data and hardware.** `--rows` gives the tables a production-like row count,
  but the rows are uniform and narrow, indexes are fresh and nothing else runs: read the times as a floor.
- **It cannot see lock waits.** On a busy production table a migration first waits for running queries; that wait is
  the classic outage, and the reason for `lock_timeout`, which it checks for.
- **One statement per transaction.** Django wraps a migration in one transaction; the summary line adds up how long
  each table stays locked in that case. `--rollback` runs the whole run in one transaction: with several files,
  a lock the first file took is still held in the second, which then shows only the locks it adds.
- **`--rows` picks tables by name.** A table is filled when its name appears as a word anywhere in the migration's
  SQL, so a table called like a common column (`name`, `email`) can be filled without being touched. Harmless, but
  at a million rows it costs seconds; `--rows that_table=0` turns it off.
- **SQL files and Django.** Django has its own command (above); other frameworks need their SQL output. In Django,
  data migrations (`RunPython`) are not run on the scratch database, so a later migration that needs their data fails
  there.

## Development

```sh
pip install -e '.[test,local]'
pytest -q            # 56 tests on a real Postgres (pgserver locally, $PGMC_TEST_DSN in CI), about 11 s
```

Built with AI pair programming.

## Licence

MIT
