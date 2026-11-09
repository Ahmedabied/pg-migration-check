"""--rollback: the whole run in one transaction, rolled back, so a staging copy with real data is left as it was."""

import json
import subprocess
import sys

import psycopg
import pytest

from pg_migration_check.check import check, held_until_commit

UNSAFE = """
alter table orders alter column qty type bigint;
create index orders_email_idx on orders (email);
alter table orders alter column email set not null;
alter table orders rename column created_at to placed_at;
alter table orders add column score float8 default random();
update orders set qty = qty + 1 where id % 3 = 0;
alter table orders drop column customer_id;
create table audit (id bigserial primary key, note text);
insert into audit (note) select 'x' from generate_series(1, 50);
create index on customers (name);
"""


def state(c) -> dict:
    """Everything a migration could change that a rollback must put back: schema, row counts, data."""
    q = lambda sql: c.execute(sql).fetchall()
    return {
        "tables": q("select table_name from information_schema.tables where table_schema = 'public' order by 1"),
        "columns": q("select table_name, column_name, data_type, is_nullable, column_default "
                     "from information_schema.columns where table_schema = 'public' order by 1, 2"),
        "indexes": q("select indexname, indexdef from pg_indexes where schemaname = 'public' order by 1"),
        "constraints": q("select conname, pg_get_constraintdef(oid) from pg_constraint "
                         "where connamespace = 'public'::regnamespace order by 1"),
        "files": q("select relname, relfilenode from pg_class where relnamespace = 'public'::regnamespace order by 1"),
        "orders": q("select count(*), md5(string_agg(o::text, ',' order by id)) from orders o"),
        "customers": q("select count(*), md5(string_agg(c::text, ',' order by id)) from customers c"),
    }


def run(dsn, *args):
    return subprocess.run([sys.executable, "-m", "pg_migration_check", *args, "--dsn", dsn],
                          capture_output=True, text=True)


def test_the_database_is_as_it_was_after_a_rollback_run(conn, dsn, tmp_path):
    before = state(conn)
    f = tmp_path / "0042.sql"
    f.write_text(UNSAFE)
    r = run(dsn, str(f), "--rollback")
    assert r.returncode == 1, r.stderr  # it found the dangers
    assert "rewrites public.orders" in r.stdout and "Rolled back" in r.stdout
    assert state(conn) == before


def test_a_rollback_run_still_sees_rewrites_and_locks(conn):
    with conn.transaction(force_rollback=True):
        res = check(conn, UNSAFE, rollback=True)
    stmts = {x.n: x for x in res if x.n}
    assert all(x.error is None for x in stmts.values())
    assert stmts[1].rewritten == ["public.orders"] and stmts[1].locks == {"public.orders": "AccessExclusiveLock"}
    assert "danger" in [f.level for f in stmts[1].findings]
    assert stmts[5].rewritten == ["public.orders"]  # a second rewrite in the same transaction is seen too
    # SHARE inside the ACCESS EXCLUSIVE held since statement 1: Postgres adds nothing, so nothing is shown
    assert stmts[2].locks == {} and stmts[4].locks == {}
    assert stmts[10].locks == {"public.customers": "ShareLock"}  # a table not locked yet: shown
    assert "danger" in [f.level for f in stmts[4].findings]  # but the rename is still a danger for running code
    assert stmts[8].findings == [] and stmts[9].findings == []  # a table made by this file: nothing live to hurt
    assert held_until_commit(res)["public.orders"] == pytest.approx(sum(x.ms for x in stmts.values()))
    assert held_until_commit(res)["public.customers"] == pytest.approx(stmts[10].ms)
    assert conn.execute("select count(*) from information_schema.tables where table_name = 'audit'").fetchone()[0] == 0


def test_a_rollback_run_reports_what_an_applied_run_reports(conn):
    sql = ("alter table orders alter column qty type bigint; create index on customers (name);"
           "alter table orders add column note text")
    with conn.transaction(force_rollback=True):
        rolled = [x for x in check(conn, sql, rollback=True) if x.n]
    applied = [x for x in check(conn, sql) if x.n]
    assert [x.rewritten for x in rolled] == [x.rewritten for x in applied] == [["public.orders"], [], []]
    assert rolled[0].locks == applied[0].locks and rolled[1].locks == applied[1].locks
    # the third statement asks for ACCESS EXCLUSIVE again: applied alone it takes it, in one transaction it holds it
    assert applied[2].locks == {"public.orders": "AccessExclusiveLock"} and rolled[2].locks == {}


def test_an_error_stops_the_run_and_leaves_nothing_behind(conn, dsn, tmp_path):
    before = state(conn)
    f = tmp_path / "bad.sql"
    f.write_text("alter table orders add column a int; alter table nope add column b int; alter table orders add column c int")
    r = run(dsn, str(f), "--rollback", "--json")
    stmts = json.loads(r.stdout)[0]["statements"]
    assert [s["n"] for s in stmts if s["n"]] == [1, 2] and "UndefinedTable" in stmts[1]["error"]
    assert r.returncode == 1 and state(conn) == before


