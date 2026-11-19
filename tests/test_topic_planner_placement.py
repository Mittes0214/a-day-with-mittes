"""谈资（接话版）注入 planner【Mittes 此刻】的心情之后，replyer 不再注入。"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncio
import importlib
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
DAY = date(2026, 11, 19)
TOPIC = "傍晚在超市发现一盒打折的宇治抹茶粉。"


def make_plugin(shared=False):
    plugin = plugin_module.ADayWithMittesPlugin()
    segment = SimpleNamespace(slot="17:00-18:00")
    state = SimpleNamespace(topic=TOPIC, mood="雀跃", manner="")
    store = MagicMock()
    store.place_at.return_value = "电车上"
    plugin._store = store
    plugin._require_store = lambda: store
    plugin._current = lambda: (None, DAY, segment, state)
    plugin._get_config = AsyncMock(side_effect=lambda key, default=None: default)
    plugin._share_seen = AsyncMock(return_value=shared)
    plugin._with_calendar_suffix = lambda item, day: item
    plugin._manner_enabled = AsyncMock(return_value=False)
    plugin_module._render_trail = lambda state: ""
    return plugin, store


def planner_items():
    return [{"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "时间：17:02"}]}]


def test_topic_follows_mood_in_planner_block():
    plugin, store = make_plugin()
    result = asyncio.run(plugin.handle_planner_before_request(items=planner_items(), session_id="s1"))
    text = result["modified_kwargs"]["items"][1]["parts"][0]["text"]
    lines = text.split("\n")
    mood = lines.index("心情：雀跃")
    assert lines[mood + 1] == f"你今天遇到的一件小事：{TOPIC}"
    assert text.index(TOPIC) < text.index("可用get_mittes_schedule查询详情")
    store.mark_injected.assert_called_once_with(DAY, "17:00-18:00", "s1")
    assert plugin._topic_awaiting_judge["s1"] == (DAY, "17:00-18:00", TOPIC)


def test_shared_topic_is_not_injected():
    plugin, store = make_plugin(shared=True)
    result = asyncio.run(plugin.handle_planner_before_request(items=planner_items(), session_id="s1"))
    assert TOPIC not in result["modified_kwargs"]["items"][1]["parts"][0]["text"]
    store.mark_injected.assert_not_called()


def test_replyer_no_longer_injects_topic():
    plugin, _ = make_plugin()
    items = [{"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人设"}]}]
    result = asyncio.run(plugin.handle_replyer_before_model_request(items=items, session_id="s1"))
    assert result == {"success": True, "action": "continue"}
