"""A corpus of migration patterns: the ones a careful reviewer must be warned about, and the safe forms that must
stay quiet. Each runs on its own copy of one seeded database (100,000 orders), with lock_timeout set so the
missing-lock_timeout warning does not count as a catch. The risky ones run with timing switched off (slow_ms so
high nothing counts as slow), so each catch comes from a rewrite, a text rule or a row count and holds at any table
size or machine speed. The safe ones run with the default slow_ms, where a brief lock is info."""

import uuid

import psycopg
import pytest

from pg_migration_check.check import check

ROWS = 100_000
SCHEMA = f"""
create table customers (id bigint primary key, name text not null);
insert into customers select g, 'c' || g from generate_series(1, 5000) g;
create type order_status as enum ('open', 'paid', 'shipped');
create table orders (id bigint generated always as identity primary key, customer_id bigint not null,
  email text, total numeric not null, status text, state order_status, created_at timestamptz not null default now());
insert into orders (customer_id, email, total, status, state, created_at)
  select 1 + g % 5000, 'u' || g || '@example.org', (g % 1000) / 10.0, case when g % 3 = 0 then null else 'open' end,
         'open', now() - (g % 720) * interval '1 day' from generate_series(1, {ROWS}) g;
create table events (id bigint, kind text);
insert into events select g, 'k' from generate_series(1, {ROWS}) g;
create materialized view order_totals as select customer_id, sum(total) as total from orders group by customer_id;
create index orders_email_idx on orders (email);
"""

FLAG = [
    ("add a column with a volatile default", "alter table orders add column token uuid default gen_random_uuid()"),
    ("change a column type", "alter table events alter column id type integer"),
    ("rename a column", "alter table orders rename column email to email_address"),
    ("rename a table", "alter table orders rename to purchases"),
    ("add a serial column", "alter table orders add column seq bigserial"),
    ("add a stored generated column",
     "alter table orders add column cents bigint generated always as ((total * 100)::bigint) stored"),
    ("add a CHECK constraint", "alter table orders add constraint total_positive check (total >= 0)"),
    ("add a FOREIGN KEY",
     "alter table orders add constraint orders_customer_fk foreign key (customer_id) references customers (id)"),
    ("create an index", "create index orders_created_idx on orders (created_at)"),
    ("add a UNIQUE constraint", "alter table orders add constraint orders_email_key unique (email)"),
    ("add a PRIMARY KEY", "alter table events add primary key (id)"),
    ("SET NOT NULL", "alter table orders alter column email set not null"),
    ("backfill with UPDATE", "update orders set status = 'open' where status is null"),
    ("delete old rows", "delete from orders where created_at < now() - interval '1 year'"),
    ("CONCURRENTLY inside BEGIN and COMMIT",
     "begin; create index concurrently orders_created_idx on orders (created_at); commit;"),
    ("VACUUM FULL", "vacuum full orders"),
    ("CLUSTER", "cluster orders using orders_pkey"),
    ("REFRESH MATERIALIZED VIEW", "refresh materialized view order_totals"),
    ("REINDEX without CONCURRENTLY", "reindex index orders_email_idx"),
    ("drop a column", "alter table orders drop column email"),
    ("drop a table", "drop table events"),
    ("SET UNLOGGED", "alter table orders set unlogged"),
    ("rename an enum value", "alter type order_status rename value 'open' to 'opened'"),
    ("text to varchar(n)", "alter table orders alter column status type varchar(10)"),
]

SAFE = [
    ("add a nullable column", "alter table orders add column note text"),
    ("add a column with a constant default", "alter table orders add column flag boolean not null default false"),
    ("create an index concurrently", "create index concurrently orders_created_idx on orders (created_at)"),
    ("CHECK NOT VALID, then VALIDATE", "alter table orders add constraint total_positive check (total >= 0) not valid;"
                                       " alter table orders validate constraint total_positive"),
    ("FOREIGN KEY NOT VALID, then VALIDATE",
     "alter table orders add constraint orders_customer_fk foreign key (customer_id) references customers (id) not valid;"
     " alter table orders validate constraint orders_customer_fk"),
    ("UNIQUE from a concurrent index", "create unique index concurrently orders_email_key on orders (email);"
                                       " alter table orders add constraint orders_email_key unique using index orders_email_key"),
    ("drop an index concurrently", "drop index concurrently orders_email_idx"),
    ("widen a varchar to text", "alter table orders alter column status type text"),
    ("add an enum value", "alter type order_status add value 'refunded'"),
    ("REFRESH MATERIALIZED VIEW CONCURRENTLY",
     "create unique index concurrently order_totals_key on order_totals (customer_id);"
     " refresh materialized view concurrently order_totals"),
    ("CONCURRENTLY after the transaction block",
     "begin; alter table orders add column note text; commit; create index concurrently orders_created_idx on orders (created_at)"),
    ("a small batch of a backfill",
     "update orders set status = 'open' where id in (select id from orders where status is null order by id limit 1000)"),
    ("VACUUM (plain)", "vacuum orders"),
    ("rename an enum value of a type this file makes",
     "create type ticket_state as enum ('new', 'done'); alter type ticket_state rename value 'new' to 'open'"),
]


