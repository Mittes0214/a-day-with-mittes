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
    rewrite_calls = 0
    rewrite_result = {"retry_allowed": True, "reason": "普通生活照，整理场景表述", "prompt": "手机拍摄桌上的杯子，室内自然光。"}

    async def json(self, model, system, data, images=None):
        if system == module.SAFE_REWRITE:
            self.rewrite_calls += 1
            self.rewrite_inputs = (data, images)
            return dict(self.rewrite_result), {}
        if images:
            self.check_inputs = (data, images)
            return self.checks.pop(0), {}
        self.planning_inputs = data
        return {"feasible": True, "target_kind": "object", "rejection_code": "", "reason": "", "subject": "杯子", "scene_type": "indoor", "include_outfit": False,
                "style_category": "portrait", "identity_reference": "none", "contains_mittes": False, "scene_context": "none", "clothing_context": "none",
                "shot": {"method": "handheld", "phone": "双手持机在桌面上方", "height": "胸口", "angle": "俯拍", "distance": "约四十厘米"},
                "frame": {"top": "杯子上方留白", "bottom": "桌面边缘", "sides": "杯子居中", "focus": "杯子占画面三分之一"},
                "face_cover": {"method": "none", "detail": ""}, "pose": "", "visible": ["杯子", "木桌面"],
                "background": {"content": "桌面与墙", "people": "无"}, "lighting": {"source": "室内灯", "direction": "顶光", "tone": "暖色"},
                "prompt": "手机俯拍杯子",
                "expression": {"visible": False, "intent": "", "mouth": "", "basis": ""},
                "use_previous_photo": self.use_previous, **(self.plan_overrides or {})} | self._face_cover(), {}

    def _face_cover(self):
        # 有人物入镜又没指定遮脸方式时，假取景分析器默认裁切（真实分析器必须自己选）
        overrides = self.plan_overrides or {}
        if overrides.get("contains_mittes") and "face_cover" not in overrides:
            return {"face_cover": {"method": "crop", "detail": "画框上沿在鼻子与上唇之间"}}
        return {}

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
        config=SimpleNamespace(get=AsyncMock(return_value=["qq:923856490", "telegram:1739875050"])),
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
        calls = record["model_calls"]
        assert [call["stage"] for call in calls] == ["planning", "generating", "checking"]
        assert all(call["status"] == "completed" for call in calls)
        assert calls[0]["input"]["system"] == module.DIRECTOR
        assert calls[0]["input"]["data"]["snapshot"] == SNAPSHOT
        assert calls[1]["input"]["text"] == client.last_prompt
        assert calls[1]["output"]["image"]["file"] == "attempt-1.png"
        assert [item["file"] for item in calls[2]["input"]["images"]] == ["attempt-1.png", *[ref["file"] for ref in record["references"]]]
        assert calls[2]["output"]["result"] == record["images"][0]["check"]
        assert "secret-not-in-record" not in json.dumps(calls)
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


@pytest.mark.parametrize("preview", [False, True])
def test_passed_person_photo_with_hair_uses_first_image(setup, preview):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self"}
        # Legacy/extra checker metadata must not trigger the removed fixed hair pass.
        client.checks = [{"passed": True, "issues": [], "repairable": False,
                          "description": "双马尾自然垂在肩侧", "hair_visible": True,
                          "hair_refinement": "整理发尾"}]
        record = await service.submit("拍一张穿搭", "hair-first", "private", SNAPSHOT, preview=preview)
        await service.process(record)
        assert client.image_calls == 1 and len(record["images"]) == 1
        assert record["final_image"] == "attempt-1.png" and "hair_refinement" not in record
        assert record["status"] == ("preview" if preview else "sent")
        if preview:
            ctx.send.image.assert_not_awaited()
        else:
            ctx.send.image.assert_awaited_once()
    asyncio.run(run())


def test_hair_structure_error_still_uses_regular_single_repair(setup):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self"}
        client.checks = [
            {"passed": False, "issues": ["右侧发束穿过衣袖"], "repairable": True, "description": "初稿"},
            {"passed": True, "issues": [], "repairable": False, "description": "发束自然垂落"},
        ]
        record = await service.submit("拍一张穿搭", "hair-error", "private", SNAPSHOT)
        await service.process(record)
        assert client.image_calls == 2 and record["final_image"] == "attempt-2.png"
        assert "右侧发束穿过衣袖" in client.last_prompt
        assert len(client.last_refs) == len(record["references"]) + 1
        ctx.send.image.assert_awaited_once()
    asyncio.run(run())


