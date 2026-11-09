"""manage.py check_migrations on a small Django project (tests/djangoproj) against a real Postgres."""

import io
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

django = pytest.importorskip("django")


@pytest.fixture(scope="module")
def project(server_uri):
    """The project database (what counts as applied) and Django set up to use it."""
    from django.conf import settings

    name = "proj_" + uuid.uuid4().hex[:8]
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"create database {name}")
    info = psycopg.conninfo.conninfo_to_dict(psycopg.conninfo.make_conninfo(server_uri, dbname=name))
    sys.path.insert(0, str(Path(__file__).parent / "djangoproj"))
    if not settings.configured:
        settings.configure(
            INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "pg_migration_check.djangoapp", "shop"],
            DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": info["dbname"],
                                   "USER": info.get("user", ""), "PASSWORD": info.get("password", ""),
                                   "HOST": info.get("host", ""), "PORT": info.get("port", "")}},
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField", USE_TZ=True)
        django.setup()
    yield
    from django.db import connections
    connections.close_all()
    with psycopg.connect(server_uri, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")


def run(*args) -> tuple[int, str]:
    from django.core.management import call_command

    out = io.StringIO()
    try:
        call_command("check_migrations", *args, stdout=out)
        code = 0
    except SystemExit as e:
        code = int(e.code or 0)
    return code, out.getvalue()


def test_every_migration_with_all(project, dsn):
    code, out = run("--dsn", dsn, "--all")
    assert code == 1, out  # the int to bigint rewrite is a danger
    part = out.split("shop.0002_widen_qty_and_index")[1].split("shop.0003_note")[0]
    assert "rewrites public.shop_order" in part
    assert "CREATE INDEX without CONCURRENTLY blocks writes" in part
    assert "held until it commits" in part  # Django's one transaction
    assert "DANGER" not in out.split("shop.0003_note")[1]
    assert "auth.0001_initial" in out and "migrations checked." in out


def test_only_what_the_database_has_not_applied(project, dsn):
    from django.core.management import call_command

    call_command("migrate", "shop", "0001", verbosity=0)
    call_command("migrate", "auth", verbosity=0)
    code, out = run("--dsn", dsn)
    assert "shop.0002_widen_qty_and_index" in out and "shop.0003_note" in out
    assert "shop.0001_initial" not in out and "auth.0001_initial" not in out
    assert "2 migrations checked (not yet applied" in out and code == 1


def test_refuses_a_scratch_database_that_is_not_empty(project, dsn):
    from django.core.management.base import CommandError

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table leftover (id int)")
    with pytest.raises(CommandError, match="not empty"):
        run("--dsn", dsn, "--all")


def test_rows_fill_the_tables_before_the_unapplied_migrations(project, dsn):
    code, out = run("--dsn", dsn, "--rows", "30000", "--rows", "shop_note=0")
    filled = out.index("Before shop.0002_widen_qty_and_index:\n  ")
    assert "filled public.shop_order with 30,000 rows" in out[filled:].split("\n\n")[0]
    assert "on public.shop_order (30,000 rows)" in out[filled:].split("shop.0003_note")[0]
    assert code == 1
