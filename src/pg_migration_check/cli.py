"""Command line: pg-migration-check MIGRATION.sql ... --dsn postgresql://.../scratch"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .check import LOCKS, check, held_until_commit
from .seed import counts_from, parse_rows, seed
from .seed import report as seed_report

MARK = {"danger": "DANGER", "warn": "warn  ", "info": "info  ", "note": "note  "}


def report(path: str, results, out, verbose: bool = False, one_transaction: bool | None = None,
           rollback: bool = False) -> None:
    """one_transaction: True when the file is known to run in one transaction, False when it is known not to.
    rollback: it ran inside --rollback's one transaction."""
    out.write(f"\n{path}\n")
    for r in results:
        if r.n == 0:
            for f in r.findings:
                out.write(f"  {MARK[f.level]}  {f.text}\n")
            continue
        first = " ".join(r.sql.split())
        out.write(f"  {r.n:>2}. {first[:96]}{'...' if len(first) > 96 else ''}  ({r.ms:,.0f} ms)\n")
        if r.error:
            out.write(f"      error  {r.error}\n")
        for f in r.findings:
            if f.level != "info" or verbose:
                out.write(f"      {MARK[f.level]}  {f.text}\n")
    held = held_until_commit(results) if one_transaction is not False else {}
    if held:
        out.write("  In --rollback's one transaction, each lock is held to the end of the file at least:\n"
                  if rollback else
                  "  Django runs this migration in one transaction, so the locks are held until it commits:\n"
                  if one_transaction else
                  "  If the whole file runs in one transaction (Django does this unless atomic = False), the locks are "
                  "held until it commits:\n")
        for t, ms in held.items():
            top = max((LOCKS[r.locks[t]][0] for r in results if t in r.locks and r.locks[t] in LOCKS), default=0)
            who = "reads and writes wait" if top == 8 else "writes wait"
            out.write(f"      {t}: {who} {ms:,.0f} ms\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pg-migration-check", description="Apply migrations to a scratch Postgres "
                                "and report locks, rewrites and lock time per statement.")
    p.add_argument("files", nargs="+", help="SQL files, applied in order (Django: python manage.py sqlmigrate app 0042)")
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="a SCRATCH database; it gets changed unless "
                   "--rollback (default: $DATABASE_URL)")
    p.add_argument("--slow-ms", type=float, default=1000, help="ACCESS EXCLUSIVE held this long is a danger (default 1000)")
    p.add_argument("--fail-on", choices=["danger", "warn", "never"], default="danger")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="also show brief locks (info)")
    p.add_argument("--rows", action="append", metavar="[TABLE=]N", help="first fill the empty tables the files name, and the "
                   "tables they reference, with N synthetic rows so lock times are real (TABLE=N for one table)")
    p.add_argument("--rollback", action="store_true", help="run everything in ONE transaction and roll it back at the "
                   "end, so a staging copy with real data is left as it was (sequences excepted). Its locks are held "
                   "until the run ends: never point it at production")
    p.add_argument("--rows-from", metavar="DSN", help="take each table's row count for --rows from this database's "
                   "statistics, read only (production is fine: no table is scanned)")
    a = p.parse_args(argv)
    try:
        every, per = parse_rows(a.rows)
    except ValueError as e:
        p.error(str(e))
    if a.rollback and (a.rows or a.rows_from):
        p.error("--rows fills an empty scratch database; --rollback is for a copy that already holds real data. Use one")
    sizes = counts_from(a.rows_from) if a.rows_from else None
    if not a.dsn:
        p.error("give --dsn or set DATABASE_URL (a scratch database: the migration is applied)")
    import contextlib

    import psycopg

    worst = 0
    rank = {"info": 0, "note": 0, "warn": 1, "danger": 2}
    all_out = []
    with psycopg.connect(a.dsn, application_name="pg-migration-check") as conn:
        if a.rollback:
            conn.autocommit = True
            outer = conn.transaction(force_rollback=True)
        else:
            seeded = seed(conn, every, per, "\n".join(Path(f).read_text(encoding="utf-8") for f in a.files), sizes)
            if seeded:
                out = sys.stderr if a.json else sys.stdout
                out.write("Before the migration:\n")
                seed_report(seeded, out)
            outer = contextlib.nullcontext()
        try:
            with outer:
                for f in a.files:
                    results = check(conn, Path(f).read_text(encoding="utf-8"), a.slow_ms, rollback=a.rollback)
                    if a.json:
                        all_out.append({"file": f, "rolled_back": a.rollback, "statements": [
                            {"n": r.n, "sql": r.sql, "ms": round(r.ms, 1), "error": r.error, "rewritten": r.rewritten,
                             "locks": {t: LOCKS.get(m, (0, m))[1] for t, m in r.locks.items()}, "rows": r.rows,
                             "findings": [{"level": x.level, "text": x.text} for x in r.findings]} for r in results],
                            "held_until_commit_ms": {t: round(v, 1) for t, v in held_until_commit(results).items()}})
                    else:
                        report(f, results, sys.stdout, a.verbose, rollback=a.rollback)
                    for r in results:
                        worst = max([worst] + [rank[x.level] for x in r.findings] + ([2] if r.error else []))
        except ValueError as e:  # a file --rollback refuses: nothing was run
            print(f"pg-migration-check: {e}", file=sys.stderr)
            return 2
        if a.rollback and not a.json:
            print("\nRolled back: the database is as it was before the run (sequences excepted).")
    if a.json:
        print(json.dumps(all_out, indent=2))
    threshold = {"danger": 2, "warn": 1, "never": 99}[a.fail_on]
    return 1 if worst >= threshold else 0
