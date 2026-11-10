import json
import subprocess
import sys

import pytest

from pg_migration_check.check import check, held_until_commit, split, text_rules


def levels(r):
    return [f.level for f in r.findings]


def test_split_respects_quotes_comments_and_dollar_quotes():
    sql = """-- a comment; with a semicolon
    insert into t values ('a;b', 'it''s; fine');
    /* block; comment */ create function f() returns int as $$ select 1; $$ language sql;
    select "odd;name" from t"""
    out = split(sql)
    assert len(out) == 3
    assert "'a;b'" in out[0] and "$$ select 1; $$" in out[1] and out[2].endswith('from t')


def test_rewrite_is_seen_and_is_a_danger(conn):
    (r,) = [x for x in check(conn, "alter table orders alter column qty type bigint") if x.n]
    assert r.rewritten == ["public.orders"]
    assert r.locks["public.orders"] == "AccessExclusiveLock"
    assert "danger" in levels(r)


def test_adding_a_nullable_column_does_not_rewrite(conn):
    (r,) = [x for x in check(conn, "set lock_timeout = '2s'; alter table orders add column note text") if x.n == 2]
    assert r.rewritten == [] and r.locks["public.orders"] == "AccessExclusiveLock"
    assert "danger" not in levels(r)


def test_a_constant_default_does_not_rewrite_but_a_volatile_one_does(conn):
    res = check(conn, "set lock_timeout='2s'; alter table orders add column a int not null default 0;"
                      "alter table orders add column b float8 default random()")
    a, b = [x for x in res if x.n in (2, 3)]
    assert a.rewritten == [] and b.rewritten == ["public.orders"]


def test_index_without_concurrently_blocks_writes(conn):
    (r,) = [x for x in check(conn, "create index on orders (email)") if x.n]
    assert r.locks["public.orders"] == "ShareLock"
    assert any("CONCURRENTLY" in f.text for f in r.findings)


def test_concurrent_index_runs_outside_a_transaction(conn):
    res = check(conn, "create index concurrently orders_email on orders (email)")
    (r,) = [x for x in res if x.n]
    assert r.error is None and r.concurrently and r.locks == {"public.orders": "ShareUpdateExclusiveLock"}
    assert not any(f.level != "info" for f in r.findings)
    assert held_until_commit(res) == {}
    assert conn.execute("select count(*) from pg_indexes where indexname = 'orders_email'").fetchone()[0] == 1


def test_drop_index_concurrently_names_the_table_not_the_index(conn):
    conn.execute("create index orders_qty on orders (qty)")
    (r,) = [x for x in check(conn, 'DROP INDEX CONCURRENTLY IF EXISTS "public"."orders_qty"') if x.n]
    assert r.error is None and r.concurrently and r.locks == {"public.orders": "ShareUpdateExclusiveLock"}


def test_the_word_concurrently_elsewhere_does_not_hide_a_rewrite(conn):
    # Only the statement's shape says CONCURRENTLY; a column name or a string that contains the word does not.
    (r,) = [x for x in check(conn, "alter table orders add column last_run_concurrently timestamptz "
                                   "default clock_timestamp()") if x.n]
    assert not r.concurrently
    assert r.rewritten == ["public.orders"] and r.locks["public.orders"] == "AccessExclusiveLock"
    assert "danger" in levels(r)


def test_refresh_materialized_view_concurrently_runs_in_a_transaction_and_blocks_writes(conn):
    conn.execute("create materialized view order_qty as select qty, count(*) n from orders group by qty")
    conn.execute("create unique index on order_qty (qty)")
    (r,) = [x for x in check(conn, "refresh materialized view concurrently order_qty") if x.n]
    assert r.error is None and not r.concurrently
    assert r.locks["public.order_qty"] == "ExclusiveLock" and r.locks["public.orders"] == "AccessShareLock"


def test_foreign_key_locks_both_tables_and_not_valid_is_the_fix(conn):
    (r,) = [x for x in check(conn, "alter table orders add foreign key (customer_id) references customers (id)") if x.n]
    assert r.locks["public.orders"] == "ShareRowExclusiveLock" and r.locks["public.customers"] == "ShareRowExclusiveLock"
    assert any("NOT VALID" in f.text for f in r.findings)
    assert not text_rules("alter table orders add constraint fk foreign key (customer_id) references customers (id) not valid")


def test_rename_is_a_danger_for_running_code(conn):
    (r,) = [x for x in check(conn, "alter table orders rename column email to mail") if x.n]
    assert "danger" in levels(r)
    assert not [f for f in text_rules("alter table orders rename constraint a to b") if "rename" in f.text]


def test_missing_lock_timeout_is_reported_once(conn):
    res = check(conn, "create index on orders (qty); create index on orders (email)")
    assert sum(1 for x in res if x.n == 0) == 1
    assert not any(x.n == 0 for x in check(conn, "set lock_timeout = '1s'; create index on orders (created_at)"))


def test_django_transaction_wrapper_is_ignored_and_lock_time_adds_up(conn):
    res = check(conn, "BEGIN;\nalter table orders alter column qty type bigint;\ncreate index on orders (email);\nCOMMIT;")
    stmts = [x for x in res if x.n]
    assert len(stmts) == 2
    held = held_until_commit(res)
    assert abs(held["public.orders"] - sum(x.ms for x in stmts)) < 0.01


def test_stops_at_the_first_error(conn):
    res = check(conn, "alter table nope add column x int; alter table orders add column y int")
    stmts = [x for x in res if x.n]
    assert len(stmts) == 1 and "UndefinedTable" in stmts[0].error


def test_new_tables_are_not_reported(conn):
    res = check(conn, "create table fresh (id int); create index on fresh (id); alter table fresh alter column id type bigint")
    assert all(x.findings == [] and x.locks == {} for x in res)


