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

import psycopg

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
    level: str  # "danger", "warn", "info", "note" (always shown, never fails a run)
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
    changed: dict[str, int] = field(default_factory=dict)  # table -> rows an UPDATE or DELETE changed
    outside: bool = False  # VACUUM: cannot run in a transaction block either


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
            # E'...' takes backslash escapes (E'\'' is one quote); other strings only double the quote
            escapes = c == "'" and i > 0 and sql[i - 1] in "Ee" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c and (j + 1 >= n or sql[j + 1] != c):
                    break
                j += 2 if sql[j] == c else 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$", sql[i:])
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


def text_rules(sql: str, server_version: int = 0) -> list[Finding]:
    """Risks the lock view cannot show. server_version as psycopg gives it (180001 for 18.1); 0 if unknown."""
    s = _norm(sql)
    f = []
    if re.search(r"\bRENAME\s+COLUMN\b|\bALTER\s+TABLE\b.*\bRENAME\s+(?!CONSTRAINT)\b(TO\b|\w+\s+TO\b)", s):
        f.append(Finding("danger", "rename: code still running with the old name breaks the moment this commits. "
                                   "Add the new name, deploy code that uses it, then drop the old one."))
    if re.search(r"\bALTER\s+TYPE\b.*\bRENAME\s+VALUE\b", s):
        f.append(Finding("danger", "renaming an enum value: rows keep their value under the new label, so code still "
                                   "writing the old one fails (invalid input value for enum) the moment this commits. "
                                   "Add the new value, deploy code that writes it, move the rows in batches, and keep "
                                   "the old label until nothing uses it."))
    if re.match(r"DROP\s+TABLE\b", s):
        f.append(Finding("warn", "drop table: deploy code that no longer reads or writes it first."))
    if re.search(r"\bDROP\s+COLUMN\b", s):
        f.append(Finding("warn", "drop column: deploy code that no longer reads or writes it first "
                                 "(Django: remove the field from the model in an earlier release)."))
    if re.search(r"\bSET\s+NOT\s+NULL\b", s):
        f.append(Finding("warn", "SET NOT NULL scans the whole table under ACCESS EXCLUSIVE. On Postgres 18, add it as "
                                 "a constraint instead: ADD CONSTRAINT name NOT NULL col NOT VALID, then VALIDATE "
                                 "CONSTRAINT in a later step (that only blocks schema changes)." if server_version >= 180000
                         else "SET NOT NULL scans the whole table under ACCESS EXCLUSIVE unless a valid "
                              "CHECK (col IS NOT NULL) exists. Add that check NOT VALID, VALIDATE it, then SET NOT NULL."))
    if re.search(r"\bADD\s+(CONSTRAINT\s+\S+\s+)?NOT\s+NULL\b", s) and "NOT VALID" not in s:
        f.append(Finding("warn", "a new NOT NULL constraint is checked against every row under ACCESS EXCLUSIVE, so "
                                 "reads wait too. Add it NOT VALID, then VALIDATE CONSTRAINT in a later step."))
    if re.search(r"\bADD\s+(CONSTRAINT\s+\S+\s+)?(FOREIGN\s+KEY|CHECK)\b", s) and "NOT VALID" not in s:
        f.append(Finding("warn", "a new FOREIGN KEY or CHECK is checked against every row while writes wait. "
                                 "Add it NOT VALID, then VALIDATE CONSTRAINT in a later step (that only blocks schema changes)."))
    if re.search(r"\bADD\s+(CONSTRAINT\s+\S+\s+)?(PRIMARY\s+KEY|UNIQUE)\b", s) and "USING INDEX" not in s:
        f.append(Finding("warn", "a new PRIMARY KEY or UNIQUE constraint builds its index under ACCESS EXCLUSIVE, so reads "
                                 "and writes wait for the whole build. Build the index first with CREATE UNIQUE INDEX "
                                 "CONCURRENTLY, then ADD CONSTRAINT name PRIMARY KEY (or UNIQUE) USING INDEX name. A "
                                 "primary key also needs its columns NOT NULL: make them so the safe way first."))
    if re.match(r"REINDEX\b", s) and not is_concurrent(sql):
        f.append(Finding("warn", "REINDEX without CONCURRENTLY blocks writes to the table, and reads that use the index, "
                                 "for the whole rebuild. Use REINDEX INDEX CONCURRENTLY (Postgres 12 and newer)."))
    if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", s) and "CONCURRENTLY" not in s:
        f.append(Finding("warn", "CREATE INDEX without CONCURRENTLY blocks writes for the whole build. "
                                 "Use CREATE INDEX CONCURRENTLY (Django: AddIndexConcurrently, atomic = False)."))
    return f


