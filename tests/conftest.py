import os
import uuid

import psycopg
import pytest


@pytest.fixture(scope="session")
def server_uri():
    """A Postgres to create scratch databases in: $PGMC_TEST_DSN (CI service), else a local pgserver."""
    uri = os.environ.get("PGMC_TEST_DSN")
    if uri:
        yield uri
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(os.path.join(os.environ.get("TMPDIR", "/tmp"), "pgmc-test"), cleanup_mode="stop")
    yield srv.get_uri()


@pytest.fixture
def dsn(server_uri):
    name = "t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name}")
    yield psycopg.conninfo.make_conninfo(server_uri, dbname=name)
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table orders (id bigserial primary key, email text, qty int, created_at timestamptz default now())")
        c.execute("insert into orders (email, qty) select 'u' || g || '@example.com', g % 7 from generate_series(1, 20000) g")
        c.execute("create table customers (id bigserial primary key, name text)")
        c.execute("insert into customers (name) select 'c' || g from generate_series(1, 1000) g")
        c.execute("alter table orders add column customer_id bigint")
        c.execute("analyze")
        yield c
