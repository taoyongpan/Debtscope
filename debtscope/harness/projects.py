"""Project registry: monitored repositories and their per-project databases.

A project is a local repository path plus a managed SQLite database under
``~/.debtscope/data/``. The registry itself is a small JSON file; the storage
layer is unchanged because each project simply owns its own database.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime


def project_id_for(path: str) -> str:
    return "p_" + hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:10]


@dataclass
class Project:
    id: str
    name: str
    path: str
    created_at: str
    last_scan: str | None = None
    last_score: int | None = None
    last_open: int | None = None
    last_commit: str | None = None
    files: int = 0
    loc: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectRegistry:
    def __init__(self, base_dir: str | None = None):
        self.base = base_dir or os.path.join(os.path.expanduser("~"), ".debtscope")
        self.data_dir = os.path.join(self.base, "data")
        self.registry_path = os.path.join(self.base, "projects.json")
        os.makedirs(self.data_dir, exist_ok=True)

    # -- persistence --------------------------------------------------------

    def _load(self) -> list[dict]:
        if not os.path.isfile(self.registry_path):
            return []
        try:
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("projects", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, rows: list[dict]) -> None:
        tmp = self.registry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"projects": rows}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.registry_path)

    # -- queries ------------------------------------------------------------

    def list(self) -> list[Project]:
        return [Project(**r) for r in sorted(self._load(), key=lambda r: r.get("created_at", ""))]

    def get(self, pid: str) -> Project | None:
        row = next((r for r in self._load() if r["id"] == pid), None)
        return Project(**row) if row else None

    def get_by_path(self, path: str) -> Project | None:
        abspath = os.path.abspath(path)
        row = next((r for r in self._load() if os.path.abspath(r["path"]) == abspath), None)
        return Project(**row) if row else None

    def db_path(self, pid: str) -> str:
        return os.path.join(self.data_dir, f"{pid}.db")

    # -- mutations ----------------------------------------------------------

    def add(self, name: str, path: str) -> Project:
        abspath = os.path.abspath(os.path.expanduser(path))
        if not name.strip():
            name = os.path.basename(abspath.rstrip(os.sep)) or "project"
        if not os.path.isdir(abspath):
            raise ValueError(f"路径不存在或不是目录：{abspath}")
        if abspath in ("/", os.path.expanduser("~")):
            raise ValueError("为避免全盘扫描，不能直接监控根目录或用户主目录，请选择具体项目目录")
        existing = self.get_by_path(abspath)
        if existing:
            return existing
        pid = project_id_for(abspath)
        project = Project(
            id=pid, name=name.strip()[:64], path=abspath,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        rows = self._load()
        rows.append(project.to_dict())
        self._save(rows)
        return project

    def remove(self, pid: str, delete_data: bool = False) -> bool:
        rows = self._load()
        kept = [r for r in rows if r["id"] != pid]
        if len(kept) == len(rows):
            return False
        self._save(kept)
        if delete_data:
            db = self.db_path(pid)
            if os.path.isfile(db):
                os.remove(db)
        return True

    def update_summary(self, pid: str, summary: dict) -> None:
        rows = self._load()
        changed = False
        for r in rows:
            if r["id"] != pid:
                continue
            r["last_scan"] = datetime.now().isoformat(timespec="seconds")
            r["last_score"] = summary.get("score")
            r["last_open"] = summary.get("total_open")
            r["last_commit"] = summary.get("commit")
            r["files"] = summary.get("files", 0)
            r["loc"] = summary.get("loc", 0)
            changed = True
        if changed:
            self._save(rows)
