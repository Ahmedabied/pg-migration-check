"""Lock time on a 1M row table for common migrations, unsafe versus safe.

python bench/bench.py [--rows 1000000] [--dsn ...]   (default: a local pgserver)
Each case gets a fresh copy of the table. For each statement: the strongest lock on the table and how long it was
held. "blocks writes" is the longest time writers waited; "blocks reads" the longest time anyone waited.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pg_migration_check.check import LOCKS, check  # noqa: E402

CASES = [
    ("add an index",
     "create index on t (email)",
     "create index concurrently on t (email)"),
    ("make a column NOT NULL",
     "alter table t alter column email set not null",
     "alter table t add constraint t_email_nn check (email is not null) not valid;"
     "alter table t validate constraint t_email_nn; alter table t alter column email set not null"),
    ("add a foreign key",
     "alter table t add foreign key (customer_id) references customers (id)",
     "alter table t add constraint t_cust_fk foreign key (customer_id) references customers (id) not valid;"
     "alter table t validate constraint t_cust_fk"),
    ("add a column with a default",
     "alter table t add column score float8 default random()",
     "alter table t add column score float8 not null default 0"),
    ("change int to bigint",
     "alter table t alter column qty type bigint",
     None),
]


# Postgres 18 can add a NOT NULL constraint NOT VALID and validate it later, so no CHECK is left to drop.
PG18_CASES = [
    ("make a column NOT NULL (Postgres 18)",
     "alter table t alter column email set not null",
     "alter table t add constraint t_email_nn not null email not valid; alter table t validate constraint t_email_nn"),
]


def ms(v: float) -> str:
    return f"{v:,.1f} ms" if v < 10 else f"{v:,.0f} ms"


def blocked(results) -> tuple[float, float, str]:
    writes = reads = 0.0
    modes = []
    for r in results:
        mode = r.locks.get("public.t") or r.locks.get("t")
        if not mode:
            continue
        rank = LOCKS[mode][0]
        modes.append(LOCKS[mode][1])
        if rank >= 5:
            writes = max(writes, r.ms)
        if rank == 8:
            reads = max(reads, r.ms)
    rewrite = any(r.rewritten for r in results)
    return writes, reads, ", ".join(dict.fromkeys(modes)) + (" + rewrite" if rewrite else "")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=1_000_000)
    p.add_argument("--dsn")
    a = p.parse_args()
    if a.dsn:
        uri = a.dsn
    else:
        import pgserver

        uri = pgserver.get_server("/tmp/pgmc-bench", cleanup_mode="stop").get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("drop database if exists pgmc_bench with (force)")
        c.execute("create database pgmc_bench")
    dsn = psycopg.conninfo.make_conninfo(uri, dbname="pgmc_bench")
    with psycopg.connect(dsn, autocommit=True) as c:
        version = c.execute("show server_version").fetchone()[0]
        c.execute("create table customers (id bigint primary key)")
        c.execute("insert into customers select g from generate_series(1, 10000) g")
        c.execute("create table base (id bigint primary key, email text, qty int, customer_id bigint, created_at timestamptz)")
        c.execute("insert into base select g, 'user' || g || '@example.com', g %% 100, 1 + g %% 10000, now() "
                  "from generate_series(1, %s) g", (a.rows,))
        print(f"Postgres {version}, {a.rows:,} rows", file=sys.stderr)
        rows = []
        cases = CASES + (PG18_CASES if c.info.server_version >= 180000 else [])
        for name, unsafe, safe in cases:
            out = [name]
            for sql in (unsafe, safe):
                if sql is None:
                    out += ["no one-step safe version: add a column, backfill in batches, swap", ""]
                    continue
                c.execute("drop table if exists t")
                c.execute("create table t as select * from base")
                c.execute("alter table t add primary key (id)")
                c.execute("analyze t")
                res = [r for r in check(c, "set lock_timeout = '5s';" + sql) if r.n > 1]
                w, rd, modes = blocked(res)
                total = sum(r.ms for r in res)
                extra = f" (whole step {ms(total)})" if total - w > 5 else ""
                out += [modes, f"writes {ms(w)}, reads {ms(rd)}{extra}"]
            rows.append(out)
    print("| change | unsafe: locks | unsafe: blocked | safe: locks | safe: blocked |")
    print("|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("drop database pgmc_bench with (force)")


if __name__ == "__main__":
    main()