# CREATE INDEX IF NOT EXISTS on a name that is taken. Seen on a schema file meant to be run on every deploy: behind
# one open transaction it waited, and the application's inserts waited behind it.
ALREADY_THERE = ("the index already exists, so nothing was built, but CREATE INDEX IF NOT EXISTS takes its lock before "
                 "it looks. Every run waits for any open transaction that wrote to the table, and writes queue behind "
                 "it. To take no lock when there is nothing to do, look first: DO $$ BEGIN IF to_regclass('index_name') "
                 "IS NULL THEN CREATE INDEX ...; END IF; END $$.")

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


# Changing this many rows in one transaction is a backfill that wants batches, however fast the scratch database is.
BATCH_ROWS = 10_000
DML = re.compile(r"\s*(UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?" + NAME, re.I)
NOT_IN_BLOCK = ("Postgres refuses this inside a transaction block, and this file runs it between BEGIN and COMMIT, so "
                "the migration fails when it gets here (cannot run inside a transaction block). Run it outside the block "
                "(Django: atomic = False on the migration).")


def _vacuum(conn, stmt: str, live_tables: set[str]) -> tuple[bool, list[str]]:
    """(FULL or not, the tables it works on) for a VACUUM, read from its text; no table named means all of them."""
    m = re.match(r"\s*VACUUM\s*(?:\((?P<opts>[^)]*)\))?(?P<words>(?:\s*\b(?:FULL|FREEZE|VERBOSE|ANALYZE|ANALYSE)\b)*)"
                 r"\s*(?P<rest>.*)$", stmt, re.I | re.S)
    if not m:
        return False, sorted(live_tables)
    full = bool(re.search(r"\bFULL\b(?!\s*(?:FALSE|OFF|0)\b)", m.group("opts") or "", re.I)
                or re.search(r"\bFULL\b", m.group("words") or "", re.I))
    names = [n.strip() for n in re.sub(r"\([^)]*\)", "", m.group("rest")).split(",") if n.strip()]
    return full, [_table_of(conn, n, False) for n in names] or sorted(live_tables)


