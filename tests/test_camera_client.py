"""接口重试范围和错误脱敏；不请求真实 CPA。"""

from pathlib import Path
from unittest.mock import AsyncMock

import asyncio
import importlib
import sys

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
module = importlib.import_module("plugins.01_a_day_with_mittes.camera.client")


def response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://test/v1/images/edits"))


@pytest.mark.parametrize("failure", ["timeout", "gateway"])
def test_uncertain_image_request_is_never_retried(failure):
    async def run():
        client = module.CPAClient("http://test/v1", "secret")
        client.http.post = AsyncMock()
        if failure == "timeout":
            client.http.post.side_effect = httpx.ReadTimeout("timeout")
        else:
            client.http.post.return_value = response(504, {"error": {"message": "timeout"}})
        try:
            with pytest.raises(module.GenerationUnknown):
                await client._post("images/edits", json={})
            client.http.post.assert_awaited_once()
        finally:
            await client.close()
    asyncio.run(run())


def test_quota_error_is_not_retried_and_key_is_redacted():
    async def run():
        client = module.CPAClient("http://test/v1", "secret")
        client.http.post = AsyncMock(return_value=response(429, {"error": {"code": "insufficient_quota", "message": "key secret has no quota"}}))
        try:
            with pytest.raises(RuntimeError) as error:
                await client._post("images/edits", json={})
            assert "secret" not in str(error.value) and "[redacted]" in str(error.value)
            client.http.post.assert_awaited_once()
        finally:
            await client.close()
    asyncio.run(run())


def test_transient_rate_limit_has_one_bounded_retry(monkeypatch):
    async def run():
        client = module.CPAClient("http://test/v1", "secret")
        client.http.post = AsyncMock(side_effect=[response(429, {"error": {"code": "rate_limit"}}), response(200, {"ok": True})])
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
        try:
            assert await client._post("images/edits", json={}) == {"ok": True}
            assert client.http.post.await_count == 2
        finally:
            await client.close()
    asyncio.run(run())
