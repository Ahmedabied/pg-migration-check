"""Run a migration against a scratch Postgres and report what each statement would do to a live table.

Each statement runs in its own transaction. Before it commits we read, from Postgres itself:
- the locks it holds (pg_locks for our own backend), and so what they block;
- whether a table or index was rewritten (its relfilenode changed);
- how long it took, which is how long those locks were held.
A few things the locks cannot show (a rename that breaks running code, no lock_timeout) are found in the SQL text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# What each table lock mode blocks for other sessions, in plain words.
LOCKS = {
    "AccessShareLock": (1, "ACCESS SHARE", "only ACCESS EXCLUSIVE"),
    "RowShareLock": (2, "ROW SHARE", "only EXCLUSIVE and ACCESS EXCLUSIVE"),
    "RowExclusiveLock": (3, "ROW EXCLUSIVE", "schema changes and index builds; reads and writes go on"),
    "ShareUpdateExclusiveLock": (4, "SHARE UPDATE EXCLUSIVE", "other schema changes and VACUUM; reads and writes go on"),
    "ShareLock": (5, "SHARE", "writes (INSERT, UPDATE, DELETE); reads go on"),
    "ShareRowExclusiveLock": (6, "SHARE ROW EXCLUSIVE", "writes; reads go on"),
    "ExclusiveLock": (7, "EXCLUSIVE", "writes and SELECT FOR UPDATE; plain reads go on"),
    "AccessExclusiveLock": (8, "ACCESS EXCLUSIVE", "everything, reads included"),
}
BLOCKS_WRITES = 5

SNAPSHOT = """
select c.oid, n.nspname || '.' || c.relname, c.relkind, c.relfilenode, c.reltuples::bigint
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'i', 'm', 'p', 'I') and n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname not like 'pg_toast%' and n.nspname not like 'pg_temp%'
"""
MY_LOCKS = """
select l.relation, l.mode from pg_locks l
where l.pid = pg_backend_pid() and l.locktype = 'relation' and l.granted
"""


@dataclass
class Finding:
    level: str  # "danger", "warn", "info"
    text: str


@dataclass
class Result:
    n: int
    sql: str
    ms: float = 0.0
    locks: dict[str, str] = field(default_factory=dict)  # table -> strongest mode taken
    rows: dict[str, int] = field(default_factory=dict)
    rewritten: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    concurrently: bool = False


def split(sql: str) -> list[str]:
    """Split SQL into statements, respecting quotes, dollar quotes and comments."""
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            buf.append("\n")
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            buf.append(" ")
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c and (j + 1 >= n or sql[j + 1] != c):
                    break
                j += 2 if sql[j] == c else 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                buf.append(sql[i:j])
                i = j
                continue
        if c == ";":
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def text_rules(sql: str) -> list[Finding]:
    """Risks the lock view cannot show."""
    s = _norm(sql)
    f = []
    if re.search(r"\bRENAME\s+COLUMN\b|\bALTER\s+TABLE\b.*\bRENAME\s+(?!CONSTRAINT)\b(TO\b|\w+\s+TO\b)", s):
        f.append(Finding("danger", "rename: code still running with the old name breaks the moment this commits. "
                                   "Add the new name, deploy code that uses it, then drop the old one."))
    if re.search(r"\bDROP\s+COLUMN\b", s):
        f.append(Finding("warn", "drop column: deploy code that no longer reads or writes it first "
                                 "(Django: remove the field from the model in an earlier release)."))
    if re.search(r"\bSET\s+NOT\s+NULL\b", s):
        f.append(Finding("warn", "SET NOT NULL scans the whole table under ACCESS EXCLUSIVE unless a valid "
                                 "CHECK (col IS NOT NULL) exists. Add that check NOT VALID, VALIDATE it, then SET NOT NULL."))
    if re.search(r"\bADD\s+(CONSTRAINT\s+\S+\s+)?(FOREIGN\s+KEY|CHECK)\b", s) and "NOT VALID" not in s:
        f.append(Finding("warn", "a new FOREIGN KEY or CHECK is checked against every row while writes wait. "
                                 "Add it NOT VALID, then VALIDATE CONSTRAINT in a later step (that only blocks schema changes)."))
    if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", s) and "CONCURRENTLY" not in s:
        f.append(Finding("warn", "CREATE INDEX without CONCURRENTLY blocks writes for the whole build. "
                                 "Use CREATE INDEX CONCURRENTLY (Django: AddIndexConcurrently, atomic = False)."))
    return f


# A table or index name as written: maybe schema-qualified, maybe quoted.
NAME = r'((?:"[^"]+"|[\w$]+)(?:\s*\.\s*(?:"[^"]+"|[\w$]+))?)'


def is_concurrent(stmt: str) -> bool:
    """True for the statements CONCURRENTLY keeps out of a transaction block, told by their shape: the word in a
    column name or a string does not count, and REFRESH MATERIALIZED VIEW CONCURRENTLY runs in a transaction."""
    s = _norm(stmt)
    return bool(re.match(r"CREATE (UNIQUE )?INDEX CONCURRENTLY\b|DROP INDEX CONCURRENTLY\b"
                         r"|REINDEX (\([^)]*\) )?(INDEX|TABLE|SCHEMA|DATABASE|SYSTEM) CONCURRENTLY\b"
                         r"|REINDEX \( ?([^)]*, ?)?CONCURRENTLY( TRUE| ON| 1)? ?(,[^)]*)?\)", s)
                or re.match(r"ALTER TABLE .* DETACH PARTITION .* CONCURRENTLY$", s))


def _table_of(conn, name: str, index: bool) -> str:
    """schema.table for a name as written (for an index: the table it is on), read before the statement runs."""
    q = ("select n.nspname || '.' || t.relname from pg_class t join pg_namespace n on n.oid = t.relnamespace "
         + ("join pg_index i on i.indrelid = t.oid where i.indexrelid = to_regclass(%s)" if index
            else "where t.oid = to_regclass(%s)"))
    try:
        row = conn.execute(q, (name,)).fetchone()
    except Exception:
        row = None
    return row[0] if row else re.sub(r'\s|"', "", name)


def concurrent_locks(conn, stmt: str) -> dict[str, str]:
    """The table locks a CONCURRENTLY statement takes, from the Postgres docs: they are gone by the time it returns."""
    sue = "ShareUpdateExclusiveLock"
    m = re.match(r"\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + NAME + r"\s+DETACH\s+PARTITION\s+" + NAME, stmt, re.I)
    if m:  # the partition gets ACCESS EXCLUSIVE in the second of its two transactions
        return {_table_of(conn, m.group(1), False): sue, _table_of(conn, m.group(2), False): "AccessExclusiveLock"}
    m = re.search(r"\bON\s+(?:ONLY\s+)?" + NAME, stmt, re.I)
    if re.match(r"\s*CREATE\s", stmt, re.I) and m:
        return {_table_of(conn, m.group(1), False): sue}
    m = re.match(r"\s*(?:DROP\s+INDEX|REINDEX\s*(?:\([^)]*\)\s*)?(INDEX|TABLE|SCHEMA|DATABASE|SYSTEM))"
                 r"(?:\s+CONCURRENTLY)?(?:\s+IF\s+EXISTS)?\s+" + NAME, stmt, re.I)
    if m:
        kind = (m.group(1) or "INDEX").upper()
        return {_table_of(conn, m.group(2), kind == "INDEX") if kind in ("INDEX", "TABLE") else m.group(2): sue}
    return {"?": sue}


def _has_valid_not_null_check(conn, stmt: str) -> bool:
    """Postgres 12+ skips the SET NOT NULL scan when a validated CHECK (col IS NOT NULL) exists."""
    m = re.match(r'\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?("?[\w.]+"?)\s+ALTER\s+(?:COLUMN\s+)?("?\w+"?)\s+SET\s+NOT\s+NULL',
                 stmt, re.I)
    if not m:
        return False
    table, col = m.group(1), m.group(2).strip('"')
    try:
        row = conn.execute(
            "select count(*) from pg_constraint where conrelid = to_regclass(%s) and contype = 'c' and convalidated "
            "and regexp_replace(pg_get_constraintdef(oid), '[()\"]', '', 'g') ilike %s",
            (table, f"CHECK {col} IS NOT NULL")).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def check(conn, sql: str, slow_ms: float = 1000.0) -> list[Result]:
    """Apply `sql` to the database behind `conn` (a psycopg connection to a scratch database), one statement per
    transaction, and return what each statement did. Stops at the first error, like a migration would."""
    conn.autocommit = True
    results: list[Result] = []
    stmts = [s for s in split(sql) if not re.fullmatch(r"(BEGIN|COMMIT|START TRANSACTION|END)(\s+\w+)*", _norm(s))]
    live = {row[1] for row in conn.execute(SNAPSHOT).fetchall()}  # tables that exist before the file: live traffic
    for n, stmt in enumerate(stmts, 1):
        r = Result(n, stmt, findings=text_rules(stmt))
        s = _norm(stmt)
        m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?("?[\w.]+"?) ALTER (?:COLUMN )?("?\w+"?) SET NOT NULL$', s)
        if m and _has_valid_not_null_check(conn, stmt):
            r.findings = [f for f in r.findings if not f.text.startswith("SET NOT NULL")]
        if re.match(r"SET\s+(LOCAL\s+)?(LOCK_TIMEOUT|STATEMENT_TIMEOUT)\b", s):
            conn.execute(stmt)
            results.append(r)
            continue
        r.concurrently = is_concurrent(stmt)
        try:
            if r.concurrently:  # cannot run in a transaction block; its locks are released before we could read them
                locks = concurrent_locks(conn, stmt)
                t = time.perf_counter()
                conn.execute(stmt)
                r.ms = (time.perf_counter() - t) * 1000
                r.locks = locks
            else:
                with conn.transaction():
                    before = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                    t = time.perf_counter()
                    conn.execute(stmt)
                    r.ms = (time.perf_counter() - t) * 1000
                    held = conn.execute(MY_LOCKS).fetchall()
                    after = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                    names = {oid: row[1] for oid, row in {**before, **after}.items()}
                    for oid, mode in held:
                        if oid in names and after.get(oid, before.get(oid))[2] in ("r", "p", "m"):
                            name = names[oid]
                            if LOCKS.get(mode, (0,))[0] > LOCKS.get(r.locks.get(name, ""), (0,))[0]:
                                r.locks[name] = mode
                    for oid, old in before.items():
                        new = after.get(oid)
                        if new and new[3] != old[3] and old[2] in ("r", "m"):  # a new file: the table was copied
                            r.rewritten.append(old[1])
                    r.locks = {t: m for t, m in r.locks.items() if t in live}
                    r.rewritten = [t for t in r.rewritten if t in live]
                    r.rows = {names[o]: int(max(before[o][4], 0)) for o in before if names[o] in r.locks}
                    if not r.locks:
                        r.findings = []  # only touched tables made by this file: no live traffic to hurt
        except Exception as e:  # report and stop, as the migration would
            r.error = f"{type(e).__name__}: {str(e).strip().splitlines()[0]}"
            results.append(r)
            break
        existing = {t for t in r.locks if t in r.rows}
        for t, mode in r.locks.items():
            rank, label, blocks = LOCKS.get(mode, (0, mode, "?"))
            if t not in existing or rank < BLOCKS_WRITES:
                continue
            if rank == 8 and (r.ms >= slow_ms or t in r.rewritten):
                level = "danger"
            elif r.ms >= slow_ms / 10:
                level = "warn"
            else:
                level = "info"  # brief; the risk is waiting in the lock queue, which lock_timeout covers
            r.findings.insert(0, Finding(level, f"{label} on {t} ({r.rows.get(t, 0):,} rows) for {r.ms:,.0f} ms: blocks {blocks}"))
        for t in r.rewritten:
            r.findings.insert(0, Finding("danger", f"rewrites {t}: the whole table is copied while it is locked"))
        results.append(r)
    if stmts and "LOCK_TIMEOUT" not in _norm(sql) and any(LOCKS.get(m, (0,))[0] >= BLOCKS_WRITES for r in results for m in r.locks.values()):
        results.append(Result(0, "", findings=[Finding("warn", "no lock_timeout: if a long query holds the table, this "
            "migration waits for it, and every query that arrives after waits behind the migration. Start with "
            "SET lock_timeout = '2s' and retry on timeout.")]))
    return results


def held_until_commit(results: list[Result]) -> dict[str, float]:
    """If the whole migration runs in one transaction (Django's default), a lock taken by statement i is held until
    the end: the sum of the durations from i on. Returns table -> ms for locks that block writes."""
    out: dict[str, float] = {}
    if any(r.concurrently for r in results):  # such a file cannot run in one transaction
        return out
    total_after = 0.0
    for r in reversed([r for r in results if r.n]):
        total_after += r.ms
        for t, mode in r.locks.items():
            if LOCKS.get(mode, (0,))[0] >= BLOCKS_WRITES and t in r.rows and not r.concurrently:
                out[t] = total_after
    return out