def _type_oid(conn, name: str) -> int | None:
    try:
        with conn.transaction():  # a savepoint under --rollback
            row = conn.execute("select to_regtype(%s)::oid", (name,)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def _table_of(conn, name: str, index: bool) -> str:
    """schema.table for a name as written (for an index: the table it is on), read before the statement runs."""
    q = ("select n.nspname || '.' || t.relname from pg_class t join pg_namespace n on n.oid = t.relnamespace "
         + ("join pg_index i on i.indrelid = t.oid where i.indexrelid = to_regclass(%s)" if index
            else "where t.oid = to_regclass(%s)"))
    try:
        with conn.transaction():  # a savepoint under --rollback: a failed lookup must not end the run's transaction
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


def _set_not_null_skips_scan(conn, stmt: str) -> bool:
    """SET NOT NULL reads no rows when the column is NOT NULL already, or (Postgres 12+) when a validated
    CHECK (col IS NOT NULL) exists. On Postgres 18 attnotnull is also true under a NOT VALID not-null constraint,
    which SET NOT NULL then validates by reading every row, so that case still counts as a scan."""
    m = re.match(r'\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?("?[\w.]+"?)\s+ALTER\s+(?:COLUMN\s+)?("?\w+"?)\s+SET\s+NOT\s+NULL',
                 stmt, re.I)
    if not m:
        return False
    table, col = m.group(1), m.group(2)
    col = col.strip('"') if col.startswith('"') else col.lower()  # unquoted names are folded to lower case
    try:
        with conn.transaction():  # a savepoint under --rollback: a failed lookup must not end the run's transaction
            row = conn.execute(
                "select count(*) from pg_constraint where conrelid = to_regclass(%s) and contype = 'c' and convalidated "
                "and regexp_replace(pg_get_constraintdef(oid), '[()\"]', '', 'g') ilike %s",
                (table, f"CHECK {col} IS NOT NULL")).fetchone()
            if row and row[0]:
                return True
            row = conn.execute(
                "select a.attnotnull and not exists (select from pg_constraint c where c.conrelid = a.attrelid "
                "and c.contype = 'n' and c.conkey = array[a.attnum] and not c.convalidated) "
                "from pg_attribute a where a.attrelid = to_regclass(%s) and a.attname = %s and not a.attisdropped",
                (table, col)).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def plain(stmt: str) -> str:
    """The statement without CONCURRENTLY, so it can run inside a transaction (--rollback measures it that way)."""
    if re.match(r"\s*ALTER\s", stmt, re.I):  # ... DETACH PARTITION p CONCURRENTLY
        return re.sub(r"\s+CONCURRENTLY\s*$", "", stmt, flags=re.I)
    if re.match(r"\s*REINDEX\s*\([^)]*\bCONCURRENTLY\b", stmt, re.I):  # the option form: REINDEX (CONCURRENTLY) ...
        return re.sub(r"\bCONCURRENTLY(\s+(TRUE|ON|1))?\b", "CONCURRENTLY FALSE", stmt, count=1, flags=re.I)
    return re.sub(r"\bCONCURRENTLY\b", "", stmt, count=1, flags=re.I)


class TransactionEnded(RuntimeError):
    """Under --rollback the run's one transaction ended early: what ran after it was not rolled back."""


# Statements that would end the one transaction --rollback depends on.
ENDS_TRANSACTION = re.compile(r"(ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE TRANSACTION|COMMIT PREPARED|ROLLBACK PREPARED)\b")
WRAPPER = re.compile(r"(BEGIN|COMMIT|START TRANSACTION|END)(\s+\w+)*")


def _execute(conn, stmt: str) -> int:
    """Send one migration statement over the extended protocol, which refuses a string holding two commands, so a
    statement split() got wrong cannot carry a second one (a ROLLBACK under --rollback) past the checks. Asking for
    binary results is what selects that protocol: the simple one cannot return them."""
    return conn.execute(stmt, prepare=False, binary=True).rowcount


def check(conn, sql: str, slow_ms: float = 1000.0, rollback: bool = False, one_transaction: bool = False) -> list[Result]:
    """Apply `sql` to the database behind `conn` (a psycopg connection to a scratch database), one statement per
    transaction, and return what each statement did. Stops at the first error, like a migration would.

    rollback=True: the caller has opened one transaction and rolls it back afterwards (cli --rollback). Each statement
    runs in a savepoint inside it and shows the locks it added; a lock an earlier statement took is still held and is
    not shown again. A CONCURRENTLY statement cannot run in a transaction, so its plain form is run to time it.

    one_transaction=True: the file runs in one transaction in production, as an atomic Django migration does (its
    SQL from collect_sql has no BEGIN and COMMIT around it), so a statement Postgres refuses there is a danger."""
    stmts, in_block, block = [], [], one_transaction
    for s in split(sql):
        if WRAPPER.fullmatch(_norm(s)):  # BEGIN ... COMMIT around the file: Django's sqlmigrate writes it
            block = _norm(s).startswith(("BEGIN", "START"))
            continue
        stmts.append(s)
        in_block.append(block)
    if rollback:
        ends = [s for s in stmts if ENDS_TRANSACTION.match(_norm(s))]
        if ends:
            raise ValueError(f"--rollback cannot run a file that ends or splits its transaction: {ends[0][:60]!r}")
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
            raise ValueError("rollback=True needs an open transaction (conn.transaction(force_rollback=True))")
    else:
        conn.autocommit = True
    results: list[Result] = []
    snapshot = conn.execute(SNAPSHOT).fetchall()
    live = {row[1] for row in snapshot}  # tables that exist before the file: live traffic
    live_oids = {row[0] for row in snapshot}  # the same tables, followed through a rename later in the file
    kind = {row[1]: row[2] for row in snapshot}
    live_tables = {name for name, k in kind.items() if k in ("r", "m")}
    live_types = {row[0] for row in conn.execute(
        "select t.oid from pg_type t join pg_namespace n on n.oid = t.typnamespace "
        "where n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%'").fetchall()}
    for n, stmt in enumerate(stmts, 1):
        r = Result(n, stmt, findings=text_rules(stmt, conn.info.server_version))
        s = _norm(stmt)
        m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?("?[\w.]+"?) ALTER (?:COLUMN )?("?\w+"?) SET NOT NULL$', s)
        if m and _set_not_null_skips_scan(conn, stmt):
            r.findings = [f for f in r.findings if not f.text.startswith("SET NOT NULL")]
        r.concurrently = is_concurrent(stmt)
        r.outside = bool(re.match(r"VACUUM\b", s))
        if in_block[n - 1] and (r.concurrently or r.outside):
            r.findings.append(Finding("danger", NOT_IN_BLOCK))
        try:
            if re.match(r"SET\s+(LOCAL\s+)?(LOCK_TIMEOUT|STATEMENT_TIMEOUT)\b", s):
                with conn.transaction():  # a plain SET outlives its transaction (or savepoint) and lasts the session
                    _execute(conn, stmt)
            elif r.outside:  # VACUUM cannot run in a transaction block; FULL rewrites what it names
                full, targets = _vacuum(conn, stmt, live_tables)
                r.locks = {t: "AccessExclusiveLock" if full else "ShareUpdateExclusiveLock" for t in targets if t in live}
                before = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                r.rows = {row[1]: int(max(row[4], 0)) for row in before.values() if row[1] in r.locks}
                if rollback:
                    r.rewritten = list(r.locks) if full else []
                    r.findings.append(Finding("note", "VACUUM cannot run inside the rollback transaction, so it was not "
                                                      "run" + ("; VACUUM FULL rewrites every table it works on" if full else "")))
                else:
                    t = time.perf_counter()
                    _execute(conn, stmt)
                    r.ms = (time.perf_counter() - t) * 1000
                    after = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                    r.rewritten = [old[1] for oid, old in before.items() if old[1] in r.locks and old[2] in ("r", "m")
                                   and oid in after and after[oid][3] != old[3]]
            elif r.concurrently and not rollback:  # cannot run in a transaction block; its locks are gone when it returns
                locks = concurrent_locks(conn, stmt)
                t = time.perf_counter()
                _execute(conn, stmt)
                r.ms = (time.perf_counter() - t) * 1000
                r.locks = locks
            else:
                declared = concurrent_locks(conn, stmt) if r.concurrently else None
                dml = DML.match(stmt)
                target = _table_of(conn, dml.group(2), False) if dml else None
                was_live_type = False
                mt = re.match(r"\s*ALTER\s+TYPE\s+" + NAME, stmt, re.I)
                if mt:
                    was_live_type = _type_oid(conn, mt.group(1)) in live_types
                with conn.transaction():  # a savepoint under --rollback
                    held_before = set(conn.execute(MY_LOCKS).fetchall())
                    before = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                    t = time.perf_counter()
                    changed = _execute(conn, plain(stmt) if r.concurrently else stmt)
                    r.ms = (time.perf_counter() - t) * 1000
                    held_all = conn.execute(MY_LOCKS).fetchall()
                    after = {row[0]: row for row in conn.execute(SNAPSHOT).fetchall()}
                    # the name before the statement: a table this renames is still the live one
                    names = {oid: row[1] for oid, row in {**after, **before}.items()}
                    renamed = {oid: row for oid, row in after.items() if oid in live_oids and row[1] not in live}
                    live |= {row[1] for row in renamed.values()}  # later statements name it by its new name
                    kind.update({row[1]: row[2] for row in renamed.values()})
                    live_tables |= {row[1] for row in renamed.values() if row[2] in ("r", "m")}
                    tables = {oid for oid in names if after.get(oid, before.get(oid))[2] in ("r", "p", "m")}
                    for oid, mode in held_all:
                        if oid in tables and (oid, mode) not in held_before:  # what this statement added
                            name = names[oid]
                            if LOCKS.get(mode, (0,))[0] > LOCKS.get(r.locks.get(name, ""), (0,))[0]:
                                r.locks[name] = mode
                    for oid, old in before.items():
                        new = after.get(oid)
                        if new and new[3] != old[3] and old[2] in ("r", "m"):  # a new file: the table was copied
                            r.rewritten.append(old[1])
                    if re.match(r"CREATE (UNIQUE )?INDEX IF NOT EXISTS\b", s) and not any(
                            oid not in before and row[2] in ("i", "I") for oid, row in after.items()):
                        # No new index came out of it: the name was there. Nothing was built, the lock was still taken.
                        r.findings = [f for f in r.findings if not f.text.startswith("CREATE INDEX without CONCURRENTLY")]
                        r.findings.append(Finding("warn", ALREADY_THERE))
                    r.locks = {t: m for t, m in r.locks.items() if t in live}
                    r.rewritten = [t for t in r.rewritten if t in live]
                    r.rows = {names[o]: int(max(before[o][4], 0)) for o in before if names[o] in r.locks}
                    if target in live and changed > 0:
                        r.changed[target] = changed
                    if not was_live_type and not any(names[oid] in live for oid, _ in held_all if oid in tables):
                        r.findings = []  # only touched tables (or a type) made by this file: no live traffic to hurt
                if declared is not None:  # report what the real CONCURRENTLY statement takes, not the plain form
                    r.locks, r.rows, r.rewritten = declared, {}, []
                    r.findings.append(Finding("note", "timed without CONCURRENTLY inside the rollback transaction; "
                        "the real statement blocks no reads or writes, and a CONCURRENTLY index build reads the table "
                        "twice, so expect it to take longer"))
        except Exception as e:  # report and stop, as the migration would
            r.error = f"{type(e).__name__}: {str(e).strip().splitlines()[0]}"
            results.append(r)
            break
        finally:  # checked on success and on error: an error can be what ended it
            if rollback and conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
                raise TransactionEnded(f"statement {n} ended the run's transaction, so what ran after it was not rolled "
                                       f"back: check the database ({stmt[:60]!r})")
        existing = {t for t in r.locks if t in r.rows}
        for t, mode in r.locks.items():
            rank, label, blocks = LOCKS.get(mode, (0, mode, "?"))
            if t not in existing or rank < BLOCKS_WRITES:
                continue
            if kind.get(t) == "m" and rank < 8:  # a materialized view takes no writes: only reads can be blocked
                continue
            if rank == 8 and (r.ms >= slow_ms or t in r.rewritten):
                level = "danger"
            elif r.ms >= slow_ms / 10:
                level = "warn"
            else:
                level = "info"  # brief; the risk is waiting in the lock queue, which lock_timeout covers
            r.findings.insert(0, Finding(level, f"{label} on {t} ({r.rows.get(t, 0):,} rows) for {r.ms:,.0f} ms: blocks {blocks}"))
        for t, rows in r.changed.items():
            level = "warn" if rows >= BATCH_ROWS or r.ms >= slow_ms / 10 else "info"
            verb = "UPDATE changes" if _norm(stmt).startswith("UPDATE") else "DELETE removes"
            r.findings.insert(0, Finding(level, f"{verb} {rows:,} rows of {t} in one transaction ({r.ms:,.0f} ms). "
                f"Each stays locked until it commits, so other writes to those rows wait, and the table carries "
                f"{rows:,} dead row versions until vacuum. Backfill in batches of a few thousand rows, each in its own "
                f"transaction, in a step of its own after the schema change."))
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
    if any(r.concurrently or r.outside for r in results):  # such a file cannot run in one transaction
        return out
    total_after = 0.0
    for r in reversed([r for r in results if r.n]):
        total_after += r.ms
        for t, mode in r.locks.items():
            if LOCKS.get(mode, (0,))[0] >= BLOCKS_WRITES and t in r.rows and not r.concurrently:
                out[t] = total_after
    return out
