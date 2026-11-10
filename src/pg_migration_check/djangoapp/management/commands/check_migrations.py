"""manage.py check_migrations: what deploying the pending migrations would do to live tables.

The scratch database is built from nothing by applying every migration in order, so each one meets the schema it
will meet in production. The SQL is Django's own (what sqlmigrate prints). Migrations the project database has
already applied are applied silently; the rest are reported, statement by statement.

With --rollback the database is instead a copy of production that already holds real data: its own
django_migrations table says what is pending, the pending migrations run in one transaction, and it is rolled back.
"""

from __future__ import annotations

import os
import sys

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

from pg_migration_check.check import TransactionEnded, check
from pg_migration_check.cli import report
from pg_migration_check.seed import counts_from, parse_rows, seed
from pg_migration_check.seed import report as seed_report

RANK = {"info": 0, "note": 0, "warn": 1, "danger": 2}


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
        parser.add_argument("--rows", action="append", metavar="[TABLE=]N",
                            help="before each reported migration, fill the empty tables it names, and the tables they "
                                 "reference, with N synthetic rows so lock times are real (TABLE=N for one table)")
        parser.add_argument("--rows-from", metavar="DSN", help="take each table's row count for --rows from this "
                                 "database's statistics, read only (production is fine: no table is scanned)")
        parser.add_argument("--rollback", action="store_true",
                            help="--dsn is a COPY of production with real data: check the migrations its own "
                                 "django_migrations table has not applied, in one transaction, and roll it back. Its "
                                 "locks are held until the run ends: never point it at production")

    def handle(self, *args, **o):
        connection = connections[o["database"]]
        if connection.vendor != "postgresql":
            raise CommandError(f"the {o['database']!r} database is {connection.vendor}; this checks Postgres migrations")
        if not o["dsn"]:
            raise CommandError("give --dsn (or set PGMC_SCRATCH_DSN): an empty scratch database, it gets changed")
        try:
            every, per = parse_rows(o["rows"])
        except ValueError as e:
            raise CommandError(str(e))
        if o["rollback"] and (o["rows"] or o["rows_from"] or o["all"]):
            raise CommandError("--rollback checks what a copy of production has not applied yet, on its real rows: "
                               "--rows, --rows-from and --all do not apply")
        sizes = counts_from(o["rows_from"]) if o["rows_from"] else None
        executor = MigrationExecutor(connection)
        applied = set(executor.loader.applied_migrations)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True)
        # What sqlmigrate prints: on the loader since Django 6.0, on the executor before.
        collect_sql = getattr(executor.loader, "collect_sql", None) or executor.collect_sql
        if o["rollback"]:
            return self._rollback(o, executor.loader, plan, collect_sql)
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
                shown = (o["all"] or key not in applied) and o["app_label"] in (None, migration.app_label)
                if shown:  # tables made by earlier migrations get their rows now; filled ones are left alone
                    fresh = [x for x in seed(conn, every, per, sql, sizes) if x.skipped != "already has rows"]
                    if fresh:
                        self.stdout.write(f"\nBefore {migration.app_label}.{migration.name}:\n")
                        seed_report(fresh, self.stdout)
                results = check(conn, sql, o["slow_ms"], one_transaction=migration.atomic)
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

    def _rollback(self, o, loader, plan, collect_sql):
        """Run what the copy has not applied, in one transaction that is rolled back, and report it."""
        with psycopg.connect(o["dsn"], autocommit=True, application_name="pg-migration-check") as conn:
            if not conn.execute("select to_regclass('django_migrations')").fetchone()[0]:
                raise CommandError("the --dsn database has no django_migrations table: --rollback needs a copy of a "
                                   "database Django has migrated")
            applied = set(conn.execute("select app, name from django_migrations").fetchall())
            for key, squashed in loader.replacements.items():  # a squashed migration counts once all it replaces ran
                if all(r in applied for r in squashed.replaces):
                    applied.add(key)
            pending = [m for m, _ in plan if (m.app_label, m.name) not in applied]
            worst, reported = -1, 0
            try:
                with conn.transaction(force_rollback=True):
                    for migration in pending:
                        sql = "\n".join(collect_sql([(migration, False)]))
                        try:
                            results = check(conn, sql, o["slow_ms"], rollback=True, one_transaction=migration.atomic)
                        except ValueError as e:
                            raise CommandError(f"{migration.app_label}.{migration.name}: {e}")
                        failed = next((r for r in results if r.error), None)
                        if o["app_label"] in (None, migration.app_label):  # others still run: later ones need them
                            reported += 1
                            title = f"{migration.app_label}.{migration.name}" + ("" if migration.atomic else " (atomic = False)")
                            report(title, results, self.stdout, o["show_info"], one_transaction=migration.atomic)
                            worst = max([worst] + [RANK[f.level] for r in results for f in r.findings])
                        if failed:
                            raise CommandError(f"{migration.app_label}.{migration.name} failed on the copy: {failed.error} "
                                               "(rolled back)")
            except TransactionEnded as e:
                raise CommandError(f"NOT ROLLED BACK: {e}", returncode=3)
        self.stdout.write(f"\n{reported} migration{'s' if reported != 1 else ''} checked (not yet applied to the copy). "
                          "Rolled back: the copy is as it was before the run (sequences excepted).\n")
        if worst >= {"danger": 2, "warn": 1, "never": 99}[o["fail_on"]]:
            sys.exit(1)
