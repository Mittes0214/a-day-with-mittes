"""服饰图片查询与跨 planner/replyer 传递的回归验证（不发模型请求）。"""

from pathlib import Path
from types import SimpleNamespace

import asyncio
import copy
import importlib
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
images_module = importlib.import_module("plugins.01_a_day_with_mittes.character.outfit_images")


def call(name, call_id, turn="turn-a", **args):
    return {
        "item_type": "FunctionCallItem",
        "meta": {"item_id": call_id, "logical_turn_id": turn, "timestamp": "2026-10-12T12:00:00+09:00"},
        "tool_call": {"call_id": call_id, "func_name": name, "args": args, "extra_content": {}},
    }


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def plugin():
    p = plugin_module.ADayWithMittesPlugin()
    p._wardrobe = plugin_module.Wardrobe(p._plugin_dir / "character/wardrobe.toml")
    p._wardrobe.load()
    p._outfit_images.load(p._wardrobe.names)
    p._store = plugin_module.ScheduleStore(p._plugin_dir / "schedule/skeleton.toml", Path("/unused"))
    p._store.load_skeleton()
    return p


def test_all_nine_assets_are_real_pngs(plugin):
    from base64 import b64decode

    for name in plugin._wardrobe.names:
        assert b64decode(run(plugin._outfit_images.image(name))).startswith(b"\x89PNG\r\n\x1a\n")


def test_tool_returns_only_title_and_matching_image_for_explicit_time(plugin):
    captured = []
    name = "浅蓝细棉长袖睡衣"
    plugin._store = SimpleNamespace(segment_at=lambda moment: (captured.append(moment), SimpleNamespace(outfit=name))[1])
    result = run(plugin.tool_get_outfit("2026-10-12 00:30"))
    assert captured[0].hour == 0 and captured[0].day == 12
    assert result["content"] == images_module.REFERENCE_TITLE
    assert result["content_items"] == [{
        "content_type": "image", "mime_type": "image/png", "data": run(plugin._outfit_images.image(name)),
    }]
    assert "metadata" not in result


@pytest.mark.parametrize("moment", ["2026-10-12 00:30", "2026-10-12 09:00", "2026-10-13 18:00"])
def test_query_follows_schedule_including_midnight_and_future(plugin, moment):
    expected = plugin._store.segment_at(plugin_module.parse_moment(moment)).outfit
    result = run(plugin.tool_get_outfit(moment))
    assert result["content_items"][0]["data"] == run(plugin._outfit_images.image(expected))


def test_invalid_time_and_unknown_outfit_fail_without_inventing_image(plugin):
    bad_time = run(plugin.tool_get_outfit("不是时间"))
    assert bad_time["success"] is False and "content_items" not in bad_time
    plugin._store = SimpleNamespace(segment_at=lambda _: SimpleNamespace(outfit="临时拍摄装"))
    unknown = run(plugin.tool_get_outfit())
    assert unknown["success"] is False and "content_items" not in unknown


def test_continuation_multiple_queries_failures_and_session_isolation():
    bridge = images_module.OutfitQueryBridge()
    bridge.tag_calls("group-a", [call("get_mittes_outfit", "q1")])
    bridge.record("group-a", "turn-a", "q1", "校服")
    # 同一逻辑轮继续推理；同一批中的 reply 只收集此前的查询。
    calls = bridge.tag_calls("group-a", [
        call("get_mittes_outfit", "q2"), call("get_mittes_outfit", "failed"),
        call("reply", "r1"), call("get_mittes_outfit", "after-reply"),
    ])
    bridge.record("group-a", "turn-a", "q2", "睡衣")
    bridge.record("group-a", "turn-a", "after-reply", "女仆装")
    args = calls[2]["tool_call"]["args"]
    snapshot = bridge.snapshot("group-a", args)
    assert snapshot[images_module.IMAGES_ARG] == ["校服", "睡衣"]
    assert bridge.snapshot("group-b", args)[images_module.IMAGES_ARG] == []
    # 新轮不能沿用旧图；已经进入 replyer 的快照在重试时仍不变。
    new = bridge.tag_calls("group-a", [call("reply", "r2", turn="turn-b")])
    assert bridge.snapshot("group-a", new[0]["tool_call"]["args"])[images_module.IMAGES_ARG] == []
    assert bridge.snapshot("group-a", snapshot) == snapshot
    bridge.record("group-a", "turn-a", "q1", "迟到的旧轮结果")
    assert bridge.snapshot("group-a", new[0]["tool_call"]["args"])[images_module.IMAGES_ARG] == []


