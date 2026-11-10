"""独立的相机任务归档。面板以只读 SQLite 连接读取同一份记录。"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import json
import sqlite3
import uuid


DAILY_LIMIT = 3
CHAT_DAILY_LIMIT = 5  # 每个聊天（群）每天最多几张，Sweyn 的请求不计
FACE_DAILY_LIMIT = 2  # 别人帮拍的露脸照，每个提问人、每个聊天每天各最多几张（Sweyn 和本地预览不限）
QUOTA_TIMEZONE = ZoneInfo("Asia/Tokyo")
# 入队时暂占名额以限制并发；只有确认发送成功后才永久计入额度。
RESERVED_STATUSES = frozenset({
    "queued", "planning", "generating", "checking", "repairing", "ready", "sending", "sent",
})


class PhotoQuotaExceeded(ValueError):
    """用户当日额度已用完，不能再创建生图任务。"""

    def __init__(self, day: str, used: int, *, chat: bool = False) -> None:
        self.day = day
        self.used = used
        self.limit = CHAT_DAILY_LIMIT if chat else DAILY_LIMIT
        self.timezone = QUOTA_TIMEZONE.key
        whose = "这个聊天今天的" if chat else "当前"
        super().__init__(f"{whose}拍照名额已占满（{used}/{self.limit}，含正在拍摄的请求）；失败会释放名额，东京时间每天零点重置。")


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
                if quota.get("exempt") or quota.get("reset") or not quota.get("day"):
                    continue
                key = (str(requester.get("platform") or ""), str(requester.get("user_id") or ""), quota["day"])
                if all(key):
                    counts[key] = counts.get(key, 0) + 1
            con.execute("DELETE FROM daily_usage")
            con.executemany("INSERT INTO daily_usage VALUES (?,?,?,?)",
                            ((platform, user_id, day, used) for (platform, user_id, day), used in counts.items()))

    def reset_quota(self) -> None:
        """释放今天所有用户、聊天和露脸照额度，保留任务与照片归档。"""
        day = datetime.now(QUOTA_TIMEZONE).date().isoformat()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            for task_id, raw in con.execute("SELECT id, record FROM tasks").fetchall():
                record = json.loads(raw)
                quota = record.get("quota") or {}
                if quota.get("day") == day:
                    quota["reset"] = True
                    record["quota"] = quota
                    con.execute("UPDATE tasks SET record=? WHERE id=?",
                                (json.dumps(record, ensure_ascii=False), task_id))
            con.execute("DELETE FROM daily_usage WHERE day=?", (day,))

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
            exempt = record.get("preview") is True or record["requester_persona"] == "Sweyn"  # 店长不计额度
            quota = {"day": day, "timezone": QUOTA_TIMEZONE.key, "limit": DAILY_LIMIT, "exempt": exempt}
            if not exempt:
                row = con.execute("SELECT used FROM daily_usage WHERE platform=? AND user_id=? AND day=?",
                                  (*account, day)).fetchone()
                used = row[0] if row else 0
                if used >= DAILY_LIMIT:
                    raise PhotoQuotaExceeded(day, used)
                chat_used = sum(1 for other in self._today_in_stream(con, record["stream_id"], day) if not (other.get("quota") or {}).get("exempt") and not (other.get("quota") or {}).get("reset"))
                if chat_used >= CHAT_DAILY_LIMIT:
                    raise PhotoQuotaExceeded(day, chat_used, chat=True)
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
            previous = json.loads(previous_raw)
            # 工作中的任务可能持有重置前的副本，保存时保留数据库中的重置标记。
            if (previous.get("quota") or {}).get("reset"):
                record["quota"]["reset"] = True
            if previous_status in RESERVED_STATUSES and record["status"] not in RESERVED_STATUSES:
                previous = json.loads(previous_raw)
                quota = previous.get("quota") or {}
                requester = previous.get("requester") or {}
                if not quota.get("exempt") and not quota.get("reset") and quota.get("day"):
                    con.execute("""UPDATE daily_usage SET used=used-1
                        WHERE platform=? AND user_id=? AND day=? AND used>0""",
                        (requester.get("platform"), requester.get("user_id"), quota["day"]))
            elif previous_status not in RESERVED_STATUSES and record["status"] in RESERVED_STATUSES:
                raise ValueError("已结束的拍照任务不能重新占用额度")
            con.execute("UPDATE tasks SET status=?,updated_at=?,record=? WHERE id=?",
                        (record["status"], record["updated_at"], json.dumps(record, ensure_ascii=False), record["id"]))

    def get(self, task_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("拍照任务不存在")
        return json.loads(row[0])

    def next_queued(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def latest_sent(self, stream_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent' "
                              "ORDER BY updated_at DESC LIMIT 1", (stream_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _today_in_stream(con: sqlite3.Connection, stream_id: str, day: str) -> list[dict[str, Any]]:
        """同一聊天里今天（东京时间）占着名额的任务（进行中的也算）。"""
        placeholders = ",".join("?" * len(RESERVED_STATUSES))
        rows = con.execute(f"SELECT record FROM tasks WHERE stream_id=? AND status IN ({placeholders})",
                           (stream_id, *sorted(RESERVED_STATUSES))).fetchall()
        records = [json.loads(raw) for (raw,) in rows]
        return [r for r in records if (r.get("quota") or {}).get("day") == day]

    def face_photos_today(self, record: dict[str, Any]) -> int:
        """今天（东京时间）已占用的别人帮拍露脸照张数，进行中的也算：同一提问人与同一聊天两者取多。"""
        requester = record["requester"] or {}
        day = datetime.now(QUOTA_TIMEZONE).date().isoformat()
        since = (datetime.now(QUOTA_TIMEZONE) - timedelta(days=2)).astimezone(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT id, status, record FROM tasks WHERE created_at >= ?", (since,)).fetchall()
        by_person = by_chat = 0
        for task_id, status, raw in rows:
            other = json.loads(raw)
            if (other.get("quota") or {}).get("reset"):
                continue
            if task_id == record["id"] or status not in RESERVED_STATUSES or not other.get("share"):
                continue
            if datetime.fromisoformat(other["created_at"]).astimezone(QUOTA_TIMEZONE).date().isoformat() != day:
                continue
            by_person += (other.get("requester") or {}) == requester
            by_chat += other["stream_id"] == record["stream_id"]
        return max(by_person, by_chat)

    def earlier_photos(self, record: dict[str, Any], days: int = 7) -> list[dict[str, Any]]:
        """最近几天已发出、没露脸的照片（别人帮拍的和露脸自拍不算），给「之前那张」类请求挑选。
        本地试拍时也列出试拍成品，方便实测。"""
        now = datetime.fromisoformat(record["snapshot"]["captured_at"])
        statuses = ("sent", "preview") if record["preview"] else ("sent",)
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute(f"SELECT id, record FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) "
                               "ORDER BY created_at DESC LIMIT 400", statuses).fetchall()
        out: list[dict[str, Any]] = []
        for task_id, raw in rows:
            other = json.loads(raw)
            plan = other.get("plan") or {}
            if task_id == record["id"] or other.get("share") or other.get("reuse") or not other.get("final_image"):
                continue
            if (plan.get("face_cover") or {}).get("method") == "shown":
                continue
            taken = datetime.fromisoformat(other["snapshot"]["captured_at"])
            if not timedelta(0) <= now - taken <= timedelta(days=days):
                continue
            out.append({"id": task_id, "taken_at": taken.astimezone(QUOTA_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                        "place": other["snapshot"].get("place", ""), "title": other["snapshot"].get("title", ""),
                        "subject": plan.get("subject", ""), "description": other.get("description", "")})
        return out

    def recent_face_covers(self, limit: int = 6) -> list[str]:
        """最近几张已发出的人物照用过的遮脸方式（从新到旧），给取景分析器轮换用。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE status IN ('sent','preview') ORDER BY updated_at DESC LIMIT 40").fetchall()
        out: list[str] = []
        for (raw,) in rows:
            method = ((json.loads(raw).get("plan") or {}).get("face_cover") or {}).get("method")
            if method and method != "none":
                out.append(method)
            if len(out) >= limit:
                break
        return out

    def recent_candids(self, limit: int = 4) -> list[str]:
        """最近几张分享照用过的抓拍方式（从新到旧），给取景分析器轮换用。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE status IN ('sent','preview') ORDER BY updated_at DESC LIMIT 40").fetchall()
        shares = [json.loads(raw).get("share") or {} for (raw,) in rows]
        return [s["candid"] for s in shares if "candid" in s][:limit]

    def in_progress(self, stream_id: str) -> list[dict[str, Any]]:
        """这个会话里已接下、还没有结果的拍照任务，按提交先后排列。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status IN "
                               "('queued','planning','generating','checking','repairing','ready','sending') "
                               "ORDER BY created_at", (stream_id,)).fetchall()
        return [json.loads(raw) for (raw,) in rows]

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
