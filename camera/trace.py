"""归档相机实际模型输入与输出，不记录密钥或重复保存图片。"""

from typing import Any

import asyncio
import copy
import hashlib
import json

from .prompts import CHECK, DIRECTOR, SAFE_REWRITE
from .store import CameraStore, timestamp


class CameraTraceClient:
    def __init__(self, client: Any, record: dict[str, Any], store: CameraStore) -> None:
        self.client = client
        self.record = record
        self.store = store

    async def _begin(self, stage: str, model: str, payload: dict[str, Any], images: list[bytes] | None) -> dict[str, Any]:
        candidates = self.record["references"] + self.record["images"]
        if stage == "checking":
            candidates = [self.record["images"][-1], *self.record["references"]]
        attachments = []
        for index, raw in enumerate(images or [], 1):
            digest = hashlib.sha256(raw).hexdigest()
            item = candidates[index - 1] if index <= len(candidates) and candidates[index - 1]["sha256"] == digest else next(item for item in candidates if item["sha256"] == digest)
            attachments.append({"index": index, "file": item["file"], "sha256": digest})
        calls = self.record.setdefault("model_calls", [])
        call = {"index": len(calls) + 1, "stage": stage, "model": model, "started_at": timestamp(),
                "status": "running", "input": {**copy.deepcopy(payload), "images": attachments}}
        calls.append(call)
        await asyncio.to_thread(self.store.save, self.record)
        return call

    async def _finish(self, call: dict[str, Any], output: dict[str, Any]) -> None:
        call.update(status="completed", completed_at=timestamp(), output=copy.deepcopy(output))
        await asyncio.to_thread(self.store.save, self.record)

    async def _failed(self, call: dict[str, Any], exc: BaseException) -> None:
        call.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                    completed_at=timestamp(), error={"type": type(exc).__name__, "message": str(exc)[:1200]})
        await asyncio.to_thread(self.store.save, self.record)

    async def json(self, model: str, system: str, data: dict[str, Any], images: list[bytes] | None = None) -> tuple[dict[str, Any], Any]:
        stage = {DIRECTOR: "planning", CHECK: "checking", SAFE_REWRITE: "optimization"}[system]
        call = await self._begin(stage, model, {"system": system, "text": json.dumps(data, ensure_ascii=False), "data": data}, images)
        try:
            result, usage = await self.client.json(model, system, data, images)
        except BaseException as exc:
            await self._failed(call, exc)
            raise
        await self._finish(call, {"result": result, "usage": usage})
        return result, usage

    async def image(self, settings: dict[str, Any], prompt: str, images: list[bytes]) -> tuple[bytes, dict[str, Any]]:
        stage = "repairing" if self.record["images"] else "generating"
        call = await self._begin(stage, settings["image_model"], {
            "text": prompt, "parameters": {key: settings[key] for key in ("size", "quality")},
        }, images)
        try:
            raw, result = await self.client.image(settings, prompt, images)
        except BaseException as exc:
            await self._failed(call, exc)
            raise
        await self._finish(call, {**result, "image": {
            "file": f"attempt-{len(self.record['images']) + 1}.png",
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        }})
        return raw, result

    async def close(self) -> None:
        await self.client.close()
