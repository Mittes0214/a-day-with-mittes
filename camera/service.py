"""持久化后台拍照队列。每份请求只生成、检查和发送一次成品。"""

from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import base64
import hashlib
import json
import logging
import tomllib

from PIL import Image

from .client import CPAClient, GenerationUnknown, read_provider
from .prompts import CHECK, DIRECTOR, STYLE
from .store import CameraStore, timestamp


_logger = logging.getLogger("a_day_with_mittes.camera")
DEFAULTS = {
    "enabled": False, "api_provider": "CLIProxyAPI", "director_model": "gpt-6.1-sol",
    "image_model": "gpt-image-2.5-sunburst", "check_model": "gpt-6.1-sol",
    "size": "1024x1536", "quality": "high", "timeout_seconds": 300,
}


def message_text(message: dict[str, Any]) -> str:
    return str(message.get("processed_plain_text") or "").strip() or " ".join(
        str(part.get("data", "")) for part in message.get("raw_message", []) if part.get("type") == "text"
    )


def write_image(path: Path, data: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if width < 256 or height < 256 or width * height > 32_000_000 or image.format != "PNG":
            raise ValueError("生图输出格式或尺寸不符合要求")
        image.load()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"width": width, "height": height, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class PhotoService:
    def __init__(self, ctx: Any, plugin_dir: Path, data_dir: Path,
                 get_config: Callable[..., Awaitable[Any]], client_factory: Any = CPAClient) -> None:
        self.ctx = ctx
        self.plugin_dir = plugin_dir
        self.store = CameraStore(data_dir / "camera")
        self.get_config = get_config
        self.client_factory = client_factory
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize)
        await asyncio.to_thread(self.store.recover)
        self.task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def submit(self, instruction: str, source_msg_id: str, stream_id: str,
                     snapshot: dict[str, Any], *, preview: bool = False) -> dict[str, Any]:
        settings = {key: await self.get_config(f"photo.{key}", default) for key, default in DEFAULTS.items()}
        if not settings["enabled"]:
            raise ValueError("拍照功能尚未开启")
        if not instruction.strip() or len(instruction) > 2000:
            raise ValueError("instruction 必须是 1–2000 字的拍摄意图")
        if not source_msg_id or (not preview and not stream_id):
            raise ValueError("缺少原会话或真实用户消息 ID")
        context = []
        previous_photo = None
        source_text = instruction
        if not preview:
            source = await self.ctx.message.get_by_id(source_msg_id, stream_id=stream_id)
            if not isinstance(source, dict) or source.get("session_id") != stream_id:
                raise ValueError("源消息不属于当前会话或无法读取")
            user = source.get("message_info", {}).get("user_info", {})
            if not user.get("user_id") or str(user["user_id"]) == str(source.get("account_id")):
                raise ValueError("拍照必须由真实用户的消息触发")
            source_text = message_text(source)
            recent = await self.ctx.message.get_recent(stream_id, limit=12)
            if not isinstance(recent, list):
                raise RuntimeError("无法读取拍照请求的相关对话")
            for message in recent:
                text = message_text(message)
                if text:
                    context.append({"text": text[:1200], "user_id": str(message.get("message_info", {}).get("user_info", {}).get("user_id", ""))})
            previous = await asyncio.to_thread(self.store.latest_sent, stream_id)
            if previous is not None:
                previous_photo = {"id": previous["id"], "description": previous["description"],
                                  "instruction": previous["instruction"], "file": previous["final_image"]}
        return await asyncio.to_thread(self.store.create, {
            "stream_id": stream_id, "source_msg_id": source_msg_id,
            "instruction": instruction.strip(), "source_text": source_text[:2000], "context": context,
            "snapshot": snapshot, "settings": settings, "preview": preview, "prompt_version": "phone-photo-v1",
            "images": [], "references": [],
            "previous_photo": previous_photo,
        })

    async def event(self, record: dict[str, Any], stage: str, text: str, **detail: Any) -> None:
        record["status"] = stage
        record["events"].append({"at": timestamp(), "stage": stage, "text": text, **detail})
        await asyncio.to_thread(self.store.save, record)
        _logger.info("[相机] %s %s %s", record["id"], stage, text)

    async def _loop(self) -> None:
        while True:
            try:
                record = await asyncio.to_thread(self.store.next_queued)
            except Exception:
                _logger.exception("[相机] 无法读取任务队列，稍后重试")
                await asyncio.sleep(5)
                continue
            if record is None:
                await asyncio.sleep(2)
                continue
            try:
                async with asyncio.timeout(720):
                    await self.process(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = "generation_unknown" if isinstance(exc, (GenerationUnknown, TimeoutError)) else "failed"
                await self.event(record, status, f"{type(exc).__name__}: {str(exc)[:800]}")
                _logger.exception("[相机] 任务失败 %s", record["id"])
                if not record["preview"]:
                    try:
                        await self.ctx.maisaka.proactive.trigger(
                            stream_id=record["stream_id"],
                            intent="用户要求的照片这次没有成功发送，可以简短说明没拍好。不要声称已有照片，不要自动重新调用拍照工具。",
                            reason="camera_failed", metadata={"background_tool_result": True, "tool_name": "take_mittes_photo",
                                                               "tool_success": False, "task_id": record["id"]},
                        )
                    except Exception:
                        _logger.exception("[相机] 失败回填未成功 %s", record["id"])

    async def process(self, record: dict[str, Any]) -> None:
        settings = record["settings"]
        await self.event(record, "planning", "读取请求快照，设计手机取景")
        base_url, key = await asyncio.to_thread(read_provider, self.plugin_dir.parents[1] / "config/model_config.toml", settings["api_provider"])
        client = self.client_factory(base_url, key, float(settings["timeout_seconds"]))
        try:
            plan, usage = await client.json(settings["director_model"], DIRECTOR, {
                "instruction": record["instruction"], "source_text": record["source_text"],
                "context": record["context"], "snapshot": record["snapshot"],
                "previous_photo": record.get("previous_photo"),
            })
            if plan.get("feasible") is not True:
                raise ValueError("无法拍摄：" + str(plan.get("reason", "模型未确认可拍摄")))
            for field in ("subject", "framing", "visible_details", "lighting", "prompt"):
                if not isinstance(plan.get(field), str) or not plan[field].strip():
                    raise ValueError(f"拍摄方案缺少 {field}")
            if plan.get("scene_type") not in {"indoor", "outdoor"} or not isinstance(plan.get("include_outfit"), bool):
                raise ValueError("拍摄方案的场景类型或穿搭标记无效")
            record["plan"] = plan
            record["planning_usage"] = usage
            images = await asyncio.to_thread(self._references, record, plan)
            prompt = STYLE + "\n\n本次拍摄：\n" + plan["prompt"] + "\n\n事实依据：\n" + json.dumps(record["snapshot"], ensure_ascii=False)
            prompt += "\n\n参考图用途：\n" + json.dumps(record["references"], ensure_ascii=False)
            record["prompt"] = prompt
            folder = self.store.root / record["id"]
            for attempt in range(2):
                await self.event(record, "generating" if attempt == 0 else "repairing",
                                 "按固定画风参考生成照片" if attempt == 0 else "根据检查结果修正一次", attempt=attempt + 1)
                raw, result = await client.image(settings, prompt, images)
                filename = f"attempt-{attempt + 1}.png"
                info = await asyncio.to_thread(write_image, folder / filename, raw)
                record["images"].append({"file": filename, "attempt": attempt + 1, **info, **result})
                await self.event(record, "checking", "检查拍摄对象、人脸、场景和画风", attempt=attempt + 1)
                check, usage = await client.json(settings["check_model"], CHECK, {
                    "instruction": record["instruction"], "plan": plan, "snapshot": record["snapshot"],
                }, images=[raw, images[0]])
                if not isinstance(check.get("passed"), bool) or not isinstance(check.get("issues"), list) or not isinstance(check.get("repairable"), bool):
                    raise ValueError("图片检查未返回有效判断")
                record["images"][-1]["check"] = check
                record["images"][-1]["check_usage"] = usage
                if check["passed"] and not check["issues"] and isinstance(check.get("description"), str) and check["description"].strip():
                    record["final_image"] = filename
                    record["description"] = check["description"]
                    await self.event(record, "ready", "检查通过，成品已保存")
                    if record["preview"]:
                        await self.event(record, "preview", "本地接口实测完成，未发送到聊天")
                    else:
                        await self._deliver(record, raw)
                    return
                if attempt == 1 or not check["repairable"]:
                    await self.event(record, "failed", "图片检查未通过，未发送", check=check)
                    raise ValueError("图片检查未通过：" + "；".join(map(str, check["issues"])))
                record["repair_prompt"] = "\n仅修正以下问题，其余拍摄对象与事实保持一致：" + json.dumps(check["issues"], ensure_ascii=False)
                prompt = record["prompt"] + record["repair_prompt"] + "\n最后一张参考图是待修正的上一版照片。"
                images = images + [raw]
        finally:
            await client.close()

    def _references(self, record: dict[str, Any], plan: dict[str, Any]) -> list[bytes]:
        folder = self.store.root / record["id"]
        folder.mkdir(parents=True, exist_ok=True)
        ref_dir = self.plugin_dir / "camera/references"
        manifest = json.loads((ref_dir / "references.json").read_text())
        entry = manifest["references"][1 if plan["scene_type"] == "outdoor" else 0]
        raw = (ref_dir / entry["file"]).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError("固定画风参考图摘要不一致")
        (folder / "reference-1.png").write_bytes(raw)
        record["references"] = [{"file": "reference-1.png", "role": "固定画风与手机取景参考", "sha256": digest}]
        record["style_version"] = manifest["version"]
        images = [raw]
        if plan["include_outfit"]:
            with (self.plugin_dir / "character/outfit_images.toml").open("rb") as handle:
                mapping = tomllib.load(handle)["images"]
            name = record["snapshot"]["outfit"]
            if name not in mapping:
                raise ValueError(f"当前穿搭「{name}」缺少设定图，无法保证照片穿搭一致")
            root = (self.plugin_dir / "character").resolve()
            target = (root / mapping[name]).resolve()
            if not target.is_relative_to(root):
                raise ValueError("服饰参考图路径越界")
            raw = target.read_bytes()
            (folder / "reference-2.png").write_bytes(raw)
            images.append(raw)
            record["references"].append({"file": "reference-2.png", "role": "当前穿搭款式参考，忽略排版",
                                         "outfit": name, "sha256": hashlib.sha256(raw).hexdigest()})
        if plan.get("use_previous_photo") is True:
            previous = record.get("previous_photo")
            if not previous:
                raise ValueError("拍摄方案要求沿用旧照片，但当前会话没有已发送的照片")
            target = (self.store.root / previous["id"] / previous["file"]).resolve()
            if not target.is_relative_to(self.store.root.resolve()):
                raise ValueError("旧照片路径越界")
            raw = target.read_bytes()
            name = f"reference-{len(images) + 1}.png"
            (folder / name).write_bytes(raw)
            images.append(raw)
            record["references"].append({"file": name, "role": "同一会话上一张照片，用于保持对象一致",
                                         "sha256": hashlib.sha256(raw).hexdigest()})
        return images

    async def _deliver(self, record: dict[str, Any], raw: bytes) -> None:
        await self.event(record, "sending", "向原会话发送已检查的成品")
        try:
            result = await self.ctx.send.image(
                base64.b64encode(raw).decode(), record["stream_id"], return_details=True,
                sync_to_maisaka_history=True, maisaka_source_kind="plugin_camera",
            )
        except Exception as exc:
            await self.event(record, "delivery_unknown", f"发送回执不明，保留成品且未自动重发：{type(exc).__name__}")
            return
        if not isinstance(result, dict) or result.get("sent") is not True:
            await self.event(record, "send_failed", "图片生成成功，但发送未成功；保留成品，不重新生图")
            return
        record["message_id"] = result.get("message_id")
        await self.event(record, "sent", "照片已成功发送到原会话")
        try:
            result = await self.ctx.maisaka.context.append(
                stream_id=record["stream_id"], segments=[{"type": "text", "text": "已按用户请求发送照片，画面内容：" + record["description"]}],
                source_kind="plugin_camera_description", message_id="camera-description:" + record["id"],
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("主程序未确认照片描述已回填")
        except Exception:
            record["events"].append({"at": timestamp(), "stage": "context_warning", "text": "图片已发送，但画面描述回填失败"})
            await asyncio.to_thread(self.store.save, record)
            _logger.exception("[相机] 照片描述回填失败 %s", record["id"])
