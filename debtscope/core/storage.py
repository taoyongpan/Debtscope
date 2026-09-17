"""SQLite persistence: findings ledger, snapshots, review log, feedback.

All history is kept: resolved findings are marked, never deleted, so the
dashboard can show both current debt and long-term trends.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

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

    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value", (key, value),
        )
        self.conn.commit()
