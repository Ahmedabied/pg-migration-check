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

It does not guess from the SQL text. Each statement runs in its own transaction on the scratch database, and before
it commits the tool asks Postgres:

- **which locks it holds**, from `pg_locks` for its own backend, mapped to what each mode blocks;
- **whether a table was rewritten**, because its `relfilenode` changed;
- **how long it took**, which is how long those locks were held, on the rows you put in the scratch database.

A few risks the locks cannot show are read from the SQL: renames and dropped columns (they break code that is still
running), `SET NOT NULL` without a validated `CHECK`, constraints added without `NOT VALID`, and no `lock_timeout`.
Tables created earlier in the same file are skipped: nothing live uses them yet.

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

## Run it in under a minute

```sh
git clone https://github.com/Ahmedabied/pg-migration-check && cd pg-migration-check
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[local]'   # local adds a throwaway Postgres
python bench/bench.py --rows 100000
```

Against your own scratch database:

```sh
pg-migration-check migrations/0042.sql --dsn postgresql://postgres:ci@localhost:5432/scratch
python manage.py sqlmigrate shop 0042 > /tmp/0042.sql && pg-migration-check /tmp/0042.sql   # Django
```

**The database gets changed**: the migration is applied. Point it at a CI service container or a copy, never at
production. Options: `--fail-on danger|warn|never`, `--slow-ms` (ACCESS EXCLUSIVE held this long is a danger,
default 1000), `--json`, `-v` to show brief locks too.

## In GitHub Actions

```yaml
services:
  postgres:
    image: postgres:16
    env: { POSTGRES_PASSWORD: ci }
    ports: ["5432:5432"]
steps:
  - run: pip install git+https://github.com/Ahmedabied/pg-migration-check
  - run: psql "$DSN" -f schema.sql      # your schema and some rows: lock time depends on them
  - run: pg-migration-check migrations/new/*.sql --dsn "$DSN"
```

This repository's own CI runs the tests and both examples on Postgres 14, 16 and 17.

## Limits

- **Lock time is measured on your scratch data.** Fill the scratch tables with a realistic number of rows, or read
  the times as "per this many rows" and scale.
- **It cannot see lock waits.** On a busy production table a migration first waits for running queries; that wait is
  the classic outage, and the reason for `lock_timeout`, which it checks for.
- **One statement per transaction.** Django wraps a migration in one transaction; the summary line adds up how long
  each table stays locked in that case.
- **SQL files only.** Django works through `sqlmigrate`. Other frameworks need their SQL output.

## Development

```sh
pip install -e '.[test,local]'
pytest -q            # 14 tests on a real Postgres (pgserver locally, $PGMC_TEST_DSN in CI), about 2 s
```

Built with AI pair programming.

## Licence

MIT
