"""每日额度必须由可信消息身份控制，并在并发、去重和重启下保持一致。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import importlib
import json
import sqlite3

import pytest


module = importlib.import_module("plugins.01_a_day_with_mittes.camera.store")


def request(index, *, user="guest", platform="qq", preview=False):
    return {
        "stream_id": f"chat-{index % 2}", "source_msg_id": f"message-{index}",
        "requester": {"platform": platform, "user_id": user}, "preview": preview,
        # 计数不依赖可能过期或由调用方传入的日程日期。
        "snapshot": {"logical_day": "2000-01-01"},
    }


@pytest.fixture
def store(tmp_path):
    result = module.CameraStore(tmp_path / "camera")
    result.initialize()
    return result


def test_parallel_requests_across_streams_cannot_exceed_three(store):
    stores = [store, module.CameraStore(store.root)]

    def submit(index):
        try:
            return stores[index % 2].create(request(index))
        except module.PhotoQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(12)))
    admitted = [result for result in results if result]
    assert len(admitted) == 3
    assert sorted(result["quota"]["remaining"] for result in admitted) == [0, 1, 2]
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
        assert con.execute("SELECT used FROM daily_usage").fetchone()[0] == 3


def test_duplicate_still_returns_existing_task_when_quota_is_full(store):
    first = store.create(request(0))
    store.create(request(1))
    store.create(request(2))
    assert store.create(request(0)) == first
    with pytest.raises(module.PhotoQuotaExceeded):
        store.create(request(3))


def test_only_qq_owner_account_is_exempt(store):
    for index in range(6):
        owner = store.create(request(index, user="923856490"))
        assert owner["quota"]["exempt"] is True
    for index in range(3):
        result = store.create(request(index + 10, user="923856490", platform="telegram"))
        assert result["quota"]["exempt"] is False
    with pytest.raises(module.PhotoQuotaExceeded):
        store.create(request(13, user="923856490", platform="telegram"))


def test_two_users_have_independent_limits(store):
    for index in range(3):
        store.create(request(index))
        store.create(request(index + 10, user="another"))
    for user in ["guest", "another"]:
        with pytest.raises(module.PhotoQuotaExceeded):
            store.create(request(50, user=user))


def test_preview_without_chat_identity_does_not_charge_users(store):
    for index in range(6):
        payload = {**request(index, preview=True), "requester": None}
        assert store.create(payload)["quota"]["exempt"] is True
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM daily_usage").fetchone()[0] == 0


def test_natural_day_resets_at_tokyo_midnight_not_schedule_day(store, monkeypatch):
    moment = datetime.fromisoformat("2026-10-17T14:59:59+00:00")
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda tz: moment.astimezone(tz)))
    for index in range(3):
        assert store.create(request(index))["quota"]["day"] == "2026-10-17"
    with pytest.raises(module.PhotoQuotaExceeded) as error:
        store.create(request(3))
    assert error.value.timezone == "Asia/Tokyo"
    assert "东京时间每天零点重置" in str(error.value)
    moment = datetime.fromisoformat("2026-10-17T15:00:00+00:00")
    result = store.create(request(3))
    assert result["quota"]["day"] == "2026-10-18"
    assert result["quota"]["timezone"] == "Asia/Tokyo"
    assert result["quota"]["remaining"] == 2
    assert store.create(request(0))["quota"]["day"] == "2026-10-17"
    # 台北零点已经是东京凌晨一点，不能再次重置。
    moment = datetime.fromisoformat("2026-10-17T16:00:00+00:00")
    assert store.create(request(4))["quota"]["remaining"] == 1


@pytest.mark.parametrize("status", ["failed", "declined", "send_failed", "generation_unknown", "delivery_unknown", "interrupted"])
def test_unsent_task_releases_slot_once(store, status):
    for index in range(3):
        record = store.create(request(index))
        if index == 0:
            record["status"] = status
            store.save(record)
            store.save(record)
    assert store.create(request(3))["quota"]["remaining"] == 0
    with pytest.raises(module.PhotoQuotaExceeded):
        store.create(request(4))


def test_sent_task_keeps_slot_after_restart(store):
    for index in range(3):
        record = store.create(request(index))
        record["status"] = "sent"
        store.save(record)
    restarted = module.CameraStore(store.root)
    restarted.initialize()
    restarted.recover()
    with pytest.raises(module.PhotoQuotaExceeded):
        restarted.create(request(3))


def test_restart_reconciles_old_failed_charges(store):
    records = [store.create(request(index)) for index in range(3)]
    with sqlite3.connect(store.db) as con:
        for record in records:
            record["status"] = "failed"
            con.execute("UPDATE tasks SET status=?, record=? WHERE id=?",
                        ("failed", json.dumps(record), record["id"]))
    restarted = module.CameraStore(store.root)
    restarted.initialize()
    for index in range(3, 6):
        assert restarted.create(request(index))["quota"]["used"] == index - 2
    with pytest.raises(module.PhotoQuotaExceeded):
        restarted.create(request(6))


def test_recovery_releases_incomplete_tasks(store):
    for index, status in enumerate(["generating", "sending"]):
        record = store.create(request(index))
        record["status"] = status
        store.save(record)
    restarted = module.CameraStore(store.root)
    restarted.initialize()
    restarted.recover()
    assert restarted.create(request(2))["quota"]["remaining"] == 2


def test_unverified_identity_cannot_create_a_real_task(store):
    with pytest.raises(ValueError, match="真实消息验证"):
        store.create({**request(0), "requester": None})
    assert store.next_queued() is None


def test_failed_queue_insert_rolls_back_charge(store):
    with pytest.raises(TypeError):
        store.create({**request(0), "unserializable": object()})
    assert store.create(request(0))["quota"]["remaining"] == 2
