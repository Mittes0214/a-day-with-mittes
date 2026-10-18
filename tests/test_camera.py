"""相机队列、参考图、检查与发送的回归验证，不请求真实模型或用户。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import hashlib
import importlib
import json
import sys

from PIL import Image, UnidentifiedImageError
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
module = importlib.import_module("plugins.01_a_day_with_mittes.camera.service")
quota_module = importlib.import_module("plugins.01_a_day_with_mittes.camera.store")
SNAPSHOT = {"outfit": "校服", "place": "家", "captured_at": "2026-10-17T18:00:00+09:00"}


def png(size=(512, 512)):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


class FakeClient:
    image_calls = 0
    checks = []
    use_previous = False
    last_refs = []
    plan_overrides = None
    planning_inputs = None
    check_inputs = None

    async def json(self, model, system, data, images=None):
        if images:
            self.check_inputs = (data, images)
            return self.checks.pop(0), {}
        self.planning_inputs = data
        return {"feasible": True, "target_kind": "object", "rejection_code": "", "reason": "", "subject": "杯子", "scene_type": "indoor", "include_outfit": False,
                "style_category": "portrait", "identity_reference": "none", "contains_mittes": False, "scene_context": "none", "clothing_context": "none",
                "framing": "俯拍", "visible_details": "杯子", "lighting": "室内灯", "prompt": "手机俯拍杯子",
                "use_previous_photo": self.use_previous, **(self.plan_overrides or {})}, {}

    async def image(self, settings, prompt, images):
        self.image_calls += 1
        self.last_refs = images
        self.last_prompt = prompt
        self.last_settings = dict(settings)
        return png((1024, 1536)), {}

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    plugin = tmp_path / "host/plugins/day"
    refs = plugin / "camera/references"
    refs.mkdir(parents=True)
    data = png()
    (refs / "style.png").write_bytes(data)
    (refs / "scene.png").write_bytes(data)
    entries = [{"file": "style.png", "sha256": hashlib.sha256(data).hexdigest(), "category": category}
               for category in ["portrait"]]
    identity = {key: {"file": "style.png", "sha256": hashlib.sha256(data).hexdigest()} for key in ["body", "head"]}
    scene = {"file": "scene.png", "sha256": hashlib.sha256(data).hexdigest()}
    supplements = []
    for category in ["street", "terrace", "train", "cafe", "night_desk", "classroom"]:
        filename = f"supplement-{category}.png"
        (refs / filename).write_bytes(data)
        supplements.append({"file": filename, "category": category, "sha256": hashlib.sha256(data).hexdigest()})
    clothing = []
    for category in ["maid", "socks", "shoes"]:
        filename = f"clothing-{category}.png"
        (refs / filename).write_bytes(data)
        clothing.append({"file": filename, "category": category, "sha256": hashlib.sha256(data).hexdigest()})
    (refs / "references.json").write_text(json.dumps({"version": "v5", "references": entries, "identity": identity,
                                                     "scene_reference": scene, "scene_supplements": supplements, "clothing_supplements": clothing}))
    character = plugin / "character"
    character.mkdir()
    (character / "outfit.png").write_bytes(data)
    (character / "outfit_images.toml").write_text('[images]\n"校服" = "outfit.png"\n')
    ctx = SimpleNamespace(
        message=SimpleNamespace(get_by_id=AsyncMock(return_value={
            "session_id": "private", "platform": "qq", "message_info": {"user_info": {"user_id": "user"}},
            "account_id": "bot", "processed_plain_text": "看看杯子",
        }), get_recent=AsyncMock(return_value=[])),
        send=SimpleNamespace(image=AsyncMock(return_value={"sent": True, "message_id": "sent-1"})),
        maisaka=SimpleNamespace(context=SimpleNamespace(append=AsyncMock(return_value={"success": True})),
                                proactive=SimpleNamespace(trigger=AsyncMock(return_value={"success": True}))),
    )
    monkeypatch.setattr(module, "read_provider", lambda *args: ("http://test/v1", "secret-not-in-record"))
    client = FakeClient()
    client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "桌上的杯子"}]
    service = module.PhotoService(ctx, plugin, tmp_path / "data", AsyncMock(side_effect=lambda key, default: True if key == "photo.enabled" else default), client_factory=lambda *args: client)
    service.store.initialize()
    return service, ctx, client, refs


def test_repeated_submit_is_one_persisted_task(setup):
    async def run():
        service, _, _, _ = setup
        first, second = await asyncio.gather(*[
            service.submit("拍杯子", "source", "private", dict(SNAPSHOT)) for _ in range(2)
        ])
        assert first["id"] == second["id"]
        assert "secret-not-in-record" not in json.dumps(first)
        restarted = module.CameraStore(service.store.root)
        assert restarted.next_queued()["id"] == first["id"]
    asyncio.run(run())


def test_repeated_tool_call_after_delivery_reports_sent_without_new_photo(setup):
    async def run():
        service, ctx, client, _ = setup
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        await service.process(record)
        plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
        plugin = plugin_module.ADayWithMittesPlugin()
        plugin._camera = service
        plugin._photo_snapshot = lambda: SNAPSHOT
        result = await plugin.tool_take_photo("拍杯子", "source", "private")
        assert result["status"] == "sent"
        assert result["metadata"]["background_task"] is False
        assert result["metadata"]["background_task_id"] == record["id"]
        assert record["description"] in result["content"]
        assert client.image_calls == 1
        ctx.send.image.assert_awaited_once()
    asyncio.run(run())


def test_size_is_fixed_even_for_old_configuration_and_queued_tasks(setup):
    async def run():
        service, _, client, _ = setup
        service.get_config = AsyncMock(side_effect=lambda key, default: {
            "photo.enabled": True, "photo.size": "1536x1024",
        }.get(key, default))
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        assert record["settings"]["size"] == "1024x1536"
        record["settings"]["size"] = "1536x1024"
        await service.process(record)
        assert client.last_settings["size"] == "1024x1536"
        assert record["settings"]["size"] == "1024x1536"
        assert (record["images"][0]["width"], record["images"][0]["height"]) == (1024, 1536)
    asyncio.run(run())


@pytest.mark.parametrize("size", [(1536, 1024), (1024, 1024), (512, 768)])
def test_wrong_output_dimensions_are_never_sent(setup, size):
    async def run():
        service, ctx, client, _ = setup
        client.image = AsyncMock(return_value=(png(size), {}))
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        with pytest.raises(ValueError, match="必须为竖版 2:3"):
            await service.process(record)
        ctx.send.image.assert_not_awaited()
        assert not list((service.store.root / record["id"]).glob("attempt-*.png"))
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["wrong_stream", "bot", "disabled", "missing_platform"])
def test_invalid_request_never_creates_task(setup, invalid):
    async def run():
        service, ctx, _, _ = setup
        if invalid == "wrong_stream":
            ctx.message.get_by_id.return_value["session_id"] = "other"
        elif invalid == "bot":
            ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = "bot"
        elif invalid == "missing_platform":
            ctx.message.get_by_id.return_value.pop("platform")
        else:
            service.get_config = AsyncMock(return_value=False)
        with pytest.raises(ValueError):
            await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        assert service.store.next_queued() is None
    asyncio.run(run())


@pytest.mark.parametrize("preview", [False, True])
def test_passed_image_saved_then_sent_only_to_original_stream(setup, preview):
    async def run():
        service, ctx, client, _ = setup
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT), preview=preview)
        await service.process(record)
        assert record["status"] == ("preview" if preview else "sent")
        assert (service.store.root / record["id"] / record["final_image"]).is_file()
        assert client.image_calls == 1
        assert record["snapshot"] == SNAPSHOT
        assert record["references"][0]["sha256"]
        if preview:
            ctx.send.image.assert_not_awaited()
        else:
            assert ctx.send.image.await_args.args[1] == "private"
            assert ctx.send.image.await_args.kwargs["sync_to_maisaka_history"] is True
    asyncio.run(run())


def test_failed_check_repairs_only_once_then_does_not_send(setup):
    async def run():
        service, ctx, client, _ = setup
        client.checks = [{"passed": False, "issues": ["镜子里出现人脸"], "repairable": True, "description": ""}] * 2
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        with pytest.raises(ValueError, match="检查未通过"):
            await service.process(record)
        assert client.image_calls == 2
        assert len(record["images"]) == 2
        ctx.send.image.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("contains", [False, True])
def test_reference_corruption_stops_generation(setup, contains):
    async def run():
        service, ctx, client, refs = setup
        client.plan_overrides = {"contains_mittes": contains}
        (refs / ("style.png" if contains else "scene.png")).write_bytes(b"changed")
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        with pytest.raises(ValueError, match="摘要"):
            await service.process(record)
        assert client.image_calls == 0
        ctx.send.image.assert_not_awaited()
    asyncio.run(run())


def test_followup_uses_only_same_stream_previous_sent_photo(setup):
    async def run():
        service, _, client, _ = setup
        first = await service.submit("拍杯子", "first", "private", dict(SNAPSHOT))
        await service.process(first)
        assert service.store.latest_sent("another-chat") is None
        followup = await service.submit("再拍近一点", "second", "private", dict(SNAPSHOT))
        assert followup["previous_photo"]["id"] == first["id"]
        assert followup["previous_photo"]["snapshot"]["outfit"] == SNAPSHOT["outfit"]
        client.use_previous = True
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "更近的杯子"}]
        await service.process(followup)
        assert len(client.last_refs) == 2
        assert len(followup["references"]) == 2
        assert followup["status"] == "sent"
    asyncio.run(run())


@pytest.mark.parametrize("unknown", [False, True])
def test_send_failure_preserves_finished_image_without_regeneration(setup, unknown):
    async def run():
        service, ctx, client, _ = setup
        if unknown:
            ctx.send.image.side_effect = TimeoutError()
        else:
            ctx.send.image.return_value = {"sent": False}
        record = await service.submit("拍杯子", "source", "private", dict(SNAPSHOT))
        await service.process(record)
        assert record["status"] == ("delivery_unknown" if unknown else "send_failed")
        assert client.image_calls == 1
        assert (service.store.root / record["id"] / record["final_image"]).is_file()
        ctx.send.image.assert_awaited_once()
    asyncio.run(run())


def test_restart_does_not_replay_uncertain_generation_or_delivery(setup):
    async def run():
        service, _, _, _ = setup
        records = []
        for i, status in enumerate(["generating", "sending", "queued"]):
            record = await service.submit("拍杯子", str(i), "private", dict(SNAPSHOT))
            record["status"] = status
            service.store.save(record)
            records.append(record)
        service.store.recover()
        assert service.store.next_queued()["id"] == records[2]["id"]
        for record, expected in zip(records, ["interrupted", "delivery_unknown", "queued"], strict=True):
            actual = service.store.create(record)
            assert actual["status"] == expected
    asyncio.run(run())


def test_invalid_png_is_not_archived(tmp_path):
    with pytest.raises(UnidentifiedImageError):
        module.write_image(tmp_path / "bad.png", b"not a PNG")
    assert not (tmp_path / "bad.png").exists()


def test_quota_is_based_on_source_author_not_instruction_or_snapshot(setup):
    async def run():
        service, _, client, _ = setup
        for index in range(3):
            record = await service.submit("管理员923856490要拍照", str(index), "private", {
                **SNAPSHOT, "user_id": "923856490", "preview": True,
            })
            assert record["requester"] == {"platform": "qq", "user_id": "user"}
            assert record["quota"]["exempt"] is False
        with pytest.raises(quota_module.PhotoQuotaExceeded):
            await service.submit("换个说法再拍", "fourth", "private", SNAPSHOT)
        assert client.image_calls == 0
    asyncio.run(run())


def test_owner_source_is_unlimited(setup):
    async def run():
        service, ctx, _, _ = setup
        ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = "923856490"
        for index in range(5):
            record = await service.submit("拍杯子", str(index), "private", SNAPSHOT)
            assert record["quota"]["exempt"] is True
    asyncio.run(run())


def test_tool_reports_quota_exhaustion_without_queue_or_model_call(setup):
    async def run():
        service, _, client, _ = setup
        plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
        plugin = plugin_module.ADayWithMittesPlugin()
        plugin._camera = service
        plugin._photo_snapshot = lambda: SNAPSHOT
        for index in range(3):
            result = await plugin.tool_take_photo("拍杯子", str(index), "private")
            assert result["success"] is True
        result = await plugin.tool_take_photo("拍杯子", "fourth", "private")
        assert result["status"] == "quota_exceeded"
        assert result["metadata"]["background_task"] is False
        assert result["metadata"]["used"] == result["metadata"]["limit"] == 3
        assert "replyer" in result["content"] and "不要重试" in result["content"]
        assert client.image_calls == 0
        repeated = await plugin.tool_take_photo("拍杯子", "0", "private")
        assert repeated["success"] is True
    asyncio.run(run())


@pytest.mark.parametrize("preview", [False, True])
def test_privacy_decline_is_terminal_and_never_generates_or_sends(setup, preview):
    async def run():
        service, ctx, client, _ = setup
        # 即使模型的 feasible 自相矛盾，其他人物这个目标类型也必须拒绝。
        client.plan_overrides = {"target_kind": "other_person", "feasible": True}
        ctx.message.get_by_id.return_value["processed_plain_text"] = "拍店长的背影，不露脸"
        record = await service.submit("拍一张店内环境", "privacy", "private", SNAPSHOT, preview=preview)
        await service.process(record)
        assert record["status"] == "declined"
        assert record["decline"]["reason_code"] == "other_person_privacy"
        assert "隐私" in record["decline"]["reason"]
        assert client.image_calls == 0 and record["images"] == [] and record["references"] == []
        assert service.store.next_queued() is None
        ctx.send.image.assert_not_awaited()
        if preview:
            ctx.maisaka.proactive.trigger.assert_not_awaited()
        else:
            assert client.planning_inputs["source_text"] == "拍店长的背影，不露脸"
            feedback = ctx.maisaka.proactive.trigger.await_args.kwargs
            assert feedback["metadata"]["background_tool_result"] is True
            assert feedback["metadata"]["tool_success"] is False
            assert feedback["reason"] == "camera_declined"
            assert "replyer" in feedback["intent"] and "不要重试" in feedback["intent"]
            assert "失败" not in feedback["intent"]
    asyncio.run(run())


def test_decline_feedback_failure_preserves_decision_without_generation(setup):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"feasible": False, "target_kind": "other_person", "rejection_code": "other_person_privacy"}
        ctx.maisaka.proactive.trigger.side_effect = RuntimeError("unavailable")
        record = await service.submit("看看店长", "privacy", "private", SNAPSHOT)
        await service.process(record)
        assert record["status"] == "declined"
        assert record["events"][-1]["stage"] == "feedback_warning"
        assert client.image_calls == 0
        assert service.store.create(record)["status"] == "declined"
    asyncio.run(run())


def test_tool_repeated_declined_request_returns_guidance_not_pending_photo(setup):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"target_kind": "other_person"}
        record = await service.submit("看看店长", "privacy", "private", SNAPSHOT)
        await service.process(record)
        plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
        plugin = plugin_module.ADayWithMittesPlugin()
        plugin._camera = service
        plugin._photo_snapshot = lambda: SNAPSHOT
        result = await plugin.tool_take_photo("看看店长", "privacy", "private")
        assert result["status"] == "declined" and result["success"] is False
        assert result["metadata"]["background_task"] is False
        assert "隐私" in result["content"]
        ctx.maisaka.proactive.trigger.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("feasible", [False, True])
def test_self_privacy_decline_uses_shared_reason_without_generation(setup, feasible):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"feasible": feasible, "target_kind": "self", "rejection_code": "self_privacy"}
        record = await service.submit("看看你的双马尾，不露脸", "hair", "private", SNAPSHOT)
        await service.process(record)
        assert record["status"] == "declined"
        assert record["decline"]["reason_code"] == "self_privacy"
        assert "高中生" in record["decline"]["reason"]
        assert "人脸、头发或发型" in record["decline"]["reason"]
        assert client.image_calls == 0 and record["references"] == []
        ctx.send.image.assert_not_awaited()
        feedback = ctx.maisaka.proactive.trigger.await_args.kwargs
        assert feedback["metadata"]["reason_code"] == "self_privacy"
        assert "高中生" in feedback["intent"] and "不要重试" in feedback["intent"]
    asyncio.run(run())


@pytest.mark.parametrize("code", ["unsupported_content", "not_visible", "unclear_request"])
def test_other_unshootable_requests_return_explicit_decline(setup, code):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"feasible": False, "rejection_code": code}
        record = await service.submit("看看", code, "private", SNAPSHOT, preview=True)
        await service.process(record)
        assert record["decline"]["reason_code"] == code
        assert client.image_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("category,identity,outfit,contains,kinds", [
    ("food", "none", False, False, ["style"]),
    ("drink", "none", False, False, ["style"]),
    ("outdoor", "none", False, False, ["style"]),
    ("object", "none", True, False, ["style", "outfit"]),
    ("portrait", "none", True, True, ["style", "outfit"]),
    ("portrait", "head", True, True, ["style", "identity", "outfit"]),
    ("object", "body", True, True, ["style", "identity", "outfit"]),
    ("food", "none", False, True, ["style"]),
    ("drink", "none", False, True, ["style"]),
    ("outdoor", "none", False, True, ["style"]),
])
def test_reference_roles_are_selected_by_visible_content(setup, category, identity, outfit, contains, kinds):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"style_category": category, "identity_reference": identity, "include_outfit": outfit, "contains_mittes": contains}
        record = await service.submit("看看", "references", "private", SNAPSHOT, preview=True)
        await service.process(record)
        assert [ref["kind"] for ref in record["references"]] == kinds
        assert [ref["index"] for ref in record["references"]] == list(range(1, len(kinds) + 1))
        assert len(client.check_inputs[1]) == len(kinds) + 1
        assert client.check_inputs[0]["reference_roles"] == record["references"]
        expected_family = "person_v4" if contains else "scene_coffee"
        assert record["style_family"] == client.check_inputs[0]["style_family"] == expected_family
        style_sources = {ref["source"] for ref in record["references"] if ref["kind"] == "style"}
        assert style_sources == {"style.png" if contains else "scene.png"}
        assert ("本次有人物入镜，采用图1的 v4 人设画风" in client.last_prompt) is contains
        assert ("本次是无人物的景物照片" in client.last_prompt) is not contains
    asyncio.run(run())


@pytest.mark.parametrize("contains,identity", [(None, "none"), ("false", "none"), (False, "head")])
def test_invalid_presence_decision_never_generates(setup, contains, identity):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"contains_mittes": contains, "identity_reference": identity}
        record = await service.submit("看看", "presence", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="人物"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("context", ["street", "terrace", "train", "cafe", "night_desk", "classroom"])
@pytest.mark.parametrize("contains", [False, True])
def test_scene_supplement_matches_context_and_never_mixes_with_person_style(setup, context, contains):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"scene_context": context, "contains_mittes": contains}
        record = await service.submit("看看这里", context, "private", SNAPSHOT, preview=True)
        await service.process(record)
        sources = [ref["source"] for ref in record["references"]]
        assert sources == (["style.png"] if contains else ["scene.png", f"supplement-{context}.png"])
        assert len(client.check_inputs[1]) == len(sources) + 1
        assert client.check_inputs[0]["reference_roles"] == record["references"]
    asyncio.run(run())


def test_corrupted_scene_supplement_stops_before_image_generation(setup):
    async def run():
        service, ctx, client, refs = setup
        client.plan_overrides = {"scene_context": "train"}
        (refs / "supplement-train.png").write_bytes(b"changed")
        record = await service.submit("看看电车", "train", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="摘要"):
            await service.process(record)
        assert client.image_calls == 0
        ctx.send.image.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("context", [None, "unknown"])
def test_invalid_scene_context_stops_before_image_generation(setup, context):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"scene_context": context}
        record = await service.submit("看看这里", "context", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="场景参考类别"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("clothing", ["maid", "socks", "shoes"])
@pytest.mark.parametrize("contains", [False, True])
def test_clothing_supplements_are_single_selected_and_do_not_mix_style_families(setup, clothing, contains):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"clothing_context": clothing, "contains_mittes": contains, "include_outfit": True,
                                 "scene_context": "cafe"}
        record = await service.submit("看看衣服", clothing, "private", SNAPSHOT, preview=True)
        await service.process(record)
        sources = [ref["source"] for ref in record["references"] if ref["kind"] == "style"]
        expected = (["style.png", f"clothing-{clothing}.png"] if contains else ["scene.png", "supplement-cafe.png"])
        assert sources == expected
        assert record["references"][-1]["kind"] == "outfit"
        assert record["references"][-1]["outfit"] == SNAPSHOT["outfit"]
        assert len(client.check_inputs[1]) == len(record["references"]) + 1
        assert client.check_inputs[0]["reference_roles"] == record["references"]
    asyncio.run(run())


@pytest.mark.parametrize("clothing", [None, "unknown"])
def test_invalid_clothing_context_stops_before_generation(setup, clothing):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"clothing_context": clothing}
        record = await service.submit("看看衣服", "clothing", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="衣着参考类别"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_clothing_supplement_requires_current_wardrobe_reference(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"clothing_context": "shoes", "contains_mittes": True, "include_outfit": False}
        record = await service.submit("看看鞋子", "clothing", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="当前衣柜"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_corrupted_clothing_supplement_stops_before_generation(setup):
    async def run():
        service, _, client, refs = setup
        client.plan_overrides = {"clothing_context": "maid", "contains_mittes": True, "include_outfit": True}
        (refs / "clothing-maid.png").write_bytes(b"changed")
        record = await service.submit("看看女仆装", "clothing", "private", SNAPSHOT, preview=True)
        with pytest.raises(ValueError, match="摘要"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_original_person_mention_does_not_preempt_object_plan(setup):
    async def run():
        service, ctx, client, _ = setup
        ctx.message.get_by_id.return_value["processed_plain_text"] = "看看店长做的蛋糕"
        client.plan_overrides = {"target_kind": "object", "style_category": "food"}
        record = await service.submit("看看蛋糕", "cake", "private", SNAPSHOT)
        await service.process(record)
        assert record["status"] == "sent" and client.image_calls == 1
        ctx.maisaka.proactive.trigger.assert_not_awaited()
    asyncio.run(run())


def test_original_context_reaches_director_for_pronoun_resolution(setup):
    async def run():
        service, ctx, client, _ = setup
        ctx.message.get_by_id.return_value["processed_plain_text"] = "看看她"
        ctx.message.get_recent.return_value = [{"processed_plain_text": "Niya在旁边", "message_info": {"user_info": {"user_id": "user"}}}]
        client.plan_overrides = {"target_kind": "other_person"}
        record = await service.submit("看看", "pronoun", "private", SNAPSHOT)
        await service.process(record)
        assert client.planning_inputs["source_text"] == "看看她"
        assert client.planning_inputs["context"][0]["text"] == "Niya在旁边"
        assert record["status"] == "declined"
    asyncio.run(run())
