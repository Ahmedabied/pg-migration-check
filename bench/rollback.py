"""--rollback on a staging-sized table: how long the run takes, and proof the database is unchanged afterwards.

python bench/rollback.py [--rows 1000000] [--dsn ...]   (default: a local pgserver under $TMPDIR)
Builds `orders` with N rows and two indexes, takes a fingerprint (schema, file of every table and index, row count,
md5 of every row), runs `pg-migration-check examples/0002_unsafe.sql --rollback`, and compares the fingerprint.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.join(HERE, "..", "examples", "0002_unsafe.sql")


def fingerprint(c) -> tuple:
    q = lambda sql: tuple(c.execute(sql).fetchall())
    return (q("select table_name, column_name, data_type, is_nullable from information_schema.columns "
              "where table_schema = 'public' order by 1, 2"),
            q("select indexdef from pg_indexes where schemaname = 'public' order by 1"),
            q("select relname, relfilenode from pg_class where relnamespace = 'public'::regnamespace order by 1"),
            q("select count(*), md5(string_agg(o::text, ',' order by id)) from orders o"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=1_000_000)
    p.add_argument("--dsn")
    a = p.parse_args()
    if a.dsn:
        uri = a.dsn
    else:
        import pgserver

        uri = pgserver.get_server(os.path.join(tempfile.gettempdir(), "pgmc-bench"), cleanup_mode="stop").get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("drop database if exists pgmc_rollback with (force)")
        c.execute("create database pgmc_rollback")
    dsn = psycopg.conninfo.make_conninfo(uri, dbname="pgmc_rollback")
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            version = c.execute("show server_version").fetchone()[0]
            c.execute("create table orders (id bigserial primary key, email text, qty int, created_at timestamptz default now())")
            c.execute("insert into orders (email, qty) select 'u' || g || '@example.com', g %% 7 "
                      "from generate_series(1, %s) g", (a.rows,))
            c.execute("create index orders_created_idx on orders (created_at)")
            c.execute("analyze orders")
            size = c.execute("select pg_size_pretty(pg_total_relation_size('orders'))").fetchone()[0]
            before = fingerprint(c)
        t = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", "pg_migration_check", MIGRATION, "--rollback", "--json", "--dsn", dsn],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.path.join(HERE, "..", "src")})
        wall = time.perf_counter() - t
        with psycopg.connect(dsn, autocommit=True) as c:
            same = fingerprint(c) == before
        stmts = [s for s in json.loads(r.stdout)[0]["statements"] if s["n"]]
        print(f"Postgres {version}, orders: {a.rows:,} rows, {size} with indexes")
        for s in stmts:
            locks = ", ".join(s["locks"].values()) or "(already held)"
            print(f"  {s['n']}. {' '.join(s['sql'].split())[:60]:<60} {s['ms']:>8,.0f} ms  {locks}"
                  f"{'  rewrite' if s['rewritten'] else ''}")
        print(f"whole run {wall:.2f} s, exit {r.returncode}; database unchanged afterwards: {'yes' if same else 'NO'}")
        if not same:
            sys.exit(1)
    finally:
        with psycopg.connect(uri, autocommit=True) as c:
            c.execute("drop database pgmc_rollback with (force)")


if __name__ == "__main__":
    main()
