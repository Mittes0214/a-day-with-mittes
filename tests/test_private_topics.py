"""私聊日程唤醒回归验证；不向实际用户发消息、不调用模型。"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import importlib
import json
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
module = importlib.import_module("plugins.01_a_day_with_mittes.schedule.private_topics")
DAY = date(2026, 10, 17)


def segment(start="18:00", end="19:00"):
    return module.Segment(start, end, "回家", "家", "便服", "独自", "自由")


def state(topic="刚买的草莓大福在袋子里被压扁了。"):
    return module.SegmentState(story="原故事保持不变", manner="", mood="", topic=topic)


def context():
    return SimpleNamespace(
        chat=SimpleNamespace(open_session=AsyncMock(return_value={"success": True, "stream_id": "real-private"})),
        maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=AsyncMock(
            return_value={"success": True, "queued": True, "task_id": "host-task"},
        ))),
    )


def statuses(path):
    with sqlite3.connect(path) as con:
        return con.execute("SELECT day,slot,platform,user_id,status FROM wakes ORDER BY user_id").fetchall()


def test_only_allowed_private_targets_and_planner_gets_topic(tmp_path):
    async def scenario():
        ctx = context()
        waker = module.PrivateTopicWaker(ctx, tmp_path / "wakes.db", (DAY, "17:00-18:00"))
        current = state()
        await waker.tick(DAY, segment(), current, ["qq:923856490", "qq:923856490"])
        ctx.chat.open_session.assert_awaited_once_with(platform="qq", chat_type="private", user_id="923856490")
        args = ctx.maisaka.proactive.trigger.await_args.kwargs
        assert args["stream_id"] == "real-private"
        assert current.topic in args["intent"]
        assert "否则可以不回复" in args["intent"] and "reply_reference" in args["intent"]
        assert args["metadata"]["background_tool_result"] is True
        assert current.story == "原故事保持不变"
        assert statuses(tmp_path / "wakes.db")[0][-1] == "queued"
    asyncio.run(scenario())


def test_no_startup_catchup_no_empty_topic_and_empty_list(tmp_path):
    async def scenario():
        ctx = context()
        path = tmp_path / "wakes.db"
        waker = module.PrivateTopicWaker(ctx, path, (DAY, segment().slot))
        await waker.tick(DAY, segment(), state(), ["qq:923856490"])
        await waker.tick(DAY, segment("19:00", "20:00"), state(" \n"), ["qq:923856490"])
        await waker.tick(DAY, segment("19:00", "20:00"), state(), [])
        ctx.chat.open_session.assert_not_awaited()
        ctx.maisaka.proactive.trigger.assert_not_awaited()
        assert not path.exists()
    asyncio.run(scenario())


def test_once_per_slot_user_even_after_topic_edit_and_restart(tmp_path):
    async def scenario():
        ctx = context()
        path = tmp_path / "wakes.db"
        waker = module.PrivateTopicWaker(ctx, path, (DAY, "17:00-18:00"))
        await waker.tick(DAY, segment(), state(), ["qq:923856490"])
        await waker.tick(DAY, segment(), state("重跑后的话题"), ["qq:923856490"])
        # 磁盘记录也阻止重复，不能只靠内存去重。
        restarted = module.PrivateTopicWaker(ctx, path, (DAY, "17:00-18:00"))
        await restarted.tick(DAY, segment(), state(), ["qq:923856490"])
        assert ctx.maisaka.proactive.trigger.await_count == 1
        await restarted.tick(DAY, segment(), state(), ["qq:923856490", "telegram:1739875050"])
        await restarted.tick(DAY, segment("19:00", "20:00"), state(), ["qq:923856490"])
        assert ctx.maisaka.proactive.trigger.await_count == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["open", "rejected", "timeout"])
def test_failed_attempt_is_recorded_and_not_retried(tmp_path, failure):
    async def scenario():
        ctx = context()
        if failure == "open":
            ctx.chat.open_session.return_value = {"success": False, "error": "no session"}
        elif failure == "rejected":
            ctx.maisaka.proactive.trigger.return_value = {"success": False, "error": "rejected"}
        else:
            ctx.maisaka.proactive.trigger.side_effect = TimeoutError("outcome unknown")
        path = tmp_path / "wakes.db"
        for _ in range(2):
            waker = module.PrivateTopicWaker(ctx, path, (DAY, "17:00-18:00"))
            await waker.tick(DAY, segment(), state(), ["qq:923856490"])
        assert ctx.chat.open_session.await_count == 1
        assert statuses(path)[0][-1] == "failed"
    asyncio.run(scenario())


def test_one_target_failure_does_not_block_another_and_midnight_uses_logical_day(tmp_path):
    async def scenario():
        ctx = context()
        ctx.maisaka.proactive.trigger.side_effect = [RuntimeError("first target"), {"success": True}]
        path = tmp_path / "wakes.db"
        waker = module.PrivateTopicWaker(ctx, path, (DAY, "23:00-24:00"))
        await waker.tick(DAY, segment("24:00", "26:00"), state(), ["qq:1", "qq:2"])
        assert statuses(path) == [
            ("2026-10-17", "24:00-26:00", "qq", "1", "failed"),
            ("2026-10-17", "24:00-26:00", "qq", "2", "queued"),
        ]
        assert "2026-10-17 24:00-26:00" in ctx.maisaka.proactive.trigger.await_args.kwargs["intent"]
    asyncio.run(scenario())


def test_cancelled_attempt_remains_claimed_after_restart(tmp_path):
    async def scenario():
        ctx = context()
        ctx.maisaka.proactive.trigger.side_effect = asyncio.CancelledError()
        path = tmp_path / "wakes.db"
        waker = module.PrivateTopicWaker(ctx, path, (DAY, "17:00-18:00"))
        with pytest.raises(asyncio.CancelledError):
            await waker.tick(DAY, segment(), state(), ["qq:923856490"])
        restarted = module.PrivateTopicWaker(ctx, path, (DAY, "17:00-18:00"))
        await restarted.tick(DAY, segment(), state(), ["qq:923856490"])
        assert ctx.maisaka.proactive.trigger.await_count == 1
        assert statuses(path)[0][-1] == "claimed"
    asyncio.run(scenario())


def test_manual_wake_can_include_initial_but_never_duplicate(tmp_path):
    async def scenario():
        ctx = context()
        waker = module.PrivateTopicWaker(ctx, tmp_path / "wakes.db", (DAY, segment().slot))
        await waker.tick(DAY, segment(), state(), ["qq:923856490"])
        ctx.maisaka.proactive.trigger.assert_not_awaited()
        for _ in range(2):
            await waker.tick(DAY, segment(), state(), ["qq:923856490"], include_initial=True)
        ctx.maisaka.proactive.trigger.assert_awaited_once()
    asyncio.run(scenario())


def test_explicit_manual_repeat_allows_one_more_wake_without_changing_default_dedup(tmp_path):
    async def scenario():
        ctx = context()
        waker = module.PrivateTopicWaker(ctx, tmp_path / "wakes.db", (DAY, segment().slot))
        await waker.tick(DAY, segment(), state(), ["qq:923856490"], include_initial=True)
        await waker.tick(DAY, segment(), state(), ["qq:923856490"], include_initial=True, repeat=True)
        await waker.tick(DAY, segment(), state(), ["qq:923856490"], include_initial=True)
        assert ctx.maisaka.proactive.trigger.await_count == 2
        assert len(statuses(tmp_path / "wakes.db")) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("target,slot,topic", [
    ("2026-10-16", "18:00-19:00", "话题"),
    ("2026-10-17", "19:00-20:00", "话题"),
    ("2026-10-17", "18:00-19:00", ""),
])
def test_manual_job_rejects_stale_or_empty_topic(monkeypatch, target, slot, topic):
    plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")

    async def scenario():
        plugin = plugin_module.ADayWithMittesPlugin()
        monkeypatch.setattr(plugin, "_require_store", lambda: None)
        monkeypatch.setattr(plugin, "_current", lambda: (None, DAY, segment(), state(topic)))
        monkeypatch.setattr(plugin, "_get_config", AsyncMock(return_value=True))
        plugin._private_topic_waker = SimpleNamespace(tick=AsyncMock())
        with pytest.raises(ValueError):
            await plugin._execute_admin_job({"action": "wake_private_topic", "target_date": target, "slot": slot})
        plugin._private_topic_waker.tick.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("source_slot,accepted", [
    ("18:00-19:00", True), ("20:00-21:00", False), ("12:00-13:00", False),
])
def test_manual_comparison_uses_original_event_but_rejects_future_or_missing_source(monkeypatch, source_slot, accepted):
    plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")

    async def scenario():
        plugin = plugin_module.ADayWithMittesPlugin()
        source, current, future = segment(), segment("19:00", "20:00"), segment("20:00", "21:00")
        original = state()
        store = SimpleNamespace(segments_of=lambda _: [source, current, future], state_of=lambda *_: original)
        monkeypatch.setattr(plugin, "_require_store", lambda: store)
        monkeypatch.setattr(plugin, "_current", lambda: (None, DAY, current, state("")))
        monkeypatch.setattr(plugin, "_get_config", AsyncMock(side_effect=[True, ["qq:923856490"]]))
        waker = SimpleNamespace(tick=AsyncMock())
        plugin._private_topic_waker = waker
        job = {
            "action": "wake_private_topic", "target_date": DAY.isoformat(), "slot": current.slot,
            "payload": json.dumps({"source_slot": source_slot, "repeat": True}),
        }
        if accepted:
            await plugin._execute_admin_job(job)
            waker.tick.assert_awaited_once_with(DAY, source, original, ["qq:923856490"], include_initial=True, repeat=True)
        else:
            with pytest.raises(ValueError):
                await plugin._execute_admin_job(job)
            waker.tick.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_plugin_loop_respects_switch_and_passes_current_schedule(monkeypatch, enabled):
    plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")

    async def scenario():
        plugin = plugin_module.ADayWithMittesPlugin()
        current = state()
        slot = segment()
        monkeypatch.setattr(plugin, "_current", lambda: (None, DAY, slot, current))
        monkeypatch.setattr(plugin, "_get_config", AsyncMock(side_effect=[enabled, ["qq:923856490"]]))
        waker = SimpleNamespace(tick=AsyncMock())
        plugin._private_topic_waker = waker
        # 一次轮询后模拟卸载，不能吞掉 CancelledError。
        monkeypatch.setattr(plugin_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await plugin._private_topic_loop()
        if enabled:
            waker.tick.assert_awaited_once_with(DAY, slot, current, ["qq:923856490"])
        else:
            waker.tick.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("users", ["qq:1", [123], ["923856490"], ["qq:"], [":1"], ["qq:1:2"]])
def test_invalid_allowlist_is_explicit_error(users):
    with pytest.raises(ValueError):
        module.parse_users(users)