def test_image_prompt_is_readable_but_planning_and_check_keep_full_snapshot(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self", "include_outfit": True}
        snapshot = {
            **SNAPSHOT, "logical_day": "2026-10-17", "slot": "18:00-20:00", "title": "休息",
            "places": [{"from": "19:00", "to": "20:00", "place": "之后才到的地点"}],
            "outfit_details": {"hair": "松束双马尾", "top": "浅蓝睡衣"},
            "appearance": {"hair_base": "银白浅樱粉", "props": "仅归档的道具设定"},
            "story": "整个时段的日程内容",
        }
        record = await service.submit("拍一张自拍", "text-prompt", "private", snapshot, preview=True)
        await service.process(record)
        assert client.planning_inputs["snapshot"] == snapshot
        assert client.check_inputs[0]["snapshot"] == snapshot
        assert "拍摄时间：2026年10月17日 18:00:00，日本时间。" in client.last_prompt
        assert "当前发型：松束双马尾。" in client.last_prompt
        assert "整个时段的日程内容" in client.last_prompt
        assert "图2（当前穿搭：校服）" in client.last_prompt
        assert "所属日程日期" not in client.last_prompt
        assert "之后才到的地点" not in client.last_prompt
        assert "仅归档的道具设定" not in client.last_prompt
        assert '"captured_at"' not in client.last_prompt
        assert "sha256" not in client.last_prompt and "reference-1.png" not in client.last_prompt
        assert record["references"][0]["sha256"]
    asyncio.run(run())


def test_expression_is_in_actual_image_request_and_checker_without_inventing_snapshot_facts(setup):
    async def run():
        service, _, client, _ = setup
        expression = {"visible": True, "intent": "轻松", "mouth": "嘴唇自然闭合，嘴角轻微上扬，不露齿",
                      "basis": "对话里已经确认她刚坐定，尚未拿到试卷"}
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self", "identity_reference": "head",
                                 "expression": expression}
        record = await service.submit("拍一张课前自拍", "expression", "private", dict(SNAPSHOT), preview=True)
        await service.process(record)
        assert expression["mouth"] in client.last_prompt
        assert expression["basis"] not in client.last_prompt
        assert record["snapshot"] == SNAPSHOT
        assert client.check_inputs[0]["plan"]["expression"] == expression
        assert "嘴部开合" in next(ref["role"] for ref in record["references"] if ref["kind"] == "identity")
        assert [call["stage"] for call in record["model_calls"]] == ["planning", "generating", "checking"]
        assert record["status"] == "preview"
    asyncio.run(run())


def test_expression_is_preserved_through_safety_rewrite(setup):
    async def run():
        service, _, client, _ = setup
        mouth = "嘴唇自然闭合，嘴角放松"
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self", "expression": {
            "visible": True, "intent": "平静", "mouth": mouth, "basis": "当前对话平静，正在休息",
        }}
        client.image = AsyncMock(side_effect=[module.GenerationRejected("blocked"), (png((1024, 1536)), {})])
        record = await service.submit("拍一张自拍", "expression-rewrite", "private", dict(SNAPSHOT), preview=True)
        await service.process(record)
        assert client.image.await_count == 2
        assert all(mouth in call.args[1] for call in client.image.await_args_list)
        assert client.rewrite_inputs[0]["plan"]["expression"]["mouth"] == mouth
        assert record["status"] == "preview"
    asyncio.run(run())


