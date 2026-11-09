"""manage.py check_migrations: what deploying the pending migrations would do to live tables.

The scratch database is built from nothing by applying every migration in order, so each one meets the schema it
will meet in production. The SQL is Django's own (what sqlmigrate prints). Migrations the project database has
already applied are applied silently; the rest are reported, statement by statement.
"""

from __future__ import annotations

import os
import sys

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

from pg_migration_check.check import check
from pg_migration_check.cli import report

RANK = {"info": 0, "warn": 1, "danger": 2}


class Command(BaseCommand):
    help = "Apply migrations to an empty scratch Postgres and report the locks, rewrites and lock time of each statement."

    def add_arguments(self, parser):
        parser.add_argument("app_label", nargs="?", help="report only this app's migrations")
        parser.add_argument("--dsn", default=os.environ.get("PGMC_SCRATCH_DSN"),
                            help="an EMPTY scratch database; it gets every migration applied (default: $PGMC_SCRATCH_DSN)")
        parser.add_argument("--all", action="store_true", help="report every migration, not only the unapplied ones")
        parser.add_argument("--database", default="default", help="the project database whose applied migrations count")
        parser.add_argument("--slow-ms", type=float, default=1000.0)
        parser.add_argument("--fail-on", choices=["danger", "warn", "never"], default="danger")
        parser.add_argument("-V", "--show-info", action="store_true", help="also show brief locks (info)")

    def handle(self, *args, **o):
        connection = connections[o["database"]]
        if connection.vendor != "postgresql":
            raise CommandError(f"the {o['database']!r} database is {connection.vendor}; this checks Postgres migrations")
        if not o["dsn"]:
            raise CommandError("give --dsn (or set PGMC_SCRATCH_DSN): an empty scratch database, it gets changed")
        executor = MigrationExecutor(connection)
        applied = set(executor.loader.applied_migrations)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True)
        # What sqlmigrate prints: on the loader since Django 6.0, on the executor before.
        collect_sql = getattr(executor.loader, "collect_sql", None) or executor.collect_sql
        with psycopg.connect(o["dsn"], autocommit=True) as conn:
            used = conn.execute("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                "where n.nspname not in ('pg_catalog', 'information_schema') "
                                "and n.nspname not like 'pg_toast%' and c.relkind in ('r', 'p', 'v', 'm')").fetchone()[0]
            if used:
                raise CommandError("the scratch database is not empty: give an empty one, every migration is applied to it")
            worst, reported = -1, 0
            for migration, _ in plan:
                key = (migration.app_label, migration.name)
                sql = "\n".join(collect_sql([(migration, False)]))
                results = check(conn, sql, o["slow_ms"])
                shown = (o["all"] or key not in applied) and o["app_label"] in (None, migration.app_label)
                failed = next((r for r in results if r.error), None)
                if shown:
                    reported += 1
                    title = f"{migration.app_label}.{migration.name}" + ("" if migration.atomic else " (atomic = False)")
                    report(title, results, self.stdout, o["show_info"], one_transaction=migration.atomic)
                    worst = max([worst] + [RANK[f.level] for r in results for f in r.findings])
                if failed:
                    raise CommandError(f"{migration.app_label}.{migration.name} failed on the scratch database: "
                                       f"{failed.error}")
        self.stdout.write(f"\n{reported} migration{'s' if reported != 1 else ''} checked"
                          f"{'' if o['all'] else ' (not yet applied to the ' + repr(o['database']) + ' database)'}.\n")
        if worst >= {"danger": 2, "warn": 1, "never": 99}[o["fail_on"]]:
            sys.exit(1)
