"""SQLite persistence: findings ledger, snapshots, review log, feedback.

All history is kept: resolved findings are marked, never deleted, so the
dashboard can show both current debt and long-term trends.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from .callgraph import node_key
from .health import health_score
from .models import Finding, Snapshot, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE NOT NULL,
    rule_id TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    symbol TEXT DEFAULT '',
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    message TEXT NOT NULL,
    suggestion TEXT DEFAULT '',
    evidence TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL,
    commit_sha TEXT,
    score INTEGER NOT NULL,
    total_open INTEGER NOT NULL,
    new_count INTEGER DEFAULT 0,
    resolved_count INTEGER DEFAULT 0,
    stats TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    reviewer TEXT DEFAULT 'local',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    description TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    suggestion TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 1,
    needs_review INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    framework TEXT NOT NULL DEFAULT '',
    handler_file TEXT NOT NULL DEFAULT '',
    handler_qualname TEXT NOT NULL DEFAULT '',
    handler_line INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoint_findings (
    endpoint_id TEXT NOT NULL,
    finding_id INTEGER NOT NULL,
    PRIMARY KEY (endpoint_id, finding_id)
);

CREATE TABLE IF NOT EXISTS endpoint_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL,
    snapshot_id INTEGER,
    scanned_at TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 100,
    open_count INTEGER NOT NULL DEFAULT 0,
    high_count INTEGER NOT NULL DEFAULT 0,
    medium_count INTEGER NOT NULL DEFAULT 0,
    chain_depth INTEGER NOT NULL DEFAULT 0,
    node_count INTEGER NOT NULL DEFAULT 0,
    blast_radius INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_epsnap_ep ON endpoint_snapshots(endpoint_id);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- reconcile per scan -------------------------------------------------

    def reconcile(self, findings: list[Finding]) -> dict[str, int]:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.cursor()

        rows = cur.execute("SELECT * FROM findings").fetchall()
        active = {r["dedup_key"]: r for r in rows if r["status"] in
                  (Status.OPEN, Status.CONFIRMED, Status.WONTFIX)}
        dormant = {r["dedup_key"]: r for r in rows if r["status"] == Status.RESOLVED}
        false_positives = {r["dedup_key"] for r in rows
                           if r["status"] == Status.FALSE_POSITIVE}

        counts = {"new": 0, "existing": 0, "reopened": 0,
                  "resolved": 0, "suppressed_fp": 0}
        seen: set[str] = set()

        for f in findings:
            key = f.dedup_key()
            seen.add(key)
            if key in false_positives:
                counts["suppressed_fp"] += 1
                continue
            if key in active:
                r = active[key]
                cur.execute(
                    """UPDATE findings SET line=?, severity=?, confidence=?, message=?,
                       suggestion=?, evidence=?, last_seen=? WHERE id=?""",
                    (f.line, f.severity, f.confidence, f.message, f.suggestion,
                     json.dumps(f.evidence, ensure_ascii=False), now, r["id"]),
                )
                counts["existing"] += 1
            elif key in dormant:
                r = dormant[key]
                cur.execute(
                    """UPDATE findings SET status='open', resolved_at=NULL, line=?,
                       severity=?, confidence=?, message=?, suggestion=?, evidence=?,
                       last_seen=? WHERE id=?""",
                    (f.line, f.severity, f.confidence, f.message, f.suggestion,
                     json.dumps(f.evidence, ensure_ascii=False), now, r["id"]),
                )
                counts["reopened"] += 1
            else:
                cur.execute(
                    """INSERT INTO findings (dedup_key, rule_id, file, line, symbol,
                       severity, confidence, message, suggestion, evidence, status,
                       first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)""",
                    (key, f.rule_id, f.file, f.line, f.symbol, f.severity,
                     f.confidence, f.message, f.suggestion,
                     json.dumps(f.evidence, ensure_ascii=False), now, now),
                )
                counts["new"] += 1

        for key, r in active.items():
            if key not in seen:
                cur.execute(
                    "UPDATE findings SET status='resolved', resolved_at=? WHERE id=?",
                    (now, r["id"]),
                )
                counts["resolved"] += 1

        self.conn.commit()
        return counts

    # -- snapshots ----------------------------------------------------------

    def add_snapshot(self, commit: str | None, score: int, total_open: int,
                     new_count: int, resolved_count: int,
                     stats: dict) -> Snapshot:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            """INSERT INTO snapshots (scanned_at, commit_sha, score, total_open,
               new_count, resolved_count, stats)
               VALUES (?,?,?,?,?,?,?)""",
            (now, commit, score, total_open, new_count, resolved_count,
             json.dumps(stats, ensure_ascii=False)),
        )
        self.conn.commit()
        return Snapshot(cur.lastrowid, now, commit, score, total_open, stats)

    def list_snapshots(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["stats"] = json.loads(d["stats"])
            out.append(d)
        return out

    # -- queries ------------------------------------------------------------

    def list_findings(self, statuses: tuple[str, ...] | None = None,
                      rule_id: str | None = None, severity: str | None = None,
                      confidence: str | None = None, q: str | None = None,
                      file: str | None = None) -> list[dict]:
        sql = "SELECT * FROM findings WHERE 1=1"
        params: list = []
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        if rule_id:
            sql += " AND rule_id=?"; params.append(rule_id)
        if severity:
            sql += " AND severity=?"; params.append(severity)
        if confidence:
            sql += " AND confidence=?"; params.append(confidence)
        if file:
            sql += " AND file=?"; params.append(file)
        if q:
            sql += " AND (message LIKE ? OR file LIKE ? OR symbol LIKE ?)"
            params += [f"%{q}%"] * 3
        sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
        sql += " CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
        sql += " file, line"
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"] or "{}")
            out.append(d)
        return out

    def set_review(self, finding_id: int, status: str, note: str | None,
                   reviewer: str = "local") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT dedup_key, rule_id FROM findings WHERE id=?", (finding_id,)
        ).fetchone()
        if row is None:
            raise KeyError(finding_id)
        self.conn.execute(
            "UPDATE findings SET status=?, note=? WHERE id=?",
            (status, note, finding_id),
        )
        self.conn.execute(
            """INSERT INTO reviews (finding_id, status, note, reviewer, created_at)
               VALUES (?,?,?,?,?)""",
            (finding_id, status, note, reviewer, now),
        )
        if status == Status.FALSE_POSITIVE:
            self.conn.execute(
                "INSERT INTO feedback (dedup_key, rule_id, created_at) VALUES (?,?,?)",
                (row["dedup_key"], row["rule_id"], now),
            )
        self.conn.commit()

    # -- rules --------------------------------------------------------------

    def seed_rules(self) -> None:
        """Insert built-in rule defaults once; user edits are preserved."""
        from .rules import BUILTIN_SPECS
        now = datetime.now().isoformat(timespec="seconds")
        for spec in BUILTIN_SPECS:
            exists = self.conn.execute(
                "SELECT 1 FROM rules WHERE id=?", (spec.id,)
            ).fetchone()
            if exists:
                continue
            self.conn.execute(
                """INSERT INTO rules (id, name, kind, severity, description, params,
                   suggestion, enabled, builtin, needs_review, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (spec.id, spec.name, spec.kind, spec.severity, spec.description,
                 json.dumps(spec.params, ensure_ascii=False), spec.suggestion,
                 1 if spec.enabled else 0, 1 if spec.builtin else 0,
                 1 if spec.needs_review else 0, now),
            )
        self.conn.commit()

    def list_rules(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM rules ORDER BY builtin DESC, "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"] or "{}")
            d["enabled"] = bool(d["enabled"])
            d["builtin"] = bool(d["builtin"])
            d["needs_review"] = bool(d["needs_review"])
            out.append(d)
        return out

    def upsert_rule(self, spec) -> None:
        from .rules import RuleSpec
        if not isinstance(spec, RuleSpec):
            spec = RuleSpec(**spec)
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO rules (id, name, kind, severity, description, params,
               suggestion, enabled, builtin, needs_review, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
               severity=excluded.severity, description=excluded.description,
               params=excluded.params, suggestion=excluded.suggestion,
               enabled=excluded.enabled, needs_review=excluded.needs_review,
               updated_at=excluded.updated_at""",
            (spec.id, spec.name, spec.kind, spec.severity, spec.description,
             json.dumps(spec.params, ensure_ascii=False), spec.suggestion,
             1 if spec.enabled else 0, 1 if spec.builtin else 0,
             1 if spec.needs_review else 0, now),
        )
        self.conn.commit()

    def delete_rule(self, rule_id: str) -> bool:
        row = self.conn.execute(
            "DELETE FROM rules WHERE id=? AND builtin=0", (rule_id,)
        )
        self.conn.commit()
        return row.rowcount > 0

    def reset_rule(self, rule_id: str) -> bool:
        """Restore a built-in rule to its shipped default."""
        from .rules import BUILTIN_SPECS
        spec = next((s for s in BUILTIN_SPECS if s.id == rule_id), None)
        if spec is None:
            return False
        self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        self.conn.commit()
        self.seed_rules()
        return True

    # -- endpoints (interface radar) ----------------------------------------

    def reconcile_endpoints(self, endpoint_dicts: list[dict], chains: dict,
                            blast: dict, active_rows: list[dict],
                            snapshot_id: int) -> dict:
        """Replace endpoint inventory, remap findings onto chains, snapshot."""
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.cursor()

        # findings indexed by function node key and by file (file-level debt)
        func_map: dict[str, list[dict]] = defaultdict(list)
        file_map: dict[str, list[dict]] = defaultdict(list)
        for f in active_rows:
            if f.get("symbol"):
                func_map[node_key(f["file"], f["symbol"])].append(f)
            else:
                file_map[f["file"]].append(f)

        incoming = {e["id"] for e in endpoint_dicts}
        existing = {r["id"] for r in cur.execute("SELECT id FROM endpoints")}
        for gone in existing - incoming:
            cur.execute("DELETE FROM endpoint_findings WHERE endpoint_id=?", (gone,))
            cur.execute("DELETE FROM endpoints WHERE id=?", (gone,))

        for ep in endpoint_dicts:
            row = cur.execute(
                "SELECT first_seen FROM endpoints WHERE id=?", (ep["id"],)
            ).fetchone()
            first_seen = row["first_seen"] if row else now
            cur.execute(
                """INSERT INTO endpoints (id, method, path, framework, handler_file,
                   handler_qualname, handler_line, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET method=excluded.method,
                   path=excluded.path, framework=excluded.framework,
                   handler_file=excluded.handler_file,
                   handler_qualname=excluded.handler_qualname,
                   handler_line=excluded.handler_line, last_seen=excluded.last_seen""",
                (ep["id"], ep["method"], ep["path"], ep["framework"],
                 ep["handler_file"], ep["handler_qualname"], ep["handler_line"],
                 first_seen, now),
            )

        cur.execute("DELETE FROM endpoint_findings")
        stats: dict[str, dict] = {}
        for ep in endpoint_dicts:
            eid = ep["id"]
            chain = chains.get(eid)
            matched: dict[int, dict] = {}
            if chain is not None:
                touched_files: set[str] = set()
                for nk in chain.nodes:
                    touched_files.add(nk.split("::", 1)[0])
                    for f in func_map.get(nk, []):
                        matched[f["id"]] = f
                for rel in touched_files:
                    for f in file_map.get(rel, []):
                        matched[f["id"]] = f
            rows = list(matched.values())
            cur.executemany(
                "INSERT OR IGNORE INTO endpoint_findings(endpoint_id, finding_id)"
                " VALUES(?,?)",
                [(eid, f["id"]) for f in rows],
            )
            open_rows = [f for f in rows
                         if f["status"] in (Status.OPEN, Status.CONFIRMED)]
            high = sum(1 for f in open_rows if f["severity"] == "high")
            medium = sum(1 for f in open_rows if f["severity"] == "medium")
            score = health_score(rows)
            depth = chain.depth if chain is not None else 0
            node_count = chain.node_count if chain is not None else 0
            radius = max((blast.get(nk, 1) for nk in chain.nodes), default=0) \
                if chain is not None else 0
            cur.execute(
                """INSERT INTO endpoint_snapshots (endpoint_id, snapshot_id,
                   scanned_at, score, open_count, high_count, medium_count,
                   chain_depth, node_count, blast_radius)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (eid, snapshot_id, now, score, len(open_rows), high, medium,
                 depth, node_count, radius),
            )
            stats[eid] = {"score": score, "open": len(open_rows),
                          "high": high, "medium": medium, "depth": depth,
                          "nodes": node_count, "blast": radius}
        self.conn.commit()
        return {"endpoints": len(incoming), "with_chains": len(chains),
                "stats": stats}

    def list_endpoints(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT e.*, s.scanned_at AS snap_at, s.score AS score,
                      s.open_count AS open_count, s.high_count AS high_count,
                      s.medium_count AS medium_count, s.chain_depth AS chain_depth,
                      s.node_count AS node_count, s.blast_radius AS blast_radius,
                      p.score AS prev_score, p.open_count AS prev_open
               FROM endpoints e
               JOIN endpoint_snapshots s ON s.id = (
                   SELECT max(id) FROM endpoint_snapshots WHERE endpoint_id = e.id)
               LEFT JOIN endpoint_snapshots p ON p.id = (
                   SELECT max(id) FROM endpoint_snapshots
                   WHERE endpoint_id = e.id AND id < s.id)
               ORDER BY s.score ASC, s.open_count DESC, e.path ASC"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["score_delta"] = (d["score"] - d["prev_score"]) \
                if d["prev_score"] is not None else 0
            d["open_delta"] = (d["open_count"] - d["prev_open"]) \
                if d["prev_open"] is not None else 0
            out.append(d)
        return out

    def endpoint_detail(self, endpoint_id: str) -> dict | None:
        ep = self.conn.execute(
            "SELECT * FROM endpoints WHERE id=?", (endpoint_id,)
        ).fetchone()
        if ep is None:
            return None
        snapshots = [
            dict(r) for r in self.conn.execute(
                """SELECT id, snapshot_id, scanned_at, score, open_count,
                          high_count, medium_count, chain_depth, node_count,
                          blast_radius
                   FROM endpoint_snapshots WHERE endpoint_id=? ORDER BY id""",
                (endpoint_id,),
            ).fetchall()
        ]
        findings = []
        for r in self.conn.execute(
            """SELECT f.* FROM findings f
               JOIN endpoint_findings ef ON ef.finding_id = f.id
               WHERE ef.endpoint_id=?
                 AND f.status IN ('open','confirmed','wontfix')
               ORDER BY CASE f.status WHEN 'wontfix' THEN 1 ELSE 0 END,
                        CASE f.severity WHEN 'high' THEN 0
                             WHEN 'medium' THEN 1 ELSE 2 END,
                        f.file, f.line""",
            (endpoint_id,),
        ).fetchall():
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"] or "{}")
            findings.append(d)
        return {"endpoint": dict(ep), "snapshots": snapshots,
                "findings": findings}

    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value", (key, value),
        )
        self.conn.commit()