@pytest.mark.parametrize("expression,contains", [
    (None, True),
    ({"visible": "false", "intent": "", "mouth": "", "basis": ""}, True),
    ({"visible": True, "intent": "平静", "mouth": "闭口", "basis": "休息"}, False),
    ({"visible": True, "intent": "平静", "mouth": "", "basis": "休息"}, True),
    ({"visible": False, "intent": "平静", "mouth": "闭口", "basis": "休息"}, True),
])
def test_invalid_expression_stops_before_generation(setup, expression, contains):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"contains_mittes": contains, "expression": expression}
        record = await service.submit("看看", "invalid-expression", "private", dict(SNAPSHOT), preview=True)
        with pytest.raises(ValueError, match="表情"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_unseen_mouth_does_not_add_expression_or_change_framing(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = {"contains_mittes": True, "target_kind": "self", "prompt": "镜头只拍袖口和一只手",
                                 "face_cover": {"method": "out_of_frame", "detail": "只拍袖口和手，头部不入画"}}
        record = await service.submit("看看袖口", "expression-invisible", "private", dict(SNAPSHOT), preview=True)
        await service.process(record)
        scene = client.last_prompt.split("本次拍摄：\n", 1)[1].split("\n\n事实依据", 1)[0]
        assert scene.endswith("画面描述：镜头只拍袖口和一只手")
        assert "遮脸：头部不入画——只拍袖口和手，头部不入画" in scene
        assert "可见表情" not in scene
        assert record["status"] == "preview"
    asyncio.run(run())


def test_safety_rejection_rewrites_once_then_checks_before_sending(setup):
    async def run():
        service, ctx, client, _ = setup
        client.image = AsyncMock(side_effect=[module.GenerationRejected("safety blocked"), (png((1024, 1536)), {})])
        record = await service.submit("拍杯子", "rewrite", "private", dict(SNAPSHOT))
        await service.process(record)
        assert record["status"] == "sent" and client.image.await_count == 2
        assert client.rewrite_calls == 1
        first, second = client.image.await_args_list
        assert first.args[2] == second.args[2] == client.rewrite_inputs[1]
        assert client.rewrite_result["prompt"] in second.args[1]
        assert record["generation_rejections"][0]["prompt"] == first.args[1]
        assert record["prompt_optimization"]["original_scene_prompt"] == "手机俯拍杯子"
        assert record["prompt_optimization"]["status"] == "generated"
        assert record["images"][0]["check"]["passed"] is True
        assert record["snapshot"] == SNAPSHOT and record["quota"]["used"] == 1
        calls = record["model_calls"]
        assert [call["stage"] for call in calls] == ["planning", "generating", "optimization", "generating", "checking"]
        assert calls[1]["status"] == "failed" and calls[1]["error"]["type"] == "GenerationRejected"
        assert calls[0]["output"]["result"]["prompt"] == "手机俯拍杯子"
        assert calls[2]["input"]["data"]["plan"]["prompt"] == "手机俯拍杯子"
        assert calls[3]["input"]["text"] == second.args[1]
        ctx.send.image.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("later_repair", [False, True])
def test_safety_rewrite_budget_is_shared_with_later_image_repair(setup, later_repair):
    async def run():
        service, ctx, client, _ = setup
        replies = [module.GenerationRejected("blocked")]
        if later_repair:
            replies.append((png((1024, 1536)), {}))
            client.checks = [{"passed": False, "issues": ["主体错误"], "repairable": True, "description": "杯子"}]
        replies.append(module.GenerationRejected("blocked again"))
        client.image = AsyncMock(side_effect=replies)
        record = await service.submit("拍杯子", "bounded-rewrite", "private", dict(SNAPSHOT))
        with pytest.raises(module.GenerationRejected):
            await service.process(record)
        assert client.image.await_count == (3 if later_repair else 2)
        assert client.rewrite_calls == 1 and len(record["generation_rejections"]) == 2
        ctx.send.image.assert_not_awaited()
    asyncio.run(run())


def test_first_safety_rejection_during_image_repair_keeps_repair_issues(setup):
    async def run():
        service, ctx, client, _ = setup
        image = png((1024, 1536))
        client.image = AsyncMock(side_effect=[(image, {}), module.GenerationRejected("blocked repair"), (image, {})])
        client.checks = [
            {"passed": False, "issues": ["背景反射中有鼻子及以上面部"], "repairable": True, "description": "杯子"},
            {"passed": True, "issues": [], "repairable": False, "description": "修正背景后的杯子"},
        ]
        record = await service.submit("拍杯子", "repair-rewrite", "private", dict(SNAPSHOT))
        await service.process(record)
        assert record["status"] == "sent" and client.image.await_count == 3 and client.rewrite_calls == 1
        retry = client.image.await_args_list[-1]
        assert "背景反射中有鼻子及以上面部" in retry.args[1]
        assert "最后一张参考图是待修正的上一版照片" in retry.args[1]
        assert retry.args[2][-1] == image
        assert len(record["images"]) == 2 and record["final_image"] == "attempt-2.png"
        ctx.send.image.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("rewrite,error", [
    ({"retry_allowed": False, "reason": "原始请求不适宜生成", "prompt": "不得使用"}, module.GenerationRejected),
    ({"retry_allowed": "true", "reason": "无效类型", "prompt": "不得使用"}, ValueError),
    ({"retry_allowed": True, "reason": "", "prompt": "不得使用"}, ValueError),
    ({"retry_allowed": True, "reason": "普通生活照", "prompt": ""}, ValueError),
])
def test_declined_or_invalid_rewrite_never_generates_again(setup, rewrite, error):
    async def run():
        service, ctx, client, _ = setup
        client.image = AsyncMock(side_effect=module.GenerationRejected("blocked"))
        client.rewrite_result = rewrite
        record = await service.submit("拍杯子", "declined-rewrite", "private", dict(SNAPSHOT))
        with pytest.raises(error):
            await service.process(record)
        client.image.assert_awaited_once()
        assert client.rewrite_calls == 1 and record["plan"]["prompt"] == "手机俯拍杯子"
        ctx.send.image.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("error", [module.GenerationUnknown("timeout"), RuntimeError("invalid key")])
def test_uncertain_generation_and_non_safety_errors_never_rewrite(setup, error):
    async def run():
        service, ctx, client, _ = setup
        client.image = AsyncMock(side_effect=error)
        record = await service.submit("拍杯子", "no-rewrite", "private", dict(SNAPSHOT))
        with pytest.raises(type(error)):
            await service.process(record)
        client.image.assert_awaited_once()
        assert client.rewrite_calls == 0 and "prompt_optimization" not in record
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
        client.use_previous = True
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "更近的杯子"}]
        await service.process(followup)
        assert followup["previous_photo"]["id"] == first["id"]
        assert followup["previous_photo"]["snapshot"]["outfit"] == SNAPSHOT["outfit"]
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
        assert "replyer" not in result["content"] and "选择 reply" in result["content"] and "不要重试" in result["content"]
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
            assert "replyer" not in feedback["intent"] and "选择 reply" in feedback["intent"] and "不要重试" in feedback["intent"]
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
        record = await service.submit("看看你的完整正脸和眼睛", "face", "private", SNAPSHOT)
        await service.process(record)
        assert record["status"] == "declined"
        assert record["decline"]["reason_code"] == "self_privacy"
        assert "高中生" in record["decline"]["reason"]
        assert "眼睛等上半张脸或完整正脸" in record["decline"]["reason"]
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


