"""每日分享的归档：一天一行，记录选题、配图任务、配文、审核与发布。管理页以只读连接读取。"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import sqlite3


# 进行中：后台循环会接着推进；pending_review 等面板审核；approved 等到点发布。
WORKING = frozenset({"picking", "shooting", "captioning", "publishing"})
OPEN = WORKING | {"pending_review", "approved"}
# 结束且允许重新生成的状态。published 与 publish_unknown（可能已发出）不允许。
RETRYABLE = frozenset({"rejected", "failed", "skipped", "publish_failed"})


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class DailyShareStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "daily_share.db"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS posts (
                day TEXT PRIMARY KEY, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, record TEXT NOT NULL)""")

    def get(self, day: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM posts WHERE day=?", (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def start(self, day: str, reason: str) -> dict[str, Any]:
        """新建或重来一天的分享。进行中、已批准或可能已发出的不能重来；待审核的重来时旧版记为不通过。"""
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT record FROM posts WHERE day=?", (day,)).fetchone()
            previous = json.loads(row[0]) if row else None
            history: list[dict[str, Any]] = []
            if previous is not None:
                if previous["status"] not in RETRYABLE and previous["status"] != "pending_review":
                    raise ValueError(f"这一天的分享状态是 {previous['status']}，不能重新生成")
                history = previous.pop("history", [])
                if previous["status"] == "pending_review":
                    previous["status"] = "rejected"
                history.append({key: previous.get(key) for key in (
                    "attempt", "status", "pick", "camera_task_id", "photo", "caption", "error", "reason")})
            record = {
                "day": day, "status": "picking", "attempt": len(history) + 1, "reason": reason,
                "created_at": timestamp(), "updated_at": timestamp(),
                "pick": None, "moment": "", "camera_task_id": "", "photo": None,
                "caption": None, "note": "", "publish_at": "", "published": None, "error": "",
                "events": [{"at": timestamp(), "stage": "picking", "text": reason}], "history": history,
            }
            con.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?)",
                        (day, record["status"], record["created_at"], record["updated_at"],
                         json.dumps(record, ensure_ascii=False)))
        return record

    def save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = timestamp()
        with closing(sqlite3.connect(self.db)) as con, con:
            changed = con.execute("UPDATE posts SET status=?, updated_at=?, record=? WHERE day=?",
                                  (record["status"], record["updated_at"],
                                   json.dumps(record, ensure_ascii=False), record["day"])).rowcount
        if not changed:
            raise ValueError("每日分享记录不存在")

    def transition(self, day: str, allowed: set[str] | frozenset[str], status: str, text: str,
                   **fields: Any) -> dict[str, Any]:
        """面板操作与发布占位：只在当前状态允许时改状态，读改写在一个写事务里，避免互相覆盖。"""
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT record FROM posts WHERE day=?", (day,)).fetchone()
            if row is None:
                raise ValueError("这一天还没有分享")
            record = json.loads(row[0])
            if record["status"] not in allowed:
                raise ValueError(f"这一天的分享状态是 {record['status']}，不能这样操作")
            record.update(fields)
            record["status"] = status
            record["updated_at"] = timestamp()
            record["events"].append({"at": record["updated_at"], "stage": status, "text": text})
            con.execute("UPDATE posts SET status=?, updated_at=?, record=? WHERE day=?",
                        (status, record["updated_at"], json.dumps(record, ensure_ascii=False), day))
        return record

    def active(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(OPEN))
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute(f"SELECT record FROM posts WHERE status IN ({placeholders}) ORDER BY day",
                               sorted(OPEN)).fetchall()
        return [json.loads(raw) for (raw,) in rows]

    def recent(self, before: str, limit: int = 7) -> list[dict[str, Any]]:
        """某天之前最近几条有配图和配文的分享，给选题和配文避开重复。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM posts WHERE day < ? ORDER BY day DESC LIMIT ?",
                               (before, limit)).fetchall()
        out = []
        for (raw,) in rows:
            record = json.loads(raw)
            if record.get("pick") and record.get("caption"):
                out.append(record)
        return out

    def recover(self) -> None:
        """发布中断时说说可能已经发出，不自动重发；其他进行中的步骤没有外部副作用，照常续跑。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM posts WHERE status='publishing'").fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            record["status"] = "publish_unknown"
            record["events"].append({"at": timestamp(), "stage": "publish_unknown",
                                     "text": "发布时插件重载或中断，说说可能已发出，请先核对空间；未自动重发"})
            self.save(record)
