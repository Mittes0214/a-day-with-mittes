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
SNAPSHOT = {"outfit": "校服", "place": "家", "captured_at": "2026-10-17T18:00:00+09:00"}


def png():
    output = BytesIO()
    Image.new("RGB", (512, 512), "white").save(output, format="PNG")
    return output.getvalue()


class FakeClient:
    image_calls = 0
    checks = []
    use_previous = False
    last_refs = []

    async def json(self, model, system, data, images=None):
        if images:
            return self.checks.pop(0), {}
        return {"feasible": True, "reason": "", "subject": "杯子", "scene_type": "indoor", "include_outfit": False,
                "framing": "俯拍", "visible_details": "杯子", "lighting": "室内灯", "prompt": "手机俯拍杯子",
                "use_previous_photo": self.use_previous}, {}

    async def image(self, settings, prompt, images):
        self.image_calls += 1
        self.last_refs = images
        return png(), {}

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    plugin = tmp_path / "host/plugins/day"
    refs = plugin / "camera/references"
    refs.mkdir(parents=True)
    data = png()
    (refs / "style.png").write_bytes(data)
    entry = {"file": "style.png", "sha256": hashlib.sha256(data).hexdigest()}
    (refs / "references.json").write_text(json.dumps({"version": "v1", "references": [entry, entry]}))
    ctx = SimpleNamespace(
        message=SimpleNamespace(get_by_id=AsyncMock(return_value={
            "session_id": "private", "message_info": {"user_info": {"user_id": "user"}},
            "account_id": "bot", "processed_plain_text": "看看杯子",
        }), get_recent=AsyncMock(return_value=[])),
        send=SimpleNamespace(image=AsyncMock(return_value={"sent": True, "message_id": "sent-1"})),
        maisaka=SimpleNamespace(context=SimpleNamespace(append=AsyncMock(return_value={"success": True}))),
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


@pytest.mark.parametrize("invalid", ["wrong_stream", "bot", "disabled"])
def test_invalid_request_never_creates_task(setup, invalid):
    async def run():
        service, ctx, _, _ = setup
        if invalid == "wrong_stream":
            ctx.message.get_by_id.return_value["session_id"] = "other"
        elif invalid == "bot":
            ctx.message.get_by_id.return_value["message_info"]["user_info"]["user_id"] = "bot"
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


def test_reference_corruption_stops_generation(setup):
    async def run():
        service, ctx, client, refs = setup
        (refs / "style.png").write_bytes(b"changed")
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