def test_concurrently_is_timed_in_its_plain_form_and_later_statements_still_work(conn):
    sql = ("set lock_timeout = '2s';"
           "create unique index concurrently orders_id_u on orders (id);"
           "alter table orders add constraint orders_id_key unique using index orders_id_u")
    with conn.transaction(force_rollback=True):
        res = [x for x in check(conn, sql, rollback=True) if x.n]
    assert all(x.error is None for x in res)
    assert res[1].concurrently and res[1].locks == {"public.orders": "ShareUpdateExclusiveLock"}
    assert [f.level for f in res[1].findings] == ["note"] and res[1].ms > 0
    assert held_until_commit(res) == {}  # a file with CONCURRENTLY cannot run in one transaction for real
    assert conn.execute("select count(*) from pg_indexes where indexname = 'orders_id_u'").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["rollback", "savepoint s1", "release savepoint s1", "abort", "prepare transaction 'x'"])
def test_a_file_that_ends_its_transaction_is_refused_before_anything_runs(conn, dsn, tmp_path, bad):
    before = state(conn)
    f = tmp_path / "x.sql"
    f.write_text(f"alter table orders add column a int; {bad}; alter table orders add column b int;")
    r = run(dsn, str(f), "--rollback")
    assert r.returncode == 2 and "ends or splits its transaction" in r.stderr
    assert state(conn) == before


def test_rows_and_rollback_are_not_mixed(dsn, tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("select 1")
    r = run(dsn, str(f), "--rollback", "--rows", "1000")
    assert r.returncode == 2 and "Use one" in r.stderr


def test_several_files_run_in_one_transaction_in_order(conn, dsn, tmp_path):
    before = state(conn)
    a, b = tmp_path / "1.sql", tmp_path / "2.sql"
    a.write_text("set lock_timeout = '2s'; alter table orders add column note text;")
    b.write_text("set lock_timeout = '2s'; update orders set note = 'n' where id < 10; create index on orders (note);")
    r = run(dsn, str(a), str(b), "--rollback", "--json")
    files = json.loads(r.stdout)
    assert r.returncode == 0, r.stdout
    assert all(f["rolled_back"] for f in files) and all(s["error"] is None for f in files for s in f["statements"])
    assert state(conn) == before


def test_a_lookup_that_finds_nothing_does_not_end_the_rollback_transaction(conn):
    sql = "set lock_timeout = '2s'; drop index concurrently if exists nosuchschema.nope; alter table orders add column z int"
    with conn.transaction(force_rollback=True):
        res = [x for x in check(conn, sql, rollback=True) if x.n]
    assert [x.error for x in res] == [None, None, None]


def test_an_escape_string_cannot_hide_a_rollback(conn, dsn, tmp_path):
    # E'\'' is one quote character in Postgres; a splitter that only knows '' would read on to the next quote and
    # send "rollback; alter table ..." to the server as part of one statement.
    before = state(conn)
    f = tmp_path / "x.sql"
    f.write_text("select E'\\''; rollback; alter table orders add column hacked int; select 'x'")
    r = run(dsn, str(f), "--rollback")
    assert r.returncode == 2 and "ends or splits its transaction" in r.stderr
    assert state(conn) == before


def test_one_statement_per_round_trip_even_if_the_splitter_is_wrong(conn, monkeypatch):
    # Second line of defence: statements go over the extended protocol, which refuses a string with two commands.
    import pg_migration_check.check as chk

    before = state(conn)
    monkeypatch.setattr(chk, "split", lambda sql: [sql])
    sql = "alter table orders add column a int; rollback; alter table orders add column hacked int"
    with conn.transaction(force_rollback=True):
        (r,) = [x for x in chk.check(conn, sql, rollback=True) if x.n]
    assert r.error and "multiple commands" in r.error
    assert state(conn) == before


def test_the_run_never_says_rolled_back_when_the_transaction_ended_early(conn, dsn, tmp_path, monkeypatch, capsys):
    # Third line of defence, tested by taking the first two away: whatever ends the transaction, the run stops
    # and says so, and does not print "Rolled back".
    import pg_migration_check.check as chk
    from pg_migration_check import cli

    monkeypatch.setattr(chk, "split", lambda sql: [sql])
    monkeypatch.setattr(chk, "_execute", lambda c, stmt: c.execute(stmt))  # the simple protocol runs both commands
    f = tmp_path / "x.sql"
    f.write_text("alter table orders add column a int; rollback; alter table orders add column b int")
    assert cli.main([str(f), "--rollback", "--dsn", dsn]) == 3
    out = capsys.readouterr()
    assert "NOT ROLLED BACK" in out.err and "Rolled back" not in out.out


def test_a_bad_set_is_reported_and_does_not_poison_the_run(conn):
    with conn.transaction(force_rollback=True):
        res = [x for x in check(conn, "set lock_timeout = 'soon'; alter table orders add column a int", rollback=True) if x.n]
    assert len(res) == 1 and "InvalidParameterValue" in res[0].error
    applied = [x for x in check(conn, "set lock_timeout = 'soon'; alter table orders add column a int") if x.n]
    assert len(applied) == 1 and applied[0].error
