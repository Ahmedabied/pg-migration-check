"""--rows: synthetic rows in empty tables, so a scratch database shows production-like lock times."""

import io
import subprocess
import sys

import psycopg
import pytest

from pg_migration_check.check import check
from pg_migration_check.seed import parse_rows, report, seed


@pytest.fixture
def empty(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


SCHEMA = """
create type kind as enum ('retail', 'trade');
create domain short_code as varchar(12);
create table customer (id bigserial primary key, name text not null, joined timestamptz not null default now(),
                       email varchar(254) unique, kind kind not null, active boolean not null, born date);
create table purchase (id int generated always as identity primary key,
                       customer_id bigint not null references customer (id) deferrable initially deferred,
                       ref uuid not null unique, amount numeric(8, 2) not null, code short_code not null,
                       extra jsonb, tags text[] not null default '{}', reply_to int references purchase (id),
                       total numeric generated always as (amount * 2) stored);
"""


def test_fills_every_type_and_keeps_foreign_keys_and_sequences_right(empty):
    empty.execute(SCHEMA)
    done = seed(empty, 5000)
    assert [(s.table, s.rows, s.skipped) for s in done] == [("public.customer", 5000, ""), ("public.purchase", 5000, "")]
    # Every purchase points at a real customer (the constraint held), and the keys spread over all of them.
    assert empty.execute("select count(distinct customer_id) from purchase").fetchone()[0] == 5000
    assert empty.execute("select count(*) from purchase p join customer c on c.id = p.customer_id").fetchone()[0] == 5000
    # The sequences moved past the rows we made, so the next normal insert does not collide.
    empty.execute("insert into customer (name, kind, active) values ('new', 'trade', true)")
    empty.execute("insert into purchase (customer_id, ref, amount, code) values (1, gen_random_uuid(), 1, 'x')")
    assert empty.execute("select max(amount) <= 999999.99, count(reply_to) from purchase").fetchone() == (True, 0)
    # Statistics are fresh, which is where the report reads its row counts.
    assert empty.execute("select reltuples::int from pg_class where relname = 'customer'").fetchone()[0] == 5000


def test_skips_what_it_cannot_fill_and_says_why(empty):
    empty.execute("create table kept (id int); insert into kept values (1)")
    empty.execute("create table strict (n int not null check (n < 0))")
    empty.execute("create table flag (on_off boolean unique)")
    empty.execute("create table geo (p point not null)")
    done = {s.table: s for s in seed(empty, 100, {"public.flag": 3})}
    assert done["public.kept"].skipped == "already has rows"
    assert done["public.strict"].skipped.startswith("CheckViolation")
    assert done["public.flag"].skipped.startswith("UniqueViolation")
    assert done["public.geo"].skipped == "no generator for p (point) and it has no default"
    assert empty.execute("select (select count(*) from kept), (select count(*) from strict)").fetchone() == (1, 0)
    out = io.StringIO()
    report(list(done.values()), out)
    assert "not filled public.geo: no generator" in out.getvalue()


def test_a_child_of_an_unfilled_table_gets_nulls_or_is_skipped(empty):
    empty.execute("create table parent (id int primary key); insert into parent values (1)")
    empty.execute("create table soft (id int, parent_id int references parent)")
    empty.execute("create table hard (id int, parent_id int not null references parent)")
    done = {s.table: s for s in seed(empty, 50)}
    assert done["public.soft"].rows == 50
    assert empty.execute("select count(parent_id) from soft").fetchone()[0] == 0
    assert done["public.hard"].skipped == "parent_id must reference a row in a table that was not filled"


def test_rows_turn_an_empty_table_rewrite_into_a_measured_one(empty):
    empty.execute("create table orders (id bigserial primary key, qty int not null, email text)")
    seed(empty, 200_000)
    r = check(empty, "alter table orders alter column qty type bigint;")[0]
    assert r.rows["public.orders"] == 200_000 and r.rewritten == ["public.orders"]
    assert any("200,000 rows" in f.text for f in r.findings)


def test_parse_rows():
    assert parse_rows(["1_000_000", "orders=5,000"]) == (1_000_000, {"orders": 5000})
    assert parse_rows(None) == (None, {})
    with pytest.raises(ValueError):
        parse_rows(["t=-1"])


def test_cli_rows(dsn, tmp_path):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table t (id serial primary key, v text)")
    f = tmp_path / "m.sql"
    f.write_text("create index on t (v);\n")
    r = subprocess.run([sys.executable, "-m", "pg_migration_check", "--dsn", dsn, "--rows", "20000", "-v", str(f)],
                       capture_output=True, text=True)
    assert "filled public.t with 20,000 rows" in r.stdout, r.stdout + r.stderr
    assert "(20,000 rows)" in r.stdout


def test_only_the_tables_the_migration_names_and_what_they_reference(empty):
    empty.execute("create table account (id int primary key)")
    empty.execute("create table invoice (id int primary key, account_id int not null references account)")
    empty.execute("create table unrelated (id int)")
    done = {s.table: s.rows for s in seed(empty, 100, {"public.unrelated": 7}, 'ALTER TABLE "invoice" ADD COLUMN x int;')}
    assert done == {"public.account": 100, "public.invoice": 100, "public.unrelated": 7}
    assert seed(empty, 100, None, "select 1") == []  # names nothing: fills nothing


def test_row_counts_from_another_databases_statistics(server_uri, dsn):
    import uuid

    prod = "p_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {prod}")
    prod_dsn = psycopg.conninfo.make_conninfo(server_uri, dbname=prod)
    try:
        with psycopg.connect(prod_dsn, autocommit=True) as p:
            p.execute("create table account (id int primary key); insert into account select generate_series(1, 300)")
            p.execute("create table invoice (id int, account_id int references account)")
            p.execute("insert into invoice select g, 1 + g % 300 from generate_series(1, 4000) g")
            p.execute("create table never_analyzed (id int)")
            p.execute("analyze account; analyze invoice")
        from pg_migration_check.seed import counts_from

        sizes = counts_from(prod_dsn)
        assert sizes == {"public.account": 300, "public.invoice": 4000}  # never analyzed: no estimate, left out
        with psycopg.connect(dsn, autocommit=True) as s:
            s.execute("create table account (id int primary key)")
            s.execute("create table invoice (id int, account_id int references account)")
            done = {x.table: x.rows for x in seed(s, None, None, "alter table invoice add column x int", sizes)}
        assert done == {"public.account": 300, "public.invoice": 4000}
    finally:
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"drop database {prod} with (force)")


def test_cli_rows_from(dsn, tmp_path):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table src (id int); insert into src select generate_series(1, 2500); analyze src")
    f = tmp_path / "m.sql"
    f.write_text("alter table src add column v int;\n")
    # The scratch database reads its own statistics here: the table already has rows, so it is left alone.
    r = subprocess.run([sys.executable, "-m", "pg_migration_check", "--dsn", dsn, "--rows-from", dsn, str(f)],
                       capture_output=True, text=True)
    assert "not filled public.src: already has rows" in r.stdout, r.stdout + r.stderr
