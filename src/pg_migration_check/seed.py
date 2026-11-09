"""Fill empty tables with synthetic rows, so lock times on a scratch database look like production's.

On an empty table every statement takes a few milliseconds, so a rewrite or an index build that would hold a lock
for seconds on a live table looks harmless. `seed` gives each table the migration names, and the tables those point at, N rows (or a count per table) made in one
INSERT ... SELECT from generate_series, with a value per column type. Row k of every table is the same expression
in k, so a foreign key can point at real parent rows: the child's value is the parent's expression for a k inside
the parent's range. Tables that already hold rows are left alone. A table that cannot be filled (a CHECK the values
break, a unique boolean, a type with no generator and no default) is skipped with the reason, never half filled.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

TABLES = """
select c.oid, quote_ident(n.nspname) || '.' || quote_ident(c.relname), n.nspname || '.' || c.relname, c.relname
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p') and not c.relispartition and c.relpersistence = 'p'
  and n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%'
  and n.nspname not like 'pg_temp%'
order by 3
"""
COLUMNS = """
select a.attnum, quote_ident(a.attname), format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity,
       a.attgenerated, d.adbin is not null, coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
       bt.typname, bt.typtype, bt.typcategory, coalesce(nullif(t.typtypmod, -1), a.atttypmod), bt.oid
from pg_attribute a
join pg_type t on t.oid = a.atttypid
join pg_type bt on bt.oid = case when t.typtype = 'd' then t.typbasetype else t.oid end
left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
where a.attrelid = %s and a.attnum > 0 and not a.attisdropped
order by a.attnum
"""
FOREIGN_KEYS = """
select conrelid, conkey, confrelid, confkey from pg_constraint where contype = 'f'
"""


@dataclass
class Seeded:
    table: str
    rows: int = 0
    ms: float = 0.0
    skipped: str = ""  # why it was not filled


@dataclass
class _Col:
    num: int
    name: str
    fmt: str
    not_null: bool
    identity: bool  # identity or serial: filled with k, then the sequence is moved past it
    generated: bool
    has_default: bool
    base: str
    typtype: str
    category: str
    typmod: int
    typoid: int


def value(c: _Col, k: str, enum_first: dict[int, str]) -> str | None:
    """SQL for this column's value in row k (an SQL expression), or None when there is no generator for its type."""
    k = f"({k})"
    t = c.base
    if c.identity or t in ("int4", "int8", "float4", "float8", "oid"):
        return f"{k}::{c.fmt}"
    if t == "int2":
        return f"({k} % 32767)::{c.fmt}"
    if t == "numeric":
        if c.typmod >= 4:  # numeric(p, s): stay inside the precision
            p, s = ((c.typmod - 4) >> 16) & 0xFFFF, (c.typmod - 4) & 0xFFFF
            return f"({k} % {10 ** max(p - s, 0) or 1})::{c.fmt}"
        return f"{k}::{c.fmt}"
    if t == "bool":
        return f"({k} % 2 = 0)::{c.fmt}"
    if t in ("text", "varchar", "bpchar", "name", "citext"):
        v = f"('v' || {k} || md5({k}::text))"
        n = c.typmod - 4 if t in ("varchar", "bpchar") and c.typmod > 4 else (63 if t == "name" else 0)
        return f"left({v}, {n})::{c.fmt}" if n else f"{v}::{c.fmt}"
    if t == "uuid":
        return f"md5({k}::text)::uuid::{c.fmt}"
    if t == "date":
        return f"(date '2000-01-01' + ({k} % 20000))::{c.fmt}"
    if t in ("timestamp", "timestamptz"):
        return f"(timestamp '2000-01-01' + {k} * interval '1 second')::{c.fmt}"
    if t in ("time", "timetz"):
        return f"(time '00:00' + ({k} % 86400) * interval '1 second')::{c.fmt}"
    if t == "interval":
        return f"({k} * interval '1 second')::{c.fmt}"
    if t in ("json", "jsonb"):
        return f"json_build_object('n', {k})::{c.fmt}"
    if t == "bytea":
        return f"decode(md5({k}::text), 'hex')::{c.fmt}"
    if t in ("inet", "cidr"):
        return f"('10.0.0.0'::inet + {k})::{c.fmt}"
    if c.typtype == "e" and c.typoid in enum_first:
        return f"{enum_first[c.typoid]}::{c.fmt}"
    if c.category == "A":
        return f"'{{}}'::{c.fmt}"
    return None


def _order(oids: list[int], parents: dict[int, set[int]]) -> list[int]:
    """Parents before children; a cycle is broken where it is met (those keys fall back to NULL or a skip)."""
    out, state = [], {}

    def visit(o: int) -> None:
        if state.get(o):
            return
        state[o] = 1
        for p in sorted(parents.get(o, ())):
            if p != o and p in oids_set:
                visit(p)
        state[o] = 2
        out.append(o)

    oids_set = set(oids)
    for o in oids:
        visit(o)
    return out


def parse_rows(specs: list[str]) -> tuple[int | None, dict[str, int]]:
    """`--rows 1000000` for every empty table, `--rows orders=5000000` for one (repeatable)."""
    every, per = None, {}
    for s in specs or []:
        name, _, n = s.rpartition("=")
        count = int(n.replace("_", "").replace(",", ""))
        if count < 0:
            raise ValueError(f"--rows {s}: a row count cannot be negative")
        if name:
            per[name] = count
        else:
            every = count
    return every, per


