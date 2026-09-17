"""Core domain models: rules, findings, symbols, statuses."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


class Severity:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}
    LABEL = {HIGH: "高", MEDIUM: "中", LOW: "低"}


class Confidence:
    """How trustworthy a finding is. Low-confidence items are folded away."""

    HIGH = "high"   # structural evidence + reviewer agree
    MEDIUM = "medium"  # structural hit, needs a human glance
    LOW = "low"     # model guess only, excluded from health score


class Status:
    OPEN = "open"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    WONTFIX = "wontfix"
    RESOLVED = "resolved"


# Statuses that still count as active debt on the dashboard.
ACTIVE_STATUSES = (Status.OPEN, Status.CONFIRMED, Status.WONTFIX)
SCORED_STATUSES = (Status.OPEN, Status.CONFIRMED)


@dataclass
class Rule:
    id: str
    name: str
    description: str
    severity: str
    needs_review: bool = False   # candidates go through the L3 reviewer
    suggestion: str = ""


@dataclass
class Symbol:
    """A function/method discovered by the L1 indexer."""

    name: str
    qualname: str          # "ClassName.method" or module-level "func"
    kind: str              # "function" | "method"
    file: str              # repo-relative path
    lineno: int
    end_lineno: int
    decorators: list[str]
    snippet: str
    shape_digest: str      # normalized structural hash of the body
    n_lines: int
    n_body_stmts: int
    is_abstract: bool
    n_args: int = 0
    max_depth: int = 0     # deepest control-flow nesting inside the body


@dataclass
class Finding:
    rule_id: str
    file: str
    line: int
    message: str
    severity: str
    confidence: str = Confidence.MEDIUM
    symbol: str = ""
    suggestion: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    # populated by the storage layer
    id: int | None = None
    status: str = Status.OPEN
    first_seen: str | None = None
    last_seen: str | None = None
    resolved_at: str | None = None
    note: str | None = None

    def dedup_key(self) -> str:
        """Stable identity across scans despite line drift."""
        basis = f"{self.rule_id}|{self.file}|{self.symbol}|{self.message[:120]}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class Snapshot:
    id: int | None
    scanned_at: str
    commit: str | None
    score: int
    total_open: int
    stats: dict[str, Any]