def test_cli_exit_codes_and_json(dsn, tmp_path):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table t (id int, v int)")
        c.execute("insert into t select g, g from generate_series(1, 1000) g")
        c.execute("analyze t")
    bad = tmp_path / "bad.sql"
    bad.write_text("alter table t alter column v type bigint;")
    good = tmp_path / "good.sql"
    good.write_text("set lock_timeout = '2s'; alter table t add column note text;")
    run = lambda *a: subprocess.run([sys.executable, "-m", "pg_migration_check", *a, "--dsn", dsn], capture_output=True, text=True)
    r = run(str(bad))
    assert r.returncode == 1 and "DANGER" in r.stdout and "rewrites public.t" in r.stdout
    r = run(str(good))
    assert r.returncode == 0, r.stdout
    good2 = tmp_path / "good2.sql"
    good2.write_text("set lock_timeout = '2s'; alter table t add column other text;")
    r = run(str(good2), "--json", "--fail-on", "never")
    data = json.loads(r.stdout)
    assert data[0]["statements"][1]["locks"] == {"public.t": "ACCESS EXCLUSIVE"}


def test_set_not_null_after_a_validated_check_is_not_flagged(conn):
    res = check(conn, "set lock_timeout = '2s';"
                      "alter table orders add constraint e_nn check (email is not null) not valid;"
                      "alter table orders validate constraint e_nn;"
                      "alter table orders alter column email set not null")
    last = [x for x in res if x.n == 4][0]
    assert last.error is None and not [f for f in last.findings if f.level != "info"]
    plain = [x for x in check(conn, "set lock_timeout = '2s'; alter table orders alter column qty set not null") if x.n == 2][0]
    assert any(f.text.startswith("SET NOT NULL") for f in plain.findings)


def test_not_null_rules_know_postgres_18():
    warn = lambda sql, v=0: [f.text for f in text_rules(sql, v) if f.level == "warn"]  # noqa: E731
    assert any("NOT NULL constraint" in w for w in warn("alter table orders add constraint q_nn not null qty"))
    assert any("NOT NULL constraint" in w for w in warn("ALTER TABLE orders ADD NOT NULL qty"))
    assert warn("alter table orders add constraint q_nn not null qty not valid") == []
    assert warn("alter table orders add column z int not null default 0") == []
    assert "CHECK (col IS NOT NULL)" in warn("alter table orders alter column qty set not null", 170004)[0]
    assert "NOT NULL col NOT VALID" in warn("alter table orders alter column qty set not null", 180001)[0]


def test_set_not_null_on_a_column_that_is_not_null_already_is_not_flagged(conn):
    conn.execute('alter table orders add column "Note" text not null default \'\'')
    for sql in ["alter table orders alter column id set not null", 'alter table orders alter column "Note" set not null']:
        res = [x for x in check(conn, "set lock_timeout = '2s';" + sql) if x.n == 2][0]
        assert res.error is None and not [f for f in res.findings if f.text.startswith("SET NOT NULL")], sql


def test_postgres_18_not_null_not_valid(conn):
    if conn.info.server_version < 180000:
        pytest.skip("NOT NULL ... NOT VALID is new in Postgres 18")
    conn.execute("insert into orders (email, qty) values (null, 1)")  # NOT VALID leaves an old NULL alone
    res = check(conn, "set lock_timeout = '2s';"
                      "alter table orders add constraint q_nn not null qty not valid;"
                      "alter table orders validate constraint q_nn")
    assert [x.error for x in res] == [None, None, None]
    assert [f.text for x in res for f in x.findings if f.level != "info"] == []
    assert res[2].locks == {"public.orders": "ShareUpdateExclusiveLock"}
    conn.execute("alter table orders add constraint e_nn not null email not valid")  # attnotnull is now true
    later = [x for x in check(conn, "set lock_timeout = '2s'; alter table orders alter column email set not null")
             if x.n == 2][0]
    assert "contains null values" in (later.error or "")  # it read the rows: still flagged
    assert any("NOT NULL col NOT VALID" in f.text for f in later.findings)
    plain = [x for x in check(conn, "set lock_timeout = '2s'; alter table orders add constraint c_nn not null created_at")
             if x.n == 2][0]
    assert plain.locks == {"public.orders": "AccessExclusiveLock"}
    assert any("NOT NULL constraint" in f.text for f in plain.findings)


def test_concurrent_shapes():
    from pg_migration_check.check import is_concurrent

    yes = ["create unique index concurrently if not exists i on t (a)", "DROP INDEX CONCURRENTLY i",
           "reindex (verbose) index concurrently i", "reindex table concurrently t", "reindex (concurrently) table t",
           "reindex (verbose, concurrently true) table t", "alter table m detach partition m_2020 concurrently"]
    no = ["refresh materialized view concurrently mv", "reindex (concurrently false) table t",
          "alter table t add column concurrently_seen bool", "update t set note = 'built concurrently'",
          "create index i on t (concurrently)", "alter table m detach partition m_2020 finalize"]
    assert [s for s in yes if not is_concurrent(s)] == []
    assert [s for s in no if is_concurrent(s)] == []


def test_split_knows_escape_strings_and_dollar_tags_with_digits():
    out = split("select E'it\\'s; fine'; select e'\\\\'; select $f1$ a; b $f1$; select 'a''b;c'")
    assert out == ["select E'it\\'s; fine'", "select e'\\\\'", "select $f1$ a; b $f1$", "select 'a''b;c'"]


def test_a_missing_file_is_a_one_line_error(dsn, tmp_path):
    r = subprocess.run([sys.executable, "-m", "pg_migration_check", str(tmp_path / "nope.sql"), "--dsn", dsn],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "no such file" in r.stderr and "Traceback" not in r.stderr