def mentioned(sql: str, rel: str) -> bool:
    return re.search(rf'(?<![\w$]){re.escape(rel)}(?![\w$])', sql, re.I) is not None


def seed(conn, every: int | None, per: dict[str, int] | None = None, sql: str | None = None) -> list[Seeded]:
    """Fill empty tables of the database behind `conn` (a SCRATCH database) and return one line per table.

    `every` rows go to the tables `sql` names (all tables when it is None) and to the tables they reference, so a
    foreign key has rows to point at; `per` sets a count for a table by name, wherever it is."""
    per = per or {}
    if not every and not per:
        return []
    conn.autocommit = True
    tables = {oid: (qname, name, rel) for oid, qname, name, rel in conn.execute(TABLES).fetchall()}
    fks: dict[int, list[tuple[list[int], int, list[int]]]] = {}
    for child, ckey, parent, pkey in conn.execute(FOREIGN_KEYS).fetchall():
        fks.setdefault(child, []).append((list(ckey), parent, list(pkey)))
    targets = {oid for oid, (_, _, rel) in tables.items() if sql is None or mentioned(sql, rel)}
    stack = list(targets)
    while stack:  # and everything they reference, all the way up
        for _, parent, _ in fks.get(stack.pop(), []):
            if parent in tables and parent not in targets:
                targets.add(parent)
                stack.append(parent)
    want = {}
    for oid, (_, name, rel) in tables.items():
        n = per.get(name, per.get(rel, every if oid in targets else None))
        if n:
            want[oid] = n
    enum_first = {oid: conn.execute("select quote_literal(enumlabel) from pg_enum where enumtypid = %s "
                                    "order by enumsortorder limit 1", (oid,)).fetchone()[0]
                  for (oid,) in conn.execute("select distinct enumtypid from pg_enum").fetchall()}

    cols = {oid: [_Col(*r[:4], bool(r[4]), bool(r[5]) and r[5] != "", r[6], r[8], r[9], r[10], r[11], r[12])
                  for r in conn.execute(COLUMNS, (oid,)).fetchall()] for oid in want}
    for oid in want:  # serial columns count as identity: they are filled with k and the sequence moved on
        for c in cols[oid]:
            if c.has_default and "nextval(" in conn.execute(
                    "select coalesce(pg_get_expr(adbin, adrelid), '') from pg_attrdef where adrelid = %s and adnum = %s",
                    (oid, c.num)).fetchone()[0]:
                c.identity = True

    done: dict[int, int] = {}  # table -> rows it now holds, all made by us
    made: dict[tuple[int, int], _Col] = {}  # (table, column number) -> the column, for values we generated
    out: list[Seeded] = []
    for oid in _order(list(want), {o: {p for _, p, _ in fks.get(o, [])} for o in want}):
        qname, name, _ = tables[oid]
        res = Seeded(name)
        out.append(res)
        if conn.execute(f"select exists (select from {qname})").fetchone()[0]:
            res.skipped = "already has rows"
            continue
        n = want[oid]
        by_col = {}
        for ckey, parent, pkey in fks.get(oid, []):
            for i, a in enumerate(ckey):
                by_col[a] = (parent, pkey[i], len(ckey))
        names, exprs, why = [], [], ""
        for c in cols[oid]:
            if c.generated:
                continue
            ref = by_col.get(c.num)
            if ref:
                parent, pnum, width = ref
                pcol = made.get((parent, pnum)) if (parent in done or parent == oid) and width == 1 else None
                if pcol is None:
                    if c.not_null:
                        why = f"{c.name} must reference a row in a table that was not filled"
                        break
                    continue  # NULL: no parent row to point at
                k = "g" if parent == oid else f"1 + (g - 1) % {done[parent]}"
                if parent == oid and not c.not_null:
                    continue  # a nullable self reference stays NULL
                v = value(pcol, k, enum_first)
            else:
                v = value(c, "g", enum_first)
            if v is None:
                if c.not_null and not c.has_default:
                    why = f"no generator for {c.name} ({c.fmt}) and it has no default"
                    break
                continue  # the default or NULL fills it
            names.append(c.name)
            exprs.append(v)
            if not ref:
                made[(oid, c.num)] = c
        if why:
            res.skipped = why
            continue
        overriding = " overriding system value" if any(c.identity and c.name in names for c in cols[oid]) else ""
        sql = (f"insert into {qname} ({', '.join(names)}){overriding} "
               f"select {', '.join(exprs)} from generate_series(1, {int(n)}) g" if names else
               f"insert into {qname} select from generate_series(1, {int(n)}) g")
        t = time.perf_counter()
        try:
            with conn.transaction():
                conn.execute(sql)
                for c in cols[oid]:
                    if c.identity and c.name in names:
                        conn.execute("select setval(pg_get_serial_sequence(%s, %s), %s)",
                                     (qname, c.name.strip('"').replace('""', '"'), n))
        except Exception as e:
            res.skipped = f"{type(e).__name__}: {str(e).strip().splitlines()[0]}"
            for key in [k for k in made if k[0] == oid]:
                del made[key]
            continue
        conn.execute(f"analyze {qname}")
        res.rows, res.ms = n, (time.perf_counter() - t) * 1000
        done[oid] = n
    return out


def report(seeded: list[Seeded], out) -> None:
    for s in seeded:
        if s.skipped:
            out.write(f"  not filled {s.table}: {s.skipped}\n")
        else:
            out.write(f"  filled {s.table} with {s.rows:,} rows in {s.ms / 1000:,.1f} s\n")