def test_previous_photo_is_taken_when_processing_starts_not_at_submit(setup):
    """11-04：拍脚照在穿搭照发出前 70ms 提交，提交时取会拿到更早的自拍。"""
    async def run():
        service, _, client, _ = setup
        first = await service.submit("拍自拍", "selfie", "private", dict(SNAPSHOT))
        await service.process(first)
        outfit = await service.submit("看看穿搭", "outfit", "private", dict(SNAPSHOT))
        feet = await service.submit("拍脚", "feet", "private", dict(SNAPSHOT))
        assert feet["previous_photo"] is None
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "穿搭"}]
        await service.process(outfit)
        assert outfit["previous_photo"]["id"] == first["id"]
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "脚"}]
        await service.process(feet)
        assert feet["previous_photo"]["id"] == outfit["id"]
        assert client.planning_inputs["previous_photo"]["id"] == outfit["id"]
    asyncio.run(run())


def test_delivery_note_names_the_request_without_repeating_description(setup):
    async def run():
        service, ctx, _, _ = setup
        ctx.message.get_by_id.return_value = {
            "session_id": "private", "platform": "qq", "account_id": "bot",
            "message_info": {"user_info": {"user_id": "user", "user_nickname": "田晓钟", "user_cardname": ""}},
            "processed_plain_text": "[回复了Mittes的消息: 照片里连脚都没拍到哦] @Mittes 拍一张看看",
        }
        record = await service.submit("群友想看脚部", "-1464541256", "private", dict(SNAPSHOT))
        await service.process(record)
        call = ctx.maisaka.context.append.await_args
        note = "↑ 这张是给田晓钟拍的，回应「拍一张看看」(msg_id=-1464541256)"
        assert call.kwargs["segments"] == [{"type": "text", "data": note}]
        assert call.kwargs["visible_text"] == note
        assert call.kwargs["message_id"] == "camera-delivery:" + record["id"]
        assert record["description"] not in note
    asyncio.run(run())


def test_group_card_is_preferred_and_long_requests_are_truncated():
    record = {"requester_name": "", "source_msg_id": "1", "instruction": "看看", "source_text": "@Mittes " + "很" * 40}
    assert module.pending_line(record) == "正在拍摄：群友要的「" + "很" * 30 + "…」（拍好会自动发出）"
    record["requester_name"] = "阿铭"
    record["source_text"] = "@Mittes"
    assert module.pending_line(record) == "正在拍摄：阿铭要的「看看」（拍好会自动发出）"


def test_in_progress_lists_only_unfinished_tasks_of_this_stream(setup):
    async def run():
        service, _, client, _ = setup
        done = await service.submit("拍杯子", "done", "private", dict(SNAPSHOT))
        await service.process(done)
        first = await service.submit("拍窗外", "a", "private", dict(SNAPSHOT))
        second = await service.submit("拍脚", "b", "private", dict(SNAPSHOT))
        assert [item["id"] for item in service.store.in_progress("private")] == [first["id"], second["id"]]
        assert service.store.in_progress("another-chat") == []
    asyncio.run(run())


