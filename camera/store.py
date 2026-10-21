"""独立的相机任务归档。面板以只读 SQLite 连接读取同一份记录。"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import json
import sqlite3
import uuid


DAILY_LIMIT = 3
QUOTA_TIMEZONE = ZoneInfo("Asia/Tokyo")
EXEMPT_ACCOUNT = ("qq", "923856490")
# 入队时暂占名额以限制并发；只有确认发送成功后才永久计入额度。
RESERVED_STATUSES = frozenset({
    "queued", "planning", "generating", "checking", "repairing", "ready", "sending", "sent",
})


class PhotoQuotaExceeded(ValueError):
    """用户当日额度已用完，不能再创建生图任务。"""

    def __init__(self, day: str, used: int) -> None:
        self.day = day
        self.used = used
        self.limit = DAILY_LIMIT
        self.timezone = QUOTA_TIMEZONE.key
        super().__init__(f"当前拍照名额已占满（{used}/{DAILY_LIMIT}，含正在拍摄的请求）；失败会释放名额，东京时间每天零点重置。")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class CameraStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "camera.db"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, source_msg_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                record TEXT NOT NULL, UNIQUE(stream_id, source_msg_id))""")
            con.execute("CREATE INDEX IF NOT EXISTS task_status ON tasks(status, created_at)")
            con.execute("""CREATE TABLE IF NOT EXISTS daily_usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL,
                used INTEGER NOT NULL CHECK (used >= 0),
                PRIMARY KEY (platform, user_id, day))""")
            # 旧版本在失败后仍保留扣次。启动时按实际任务状态重建账本，
            # 同时修复上次进程在状态落盘与额度更新之间中断的情况。
            counts: dict[tuple[str, str, str], int] = {}
            for status, raw in con.execute("SELECT status, record FROM tasks"):
                if status not in RESERVED_STATUSES:
                    continue
                record = json.loads(raw)
                quota = record.get("quota") or {}
                requester = record.get("requester") or {}
                if quota.get("exempt") or not quota.get("day"):
                    continue
                key = (str(requester.get("platform") or ""), str(requester.get("user_id") or ""), quota["day"])
                if all(key):
                    counts[key] = counts.get(key, 0) + 1
            con.execute("DELETE FROM daily_usage")
            con.executemany("INSERT INTO daily_usage VALUES (?,?,?,?)",
                            ((platform, user_id, day, used) for (platform, user_id, day), used in counts.items()))

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, "id": uuid.uuid4().hex, "status": "queued",
                  "created_at": timestamp(), "updated_at": timestamp(), "events": []}
        with closing(sqlite3.connect(self.db)) as con, con:
            # 去重、额度检查、扣次和入队必须在同一写事务中完成，跨会话及并发不能绕过。
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT record FROM tasks WHERE stream_id=? AND source_msg_id=?",
                                   (record["stream_id"], record["source_msg_id"])).fetchone()
            if existing is not None:
                return json.loads(existing[0])
            day = datetime.now(QUOTA_TIMEZONE).date().isoformat()
            requester = record.get("requester") or {}
            account = (str(requester.get("platform") or ""), str(requester.get("user_id") or ""))
            if record.get("preview") is not True and not all(account):
                raise ValueError("缺少经真实消息验证的拍照用户身份")
            exempt = record.get("preview") is True or account == EXEMPT_ACCOUNT
            quota = {"day": day, "timezone": QUOTA_TIMEZONE.key, "limit": DAILY_LIMIT, "exempt": exempt}
            if not exempt:
                row = con.execute("SELECT used FROM daily_usage WHERE platform=? AND user_id=? AND day=?",
                                  (*account, day)).fetchone()
                used = row[0] if row else 0
                if used >= DAILY_LIMIT:
                    raise PhotoQuotaExceeded(day, used)
                con.execute("""INSERT INTO daily_usage VALUES (?,?,?,1)
                    ON CONFLICT(platform,user_id,day) DO UPDATE SET used=used+1""", (*account, day))
                quota.update(used=used + 1, remaining=DAILY_LIMIT - used - 1)
            record["quota"] = quota
            con.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (record["id"], record["stream_id"], record["source_msg_id"], record["status"],
                 record["created_at"], record["updated_at"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = timestamp()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status, record FROM tasks WHERE id=?", (record["id"],)).fetchone()
            if row is None:
                raise ValueError("拍照任务不存在")
            previous_status, previous_raw = row
            if previous_status in RESERVED_STATUSES and record["status"] not in RESERVED_STATUSES:
                previous = json.loads(previous_raw)
                quota = previous.get("quota") or {}
                requester = previous.get("requester") or {}
                if not quota.get("exempt") and quota.get("day"):
                    con.execute("""UPDATE daily_usage SET used=used-1
                        WHERE platform=? AND user_id=? AND day=? AND used>0""",
                        (requester.get("platform"), requester.get("user_id"), quota["day"]))
            elif previous_status not in RESERVED_STATUSES and record["status"] in RESERVED_STATUSES:
                raise ValueError("已结束的拍照任务不能重新占用额度")
            con.execute("UPDATE tasks SET status=?,updated_at=?,record=? WHERE id=?",
                        (record["status"], record["updated_at"], json.dumps(record, ensure_ascii=False), record["id"]))

    def next_queued(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def latest_sent(self, stream_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent' "
                              "ORDER BY updated_at DESC LIMIT 1", (stream_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self) -> None:
        """不重试结果未知的生成和发送；尚未开始的排队任务照常恢复。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE status IN "
                               "('planning','generating','checking','repairing','ready','sending')").fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            status = "delivery_unknown" if record["status"] == "sending" else "interrupted"
            record["events"].append({"at": timestamp(), "stage": status, "text": "插件重载或进程中断，未自动重试"})
            record["status"] = status
            self.save(record)
