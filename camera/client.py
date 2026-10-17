"""CPA 的文字、视觉和参考图生图接口；密钥只在请求时读取，不进入任务归档。"""

from pathlib import Path
from typing import Any

import asyncio
import base64
import json
import tomllib

import httpx


class GenerationUnknown(RuntimeError):
    """请求可能已执行，但没有取得结果，不能盲目再次生成。"""


def read_provider(config_path: Path, name: str) -> tuple[str, str]:
    config = tomllib.loads(config_path.read_text())
    provider = next((p for p in config["api_providers"] if p["name"] == name), None)
    if provider is None:
        raise ValueError(f"model_config.toml 未配置供应商 {name}")
    base_url, key = str(provider["base_url"]).rstrip("/"), str(provider["api_key"])
    if not base_url.startswith(("http://", "https://")) or not key:
        raise ValueError("相机供应商缺少有效地址或密钥")
    return base_url, key


class CPAClient:
    def __init__(self, base_url: str, key: str, timeout: float = 300) -> None:
        self._key = key
        self.http = httpx.AsyncClient(
            base_url=base_url + "/", headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(timeout, connect=10), trust_env=False,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            try:
                response = await self.http.post(endpoint, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise GenerationUnknown(f"CPA 请求结果不明确（{type(exc).__name__}），未自动重试") from exc
            try:
                error = response.json().get("error", {}) if response.is_error else {}
            except (ValueError, AttributeError):
                error = {}
            if not isinstance(error, dict):
                error = {"message": str(error)}
            if endpoint == "images/edits" and response.status_code in {502, 504}:
                raise GenerationUnknown(f"CPA HTTP {response.status_code}，生成结果未取得，未自动重做")
            if response.status_code in {429, 502, 503, 504} and attempt == 0 and error.get("code") != "insufficient_quota":
                await asyncio.sleep(3)
                continue
            if response.is_error:
                try:
                    message = error.get("message", "请求失败")
                except (ValueError, AttributeError):
                    message = "非 JSON 错误响应"
                message = str(message).replace(self._key, "[redacted]") if self._key else str(message)
                raise RuntimeError(f"CPA HTTP {response.status_code}: {message[:600]}")
            return response.json()
        raise RuntimeError("CPA 请求未完成")

    async def json(self, model: str, system: str, data: dict[str, Any], images: list[bytes] | None = None) -> tuple[dict[str, Any], Any]:
        content: Any = json.dumps(data, ensure_ascii=False)
        if images:
            content = [{"type": "text", "text": content}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}
                for image in images
            ]
        result = await self._post("chat/completions", json={
            "model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"}, "max_tokens": 6000,
        })
        raw = result["choices"][0]["message"]["content"]
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("相机文字/视觉模型没有返回 JSON 对象")
        return obj, result.get("usage")

    async def image(self, settings: dict[str, Any], prompt: str, images: list[bytes]) -> tuple[bytes, dict[str, Any]]:
        fields = {"model": settings["image_model"], "prompt": prompt, "n": "1",
                  "size": settings["size"], "quality": settings["quality"], "output_format": "png"}
        files = [("image[]", (f"reference-{i + 1}.png", data, "image/png")) for i, data in enumerate(images)]
        result = await self._post("images/edits", data=fields, files=files)
        outputs = result.get("data", [])
        if len(outputs) != 1 or not outputs[0].get("b64_json"):
            raise ValueError("CPA 未返回一张完整的 base64 图片")
        return base64.b64decode(outputs[0]["b64_json"], validate=True), {
            "usage": result.get("usage"), "revised_prompt": outputs[0].get("revised_prompt", ""),
        }