def test_replyer_uses_user_image_before_reference_and_preserves_retry_snapshot(plugin):
    # 按主程序的客户端初始化顺序导入，避免直接载入 snapshot 的循环导入。
    import src.llm_models.model_client  # noqa: F401

    from src.llm_models.payload_content.context_item import ContextImagePart, UserMessageItem
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    from src.llm_models.request_snapshot import deserialize_context_item_snapshot
    from src.plugin_runtime.component_query import ComponentQueryService
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.core.tooling import ToolExecutionContext, ToolInvocation

    tagged = run(plugin.handle_outfit_planner_response(
        output_items=[call("get_mittes_outfit", "q1"), call("reply", "r1", reply_reference="回答服饰问题")],
        session_id="group-a", item_schema_version=1,
    ))["modified_kwargs"]["output_items"]
    # 实际 host 协议必须接受附加的内部参数，并原样传入插件执行器。
    restored = deserialize_prompt_items(tagged, mode=ContextProtocolMode.MODEL_OUTPUT)
    query_args = restored[0].tool_call.materialize_args()
    payload = ComponentQueryService._build_tool_invocation_payload(
        SimpleNamespace(invoke_method="plugin.invoke_tool"),
        ToolInvocation(tool_name="get_mittes_outfit", arguments={"time": "2026-10-12 09:00", **query_args}),
        ToolExecutionContext(session_id="group-a", stream_id="group-a"),
    )
    tool = run(plugin.tool_get_outfit(**payload))
    args = restored[1].tool_call.materialize_args()
    frozen = run(plugin.handle_outfit_replyer_request(
        reply_tool_args=args, session_id="group-a", task_name="replyer", item_schema_version=1,
    ))["modified_kwargs"]["reply_tool_args"]
    original = [plugin_module._new_user_item("真实聊天"), plugin_module._new_user_item("回答服饰问题"),
                plugin_module._new_user_item("当前时间：2026-10-12")]
    before = copy.deepcopy(original)
    injected = run(plugin.handle_outfit_replyer_model_request(
        items=original, reply_tool_args=frozen, session_id="group-a", selected_model_visual=True, item_schema_version=1,
    ))["modified_kwargs"]
    assert original == before
    assert injected["session_id"] == "group-a" and injected["item_schema_version"] == 1
    block = injected["items"][1]
    assert block["parts"][0] == {"type": "text", "text": images_module.REFERENCE_TITLE}
    assert len(block["parts"]) == 2
    parsed = deserialize_context_item_snapshot(block)
    assert isinstance(parsed, UserMessageItem)
    assert isinstance(parsed.parts[1], ContextImagePart)
    assert parsed.parts[1].image_base64 == tool["content_items"][0]["data"]
    twice = run(plugin.handle_outfit_replyer_model_request(
        items=injected["items"], reply_tool_args=frozen, selected_model_visual=True,
    ))["modified_kwargs"]["items"]
    assert sum(plugin_module._item_text(i) == images_module.REFERENCE_TITLE for i in twice) == 1
    assert plugin._outfit_queries.snapshot("group-b", args)[images_module.IMAGES_ARG] == []


def test_replyer_without_query_or_visual_model_has_no_injected_image(plugin):
    items = [plugin_module._new_user_item("当前时间：2026-10-12")]
    assert "modified_kwargs" not in run(plugin.handle_outfit_replyer_model_request(
        items=items, reply_tool_args={}, selected_model_visual=True,
    ))
    assert "modified_kwargs" not in run(plugin.handle_outfit_replyer_model_request(
        items=items, reply_tool_args={images_module.IMAGES_ARG: ["黑白女仆装"]},
        selected_model_visual=False, selected_model_name="text-only",
    ))


def test_planner_tool_result_matches_host_image_protocol(plugin):
    from src.plugin_runtime.component_query import ComponentQueryService

    entry = SimpleNamespace(name="get_mittes_outfit", plugin_id="khiqwq.a-day-with-mittes")
    raw = run(plugin.tool_get_outfit("2026-10-12 09:00"))
    parsed = ComponentQueryService._parse_tool_invoke_result(entry, raw)
    assert parsed.success
    assert parsed.get_history_content() == images_module.REFERENCE_TITLE
    assert parsed.content_items[0].content_type == "image"
    assert parsed.content_items[0].data == raw["content_items"][0]["data"]
