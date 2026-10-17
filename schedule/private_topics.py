"""日程到点只唤醒私聊 planner；是否说话由 planner 决定。"""

from datetime import date
from pathlib import Path
from typing import Any

import asyncio
import logging
import sqlite3

from .store import Segment, SegmentState


_logger = logging.getLogger("a_day_with_mittes.private_topics")


def parse_users(users: list[str]) -> list[tuple[str, str]]:
    """名单显式包含平台，不把群号或其他会话当成私聊目标。"""
    if not isinstance(users, list):
        raise ValueError("proactive_private.users 必须是 platform:user_id 字符串列表")
    targets = []
    for value in users:
        if not isinstance(value, str):
            raise ValueError("proactive_private.users 的条目必须是 platform:user_id 字符串")
        platform, separator, user_id = value.strip().partition(":")
        if not separator or not platform or not user_id or ":" in user_id:
            raise ValueError(f"无效的私聊主动名单条目：{value!r}，应为 platform:user_id")
        target = (platform, user_id)
        if target not in targets:
            targets.append(target)
    return targets


class PrivateTopicWaker:
    def __init__(self, ctx: Any, path: Path, initial: tuple[date, str]) -> None:
        self._ctx = ctx
        self._path = path
        # 冷启动不补发正在进行或已经过去的时段。
        self._initial = initial
        self._seen: set[tuple[str, str, str, str]] = set()

    def _claim(self, key: tuple[str, str, str, str], repeat: bool = False) -> bool:
        with sqlite3.connect(self._path) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS wakes (
                day TEXT, slot TEXT, platform TEXT, user_id TEXT,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (day, slot, platform, user_id))""")
            if repeat:
                # 仅显式手动重测允许覆盖本次结果；之前的任务与日志仍保留。
                con.execute("DELETE FROM wakes WHERE day=? AND slot=? AND platform=? AND user_id=?", key)
            return con.execute(
                "INSERT OR IGNORE INTO wakes (day,slot,platform,user_id,status) VALUES (?,?,?,?,'claimed')",
                key,
            ).rowcount == 1

    def _finish(self, key: tuple[str, str, str, str], status: str, detail: str) -> None:
        with sqlite3.connect(self._path) as con:
            con.execute(
                "UPDATE wakes SET status=?,detail=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE day=? AND slot=? AND platform=? AND user_id=?",
                (status, detail, *key),
            )

    async def tick(
        self, day: date, segment: Segment, state: SegmentState, users: list[str], *,
        include_initial: bool = False, repeat: bool = False,
    ) -> None:
        if ((day, segment.slot) == self._initial and not include_initial) or not state.topic.strip():
            return
        for platform, user_id in parse_users(users):
            key = (day.isoformat(), segment.slot, platform, user_id)
            if key in self._seen and not repeat:
                continue
            # 先持久化再调用。RPC 超时或进程退出时不能确定是否已接受，因此不自动重试。
            if not await asyncio.to_thread(self._claim, key, repeat):
                self._seen.add(key)
                continue
            self._seen.add(key)
            try:
                session = await self._ctx.chat.open_session(
                    platform=platform, chat_type="private", user_id=user_id,
                )
                if not isinstance(session, dict) or session.get("success") is not True:
                    raise RuntimeError(f"打开私聊会话失败：{session!r}")
                stream_id = session.get("stream_id") or session.get("session_id")
                if not stream_id:
                    raise RuntimeError(f"私聊会话缺少 stream_id：{session!r}")
                task_id = "schedule-private-topic:" + ":".join(key)
                intent = (
                    "这是一次由日程触发的私聊主动聊天判断，对方没有因此发来新消息。\n\n"
                    f"日程：{day.isoformat()} {segment.slot}（日本时间），{segment.title}。\n\n"
                    f"事件背景：\n{state.topic.strip()}\n\n"
                    "请结合私聊关系、最近的聊天和对方的反应，判断：这件事是否让 Mittes 此刻有了"
                    "想找对方说点什么的念头？这个念头可能来自某个具体细节、一点尚未消散的情绪，"
                    "或这件事与对方的某种联系。\n\n"
                    "有这样的念头，且此刻适合联系，就选择 reply；否则可以不回复。\n\n"
                    "选择 reply 时，请先确定她为什么想找这个人，以及最想让对方知道哪一点。"
                    "事件背景供你判断和取材，经过与结局不需要一并交给 replyer。\n\n"
                    "reply_reference 遵循工具的参数说明：只同步支撑这次联系动机的必要客观事实、"
                    "Mittes 此刻的状态，以及核心表达态度。篇幅集中在选中的那一点，具体语言组织交给 replyer。"
                )
                result = await self._ctx.maisaka.proactive.trigger(
                    stream_id=stream_id, intent=intent, reason="schedule_private_topic",
                    metadata={
                        "background_tool_result": True, "tool_name": "schedule_private_topic",
                        "tool_success": True, "task_id": task_id,
                        "day": day.isoformat(), "slot": segment.slot,
                    },
                )
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise RuntimeError(f"planner 唤醒失败：{result!r}")
                await asyncio.to_thread(self._finish, key, "queued", str(result.get("task_id", task_id)))
                _logger.info("[私聊主动] planner 已接受 %s %s → %s:%s", *key)
            except Exception as exc:
                await asyncio.to_thread(self._finish, key, "failed", str(exc))
                _logger.exception("[私聊主动] 唤醒失败，不自动重复：%s", key)