@pytest.fixture(scope="module")
def template(server_uri):
    name = "corpus_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name}")
    with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name), autocommit=True) as c:
        c.execute(SCHEMA)
        c.execute("vacuum analyze")
    yield name
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")


def verdict(server_uri, template, sql, slow_ms=1000.0):
    name = "case_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name} template {template}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name)) as conn:
            results = check(conn, "set lock_timeout = '2s';\n" + sql, slow_ms=slow_ms)
    finally:
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"drop database {name} with (force)")
    errors = [r.error for r in results if r.error]
    loud = [f.text for r in results for f in r.findings if f.level in ("danger", "warn")]
    return errors, loud


@pytest.mark.parametrize("sql", [s for _, s in FLAG], ids=[n for n, _ in FLAG])
def test_a_risky_pattern_is_flagged(server_uri, template, sql):
    errors, loud = verdict(server_uri, template, sql, slow_ms=1e12)
    assert not errors, errors
    assert loud, "nothing above info"


@pytest.mark.parametrize("sql", [s for _, s in SAFE], ids=[n for n, _ in SAFE])
def test_a_safe_form_stays_quiet(server_uri, template, sql):
    errors, loud = verdict(server_uri, template, sql)
    assert not errors, errors
    assert not loud, loud


def test_a_materialized_view_refreshed_concurrently_blocks_no_reader(server_uri, template):
    # EXCLUSIVE on a matview blocks only writes and SELECT FOR UPDATE, which a matview never takes. Every lock
    # counts as slow here, so only the lock's meaning keeps it quiet; the plain refresh still blocks reads.
    quiet = "create unique index concurrently order_totals_key on order_totals (customer_id);" \
            " refresh materialized view concurrently order_totals"
    assert verdict(server_uri, template, quiet, slow_ms=0.0) == ([], [])
    errors, loud = verdict(server_uri, template, "refresh materialized view order_totals", slow_ms=0.0)
    assert not errors and any(t.startswith("ACCESS EXCLUSIVE on public.order_totals") for t in loud), loud


def test_a_backfill_names_its_rows_and_a_small_batch_is_only_info(server_uri, template):
    errors, loud = verdict(server_uri, template, "update orders set status = 'open' where status is null", slow_ms=1e12)
    assert any(t.startswith("UPDATE changes 33,333 rows of public.orders in one transaction") for t in loud), loud
    name = "case_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name} template {template}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name)) as conn:
            results = check(conn, "set lock_timeout = '2s'; delete from orders where id <= 500", slow_ms=1e12)
    finally:
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"drop database {name} with (force)")
    [(level, text)] = [(f.level, f.text) for r in results for f in r.findings]
    assert level == "info" and text.startswith("DELETE removes 500 rows of public.orders in one transaction"), text


def test_vacuum_full_under_rollback_is_reported_not_run(server_uri, template):
    name = "case_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name} template {template}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name)) as conn:
            node = conn.execute("select relfilenode from pg_class where relname = 'orders'").fetchone()
            conn.commit()
            with conn.transaction(force_rollback=True):
                [r] = [x for x in check(conn, "vacuum full orders", rollback=True) if x.n]  # n 0: no lock_timeout
            assert conn.execute("select relfilenode from pg_class where relname = 'orders'").fetchone() == node
    finally:
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"drop database {name} with (force)")
    assert r.error is None and r.rewritten == ["public.orders"] and r.locks == {"public.orders": "AccessExclusiveLock"}
    assert [f.level for f in r.findings][:1] == ["danger"] and r.findings[-1].level == "note"


def test_a_table_renamed_earlier_in_the_file_is_still_live(server_uri, template):
    name = "case_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name} template {template}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name)) as conn:
            results = check(conn, "set lock_timeout = '2s'; alter table orders rename to purchases;"
                                  " create index purchases_created_idx on purchases (created_at);"
                                  " vacuum full purchases", slow_ms=1e12)
    finally:
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"drop database {name} with (force)")
    index, vacuum = results[2], results[3]
    assert any(f.text.startswith("CREATE INDEX without CONCURRENTLY") for f in index.findings), index.findings
    assert vacuum.rewritten == ["public.purchases"] and vacuum.findings[0].level == "danger", vacuum.findings


def test_concurrently_in_a_file_that_runs_as_one_transaction_is_a_danger(server_uri, template):
    sql = "create index concurrently orders_created_idx on orders (created_at)"
    for one, want in ((True, True), (False, False)):
        name = "case_" + uuid.uuid4().hex[:8]
        with psycopg.connect(server_uri, autocommit=True) as c:
            c.execute(f"create database {name} template {template}")
        try:
            with psycopg.connect(psycopg.conninfo.make_conninfo(server_uri, dbname=name)) as conn:
                results = check(conn, "set lock_timeout = '2s'; " + sql, one_transaction=one)
        finally:
            with psycopg.connect(server_uri, autocommit=True) as c:
                c.execute(f"drop database {name} with (force)")
        dangers = [f.text for r in results for f in r.findings if f.level == "danger"]
        assert bool(dangers) is want and all(t.startswith("Postgres refuses this inside a transaction") for t in dangers)
