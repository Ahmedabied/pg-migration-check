"""Command line: pg-migration-check MIGRATION.sql ... --dsn postgresql://.../scratch"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .check import LOCKS, check, held_until_commit

MARK = {"danger": "DANGER", "warn": "warn  ", "info": "info  "}


def report(path: str, results, out, verbose: bool = False, one_transaction: bool | None = None) -> None:
    """one_transaction: True when the file is known to run in one transaction, False when it is known not to."""
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
        out.write("  Django runs this migration in one transaction, so the locks are held until it commits:\n"
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
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="a SCRATCH database; it gets changed "
                   "(default: $DATABASE_URL)")
    p.add_argument("--slow-ms", type=float, default=1000, help="ACCESS EXCLUSIVE held this long is a danger (default 1000)")
    p.add_argument("--fail-on", choices=["danger", "warn", "never"], default="danger")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="also show brief locks (info)")
    a = p.parse_args(argv)
    if not a.dsn:
        p.error("give --dsn or set DATABASE_URL (a scratch database: the migration is applied)")
    import psycopg

    worst = 0
    rank = {"info": 0, "warn": 1, "danger": 2}
    all_out = []
    with psycopg.connect(a.dsn) as conn:
        for f in a.files:
            results = check(conn, Path(f).read_text(encoding="utf-8"), a.slow_ms)
            if a.json:
                all_out.append({"file": f, "statements": [
                    {"n": r.n, "sql": r.sql, "ms": round(r.ms, 1), "error": r.error, "rewritten": r.rewritten,
                     "locks": {t: LOCKS.get(m, (0, m))[1] for t, m in r.locks.items()}, "rows": r.rows,
                     "findings": [{"level": x.level, "text": x.text} for x in r.findings]} for r in results],
                    "held_until_commit_ms": {t: round(v, 1) for t, v in held_until_commit(results).items()}})
            else:
                report(f, results, sys.stdout, a.verbose)
            for r in results:
                worst = max([worst] + [rank[x.level] for x in r.findings] + ([2] if r.error else []))
    if a.json:
        print(json.dumps(all_out, indent=2))
    threshold = {"danger": 2, "warn": 1, "never": 99}[a.fail_on]
    return 1 if worst >= threshold else 0