def test_planner_block_lists_photos_still_being_taken(setup, monkeypatch):
    async def run():
        service, _, _, _ = setup
        plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
        plugin = plugin_module.ADayWithMittesPlugin()
        plugin._camera = service
        monkeypatch.setattr(plugin, "_current", lambda: (None, None, None, None))
        monkeypatch.setattr(plugin, "_planner_block", lambda *args: "【Mittes 此刻】\n所在：厨房")
        monkeypatch.setattr(plugin, "_with_calendar_suffix", lambda item, day: item)
        monkeypatch.setattr(plugin, "_topic_gate", AsyncMock(return_value=(True, "")))
        items = [{"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "时间：23:33"}]}]

        def injected(result):
            return result["modified_kwargs"]["items"][1]["parts"][0]["text"]

        result = await plugin.handle_planner_before_request(items=items, session_id="private")
        assert injected(result) == "【Mittes 此刻】\n所在：厨房"
        await service.submit("群友想看脚部", "feet", "private", dict(SNAPSHOT))
        result = await plugin.handle_planner_before_request(items=items, session_id="private")
        assert injected(result) == "【Mittes 此刻】\n所在：厨房\n正在拍摄：群友要的「看看杯子」（拍好会自动发出）"
    asyncio.run(run())


def test_provider_comes_from_shared_model_catalog(tmp_path, monkeypatch):
    """主程序 1.4.0 把供应商迁入宿主公共模型目录，相机必须从那里读地址与密钥。"""
    from importlib import import_module

    client = import_module(module.__name__.rsplit(".", 1)[0] + ".client")
    catalog = tmp_path / "model_catalog.toml"
    catalog.write_text('[[api_providers]]\nname = "CPA"\nbase_url = "http://cpa/v1/"\napi_key = "k"\n')
    monkeypatch.setenv(client.SHARED_MODEL_CATALOG_ENV, str(catalog))
    assert client.read_provider("CPA") == ("http://cpa/v1", "k")
    with pytest.raises(ValueError, match="未配置供应商"):
        client.read_provider("missing", catalog)
    monkeypatch.delenv(client.SHARED_MODEL_CATALOG_ENV)
    with pytest.raises(ValueError, match="MAIBOT_SHARED_MODEL_CATALOG"):
        client.shared_model_catalog_path()


SHARE_OPTION = {"logical_day": "2026-11-05", "slot": "17:00-19:00", "title": "放学后的店", "place": "一楼营业区", "places": [],
                "skeleton_place": "一楼营业区", "weather": "大雨", "outfit": "黑白女仆装", "outfit_details": {}, "outfit_optional": "",
                "story": "和 Niya 一起看店", "topic": "", "company": "客人、Sweyn、Niya", "helpers": ["Sweyn", "Niya"]}


def _share_plan(mode="share", **share):
    return {"mode": mode, "contains_mittes": True, "target_kind": "self", "identity_reference": "head",
            "shot": {"method": share.pop("method", "helper"), "phone": "Niya 在两米外举着手机", "height": "胸口", "angle": "平视", "distance": "两米"},
            "face_cover": {"method": "shown", "detail": "侧脸低头吃便当"},
            "share": {"source": "Niya", "candid": "unaware", "day": "2026-11-05", "slot": "17:00-19:00", "taken_at": "18:10", "caption": "傍晚看店时 Niya 帮我拍的", **share}}


@pytest.mark.parametrize("share, message", [
    ({"method": "front_selfie"}, "helper"),
    ({"candid": "posed"}, "拍法无效"),
    ({"slot": "09:00-10:00"}, "候选"),
    ({"day": "2026-11-04"}, "候选"),
    ({"taken_at": "16:00"}, "时段内"),
    ({"source": "梧桐"}, "梧桐 不在场"),
    ({"caption": " "}, "来历"),
])
def test_share_rejects_unavailable_sources(setup, share, message):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan(**share)
        snapshot = {**SNAPSHOT, "captured_at": "2026-11-05T23:30:00+09:00", "helpers": [], "share_options": [SHARE_OPTION]}
        record = await service.submit("想看脸", "share-bad-" + message, "private", snapshot, preview=True)
        with pytest.raises(ValueError, match=message):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_share_requires_helper_present(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan()
        snapshot = {**SNAPSHOT, "captured_at": "2026-11-05T23:30:00+09:00", "helpers": [], "share_options": [{**SHARE_OPTION, "helpers": ["Sweyn"]}]}
        record = await service.submit("想看脸", "share-no-niya", "private", snapshot, preview=True)
        with pytest.raises(ValueError, match="Niya 不在场"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_share_uses_selected_slot_and_notes_source(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan()
        snapshot = {**SNAPSHOT, "captured_at": "2026-11-05T23:30:00+09:00", "helpers": [], "share_options": [SHARE_OPTION]}
        record = await service.submit("想看脸", "share-ok", "private", snapshot, preview=True)
        await service.process(record)
        assert record["snapshot"]["captured_at"] == "2026-11-05T18:10:00+09:00"
        assert record["snapshot"]["outfit"] == "黑白女仆装" and record["request_snapshot"]["captured_at"].startswith("2026-11-05T23:30")
        assert client.last_prompt.startswith("生成一张 Niya 给 Mittes 拍的二次元生活照")
        assert "拍摄者：Niya。拍法：她没察觉" in client.last_prompt and "镜自拍等自然方式" not in client.last_prompt
        assert record["share"] == {"kind": "share", "source": "Niya", "candid": "unaware", "day": "2026-11-05", "slot": "17:00-19:00", "taken_at": "18:10", "caption": "傍晚看店时 Niya 帮我拍的"}
        assert "脸部：完整入镜" in client.last_prompt
        note = module.delivery_note(record)
        assert "Niya在 2026-11-05 18:10帮 Mittes 拍的" in note and "照片的来历（回复时照此说明）：傍晚看店时 Niya 帮我拍的" in note
    asyncio.run(run())


def test_assist_uses_helper_beside_her_now(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan("assist", source="Sweyn", candid="requested", day="", slot="", taken_at="", caption="刚好店长在旁边，就请店长帮我拍了一张")
        snapshot = {**SNAPSHOT, "slot": "18:00-19:00", "helpers": ["Sweyn"], "share_options": []}
        record = await service.submit("想看看你", "assist-ok", "private", snapshot, preview=True)
        await service.process(record)
        assert record["snapshot"]["captured_at"] == SNAPSHOT["captured_at"] and "request_snapshot" not in record
        assert record["share"]["kind"] == "assist" and record["share"]["source"] == "Sweyn"
        assert client.last_prompt.startswith("生成一张 Sweyn 给 Mittes 拍的二次元生活照")
        assert "Sweyn刚刚帮 Mittes 拍的" in module.delivery_note(record)
    asyncio.run(run())


@pytest.mark.parametrize("share, helpers, message", [
    ({"source": "Niya"}, ["Sweyn"], "此刻身边没有 Niya"),
    ({"candid": "unaware"}, ["Sweyn"], "当场帮拍"),
])
def test_assist_rejects_absent_helper_or_candid_shot(setup, share, helpers, message):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan("assist", **{"source": "Sweyn", "candid": "requested", "slot": "", "taken_at": "", **share})
        snapshot = {**SNAPSHOT, "slot": "18:00-19:00", "helpers": helpers, "share_options": []}
        record = await service.submit("想看看你", "assist-bad-" + message, "private", snapshot, preview=True)
        with pytest.raises(ValueError, match=message):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_shoot_cannot_use_helper_or_show_face(setup):
    async def run():
        service, _, client, _ = setup
        client.plan_overrides = _share_plan("shoot")
        record = await service.submit("自拍", "shoot-helper", "private", {**SNAPSHOT, "helpers": [], "share_options": []}, preview=True)
        with pytest.raises(ValueError, match="现拍照片不能"):
            await service.process(record)
    asyncio.run(run())


def test_helpers_of_skips_strangers_and_alone():
    helpers_of = importlib.import_module("plugins.01_a_day_with_mittes.camera.prompt_context").helpers_of
    assert helpers_of("客人、Sweyn、Niya") == ["Sweyn", "Niya"]
    assert helpers_of("一个人") == [] and helpers_of("陌生人") == []
    assert helpers_of("前桌同学、同学") == ["前桌同学", "同学"]
    assert helpers_of("老师、同学") == ["同学"] and helpers_of("同学、班主任") == ["同学"]


def _shown_selfie():
    plan = _share_plan("shoot", source="", candid="", slot="", taken_at="", caption="")
    plan["shot"]["method"] = "front_selfie"
    return plan


@pytest.mark.parametrize("user, ok", [("923856490", True), ("guest", False)])
def test_only_sweyn_gets_face_shown_selfie(setup, user, ok):
    async def run():
        service, ctx, client, _ = setup
        ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = user
        client.plan_overrides = _shown_selfie()
        record = await service.submit("想看看你", "shown-" + user, "private", {**SNAPSHOT, "helpers": [], "share_options": []})
        assert record["requester_persona"] == ("Sweyn" if ok else "")
        if ok:
            await service.process(record)
            assert client.last_prompt.startswith(module.STYLE_FACE_SHOWN.split("\n")[0])
            assert "这张是发给店长 Sweyn 的照片，脸部可以完整入镜" in client.last_prompt
        else:
            with pytest.raises(ValueError, match="只有发给 Sweyn"):
                await service.process(record)
    asyncio.run(run())


def test_requester_cannot_be_the_photographer(setup):
    async def run():
        service, ctx, client, _ = setup
        ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = "923856490"
        client.plan_overrides = _share_plan("assist", source="Sweyn", candid="requested", slot="", taken_at="")
        record = await service.submit("想看看你", "assist-self", "private", {**SNAPSHOT, "slot": "18:00-19:00", "helpers": ["Sweyn"], "share_options": []})
        with pytest.raises(ValueError, match="提问的人自己"):
            await service.process(record)
        assert client.image_calls == 0
    asyncio.run(run())


def test_sweyn_never_gets_self_privacy(setup):
    async def run():
        service, ctx, client, _ = setup
        ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = "923856490"
        client.plan_overrides = {"feasible": False, "target_kind": "self", "rejection_code": "self_privacy", "reason": "隐私"}
        record = await service.submit("想看脸", "sweyn-privacy", "private", SNAPSHOT)
        with pytest.raises(ValueError, match="对 Sweyn 不使用 self_privacy"):
            await service.process(record)
    asyncio.run(run())


def test_no_photo_decline_just_says_there_is_none(setup):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"feasible": False, "target_kind": "self", "rejection_code": "no_photo", "reason": "早上一个人在卧室"}
        record = await service.submit("早上的你有照片吗", "no-photo", "private", SNAPSHOT)
        await service.process(record)
        assert record["decline"]["reason_code"] == "no_photo"
        assert record["decline"]["reason"] == "Mittes 手上没有这种照片。"
        assert "直接告诉对方没有那时候的照片" in record["decline"]["planner_guidance"]
        assert client.image_calls == 0
    asyncio.run(run())


def test_share_picks_an_earlier_day_when_today_has_none(setup):
    async def run():
        service, _, client, _ = setup
        yesterday = {**SHARE_OPTION, "logical_day": "2026-11-04", "slot": "17:00-19:00", "story": "昨天的晚班"}
        client.plan_overrides = _share_plan(day="2026-11-04")
        snapshot = {**SNAPSHOT, "captured_at": "2026-11-05T12:00:00+09:00", "helpers": [], "share_options": [yesterday]}
        record = await service.submit("你在店里的照片", "share-yesterday", "private", snapshot, preview=True)
        await service.process(record)
        assert record["snapshot"]["captured_at"] == "2026-11-04T18:10:00+09:00" and record["snapshot"]["story"] == "昨天的晚班"
    asyncio.run(run())


def test_face_photo_daily_limit_per_requester(setup):
    async def run():
        service, _, client, _ = setup
        snapshot = {**SNAPSHOT, "slot": "18:00-19:00", "helpers": ["Niya"], "share_options": []}
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "Niya 帮拍"}] * 2
        for index in range(3):
            client.plan_overrides = _share_plan("assist", source="Niya", candid="requested", day="", slot="", taken_at="")
            record = await service.submit("想看看你", f"face-{index}", "private", snapshot)
            if index < 2:
                await service.process(record)
                assert record["status"] == "sent"
            else:
                with pytest.raises(ValueError, match="露脸照已到上限"):
                    await service.process(record)
                assert client.planning_inputs["face_photos_left"] == 0
    asyncio.run(run())


def test_reuse_resends_an_earlier_unmasked_photo_without_generation(setup):
    async def run():
        service, ctx, client, _ = setup
        first = await service.submit("拍杯子", "earlier-cup", "private", SNAPSHOT)
        await service.process(first)
        assert first["status"] == "sent" and client.image_calls == 1
        client.plan_overrides = {"mode": "reuse", "reuse": {"photo_id": first["id"]}}
        record = await service.submit("之前那张杯子再发一下", "reuse-cup", "private", SNAPSHOT)
        await service.process(record)
        assert [p["id"] for p in client.planning_inputs["earlier_photos"]] == [first["id"]]
        assert record["status"] == "sent" and client.image_calls == 1
        assert record["reuse"]["id"] == first["id"] and record["images"][0]["reused_from"] == first["id"]
        assert (service.store.root / record["id"] / "attempt-1.png").read_bytes() == (service.store.root / first["id"] / first["final_image"]).read_bytes()
        assert "之前" in module.delivery_note(record) and "拍过的照片，再发给" in module.delivery_note(record)
    asyncio.run(run())


def test_reuse_only_picks_from_listed_unmasked_photos(setup):
    async def run():
        service, _, client, _ = setup
        face = await service.submit("想看看你", "face-photo", "private", {**SNAPSHOT, "slot": "18:00-19:00", "helpers": ["Niya"], "share_options": []})
        client.plan_overrides = _share_plan("assist", source="Niya", candid="requested", day="", slot="", taken_at="")
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "Niya 帮拍"}]
        await service.process(face)
        assert face["status"] == "sent"
        client.plan_overrides = {"mode": "reuse", "reuse": {"photo_id": face["id"]}}
        record = await service.submit("之前那张再发一下", "reuse-face", "private", SNAPSHOT)
        with pytest.raises(ValueError, match="不在候选里"):
            await service.process(record)
        assert client.planning_inputs["earlier_photos"] == []
    asyncio.run(run())


def test_chat_photo_limit_counts_everyone_but_sweyn(tmp_path):
    store_module = importlib.import_module("plugins.01_a_day_with_mittes.camera.store")
    store = store_module.CameraStore(tmp_path / "camera")
    store.initialize()

    def request(index, user, persona=""):
        return {"stream_id": "group", "source_msg_id": f"m{index}", "requester": {"platform": "qq", "user_id": user},
                "requester_persona": persona, "preview": False, "snapshot": {}}
    for index in range(4):
        store.create(request(index, "923856490", "Sweyn"))
    for index, user in enumerate(["a", "a", "b", "b", "c"]):
        store.create(request(10 + index, user))
    with pytest.raises(store_module.PhotoQuotaExceeded, match="这个聊天今天的拍照名额已占满（5/5"):
        store.create(request(20, "d"))
    store.create(request(21, "923856490", "Sweyn"))


def test_photo_origin_names_who_took_it():
    assert module.photo_origin({"share": {"source": "Niya", "caption": "美术课 Niya 帮我拍的。"}}) == "是Niya帮 Mittes 拍的照片，来历：美术课 Niya 帮我拍的"
    assert "之前（2026-11-08 12:10，三楼卧室）拍过" in module.photo_origin({"reuse": {"taken_at": "2026-11-08 12:10", "place": "三楼卧室"}})
    assert module.photo_origin({}) == "是 Mittes 刚用手机拍的"


def test_face_photo_limit_also_counts_per_chat(setup):
    async def run():
        service, ctx, client, _ = setup
        snapshot = {**SNAPSHOT, "slot": "18:00-19:00", "helpers": ["Niya"], "share_options": []}
        client.checks = [{"passed": True, "issues": [], "repairable": False, "description": "Niya 帮拍"}] * 2
        for index, user in enumerate(["a", "b", "c"]):
            ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = user
            client.plan_overrides = _share_plan("assist", source="Niya", candid="requested", day="", slot="", taken_at="")
            record = await service.submit("想看看你", f"chat-face-{index}", "private", snapshot)
            if index < 2:
                await service.process(record)
                assert record["status"] == "sent"
            else:
                with pytest.raises(ValueError, match="露脸照已到上限"):
                    await service.process(record)
                assert client.planning_inputs["face_photos_left"] == 0
    asyncio.run(run())


def test_named_helper_absent_is_declined_with_their_name(setup):
    async def run():
        service, ctx, client, _ = setup
        client.plan_overrides = {"feasible": False, "target_kind": "self", "rejection_code": "helper_absent", "absent_helper": "Niya", "reason": "不在"}
        record = await service.submit("让 Niya 帮你拍一张", "absent-niya", "private", {**SNAPSHOT, "helpers": ["Sweyn"], "share_options": []})
        await service.process(record)
        assert record["decline"]["reason_code"] == "helper_absent" and record["decline"]["reason"] == "Niya 现在不在我身边。"
        intent = ctx.maisaka.proactive.trigger.call_args.kwargs["intent"]
        assert "Niya 现在不在我身边。" in intent and client.image_calls == 0
        client.plan_overrides["absent_helper"] = "Sweyn"
        record = await service.submit("让 Sweyn 帮你拍一张", "absent-sweyn", "private", {**SNAPSHOT, "helpers": ["Sweyn"], "share_options": []})
        with pytest.raises(ValueError, match="其实在场"):
            await service.process(record)
    asyncio.run(run())
