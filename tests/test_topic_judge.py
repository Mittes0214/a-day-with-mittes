"""谈资用掉判定：解析与 Hook 接线。不调用真实模型。"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncio
import importlib
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
judge = importlib.import_module("plugins.01_a_day_with_mittes.schedule.topic_judge")
plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
DAY = date(2026, 11, 7)
SLOT = "18:00-19:00"
TOPIC = "回家路上买的草莓大福在袋子里被压扁了。"


@pytest.mark.parametrize("text,expected", [
    ('{"used": true, "evidence": "大福都压扁了"}', (True, "大福都压扁了")),
    ('```json\n{"used": false, "evidence": ""}\n```', (False, "")),
    ('判断如下：{"used": true}', (True, "")),
])
def test_parse_accepts_wrapped_json(text, expected):
    assert judge.parse_judge_response(text) == expected


@pytest.mark.parametrize("text", ["", "讲到了", '{"used": "yes"}', '{"evidence": "x"}'])
def test_parse_rejects_unusable_output(text):
    with pytest.raises(ValueError):
        judge.parse_judge_response(text)


def test_prompt_carries_topic_and_reply():
    messages = judge.build_judge_prompt(TOPIC, "别提了，大福压成饼了")
    assert messages[0]["role"] == "system"
    assert TOPIC in messages[1]["content"] and "压成饼" in messages[1]["content"]


def make_plugin(monkeypatch, llm_result, shared=False):
    plugin = plugin_module.ADayWithMittesPlugin()
    store = SimpleNamespace(mark_shared=MagicMock())
    llm = SimpleNamespace(generate=AsyncMock(return_value=llm_result))
    plugin._ctx = SimpleNamespace(llm=llm)
    monkeypatch.setattr(plugin, "_require_store", lambda: store)
    monkeypatch.setattr(plugin, "_get_config", AsyncMock(side_effect=lambda key, default=None: default))
    monkeypatch.setattr(plugin, "_share_seen", AsyncMock(return_value=shared))
    return plugin, store, llm


def test_used_reply_marks_shared(monkeypatch):
    plugin, store, llm = make_plugin(monkeypatch, {"success": True, "response": '{"used": true, "evidence": "大福压成饼了"}'})
    plugin._topic_awaiting_judge["s1"] = (DAY, SLOT, TOPIC)
    asyncio.run(plugin.handle_replyer_after_response(session_id="s1", response="别提了，大福压成饼了"))
    assert llm.generate.await_args.kwargs["task_name"] == "utils"
    store.mark_shared.assert_called_once()
    args, kwargs = store.mark_shared.call_args
    assert args[:3] == (DAY, SLOT, "s1")
    assert kwargs["reply_text"] == "别提了，大福压成饼了"
    assert kwargs["hit_key"] == "判定：大福压成饼了"
    assert "s1" not in plugin._topic_awaiting_judge


@pytest.mark.parametrize("llm_result", [
    {"success": True, "response": '{"used": false, "evidence": ""}'},
    {"success": True, "response": "无法判断"},
    {"success": False, "error": "模型不可用"},
])
def test_not_used_or_failed_does_not_mark(monkeypatch, llm_result):
    plugin, store, _ = make_plugin(monkeypatch, llm_result)
    plugin._topic_awaiting_judge["s1"] = (DAY, SLOT, TOPIC)
    asyncio.run(plugin.handle_replyer_after_response(session_id="s1", response="今天好冷"))
    store.mark_shared.assert_not_called()


@pytest.mark.parametrize("pending,shared", [(False, False), (True, True)])
def test_skips_without_injection_or_when_already_shared(monkeypatch, pending, shared):
    plugin, store, llm = make_plugin(monkeypatch, {"success": True, "response": '{"used": true}'}, shared=shared)
    if pending:
        plugin._topic_awaiting_judge["s1"] = (DAY, SLOT, TOPIC)
    asyncio.run(plugin.handle_replyer_after_response(session_id="s1", response="随便说说"))
    llm.generate.assert_not_awaited()
    store.mark_shared.assert_not_called()
