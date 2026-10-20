"""持久化后台拍照队列。每份请求只生成、检查和发送一次成品。"""

from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import base64
import hashlib
import json
import logging

from PIL import Image

from .client import CPAClient, GenerationRejected, GenerationUnknown, read_provider
from .prompt_context import reference_roles_text, snapshot_text
from .prompts import CHECK, DECLINE_GUIDANCE, DECLINE_REASONS, DIRECTOR, PERSON_STYLE, SAFE_REWRITE, SCENE_STYLE, STYLE
from .reference_images import build_references
from .store import CameraStore, timestamp


_logger = logging.getLogger("a_day_with_mittes.camera")
PHOTO_DIMENSIONS = (1024, 1536)
PHOTO_SIZE = "1024x1536"
DEFAULTS = {
    "enabled": False, "api_provider": "CLIProxyAPI", "director_model": "gpt-6.1-sol",
    "image_model": "gpt-image-2.5-sunburst", "check_model": "gpt-6.1-sol",
    "quality": "high", "timeout_seconds": 300,
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
        if image.size != PHOTO_DIMENSIONS:
            raise ValueError(f"生图输出必须为竖版 2:3、{PHOTO_SIZE}，实际为 {width}x{height}")
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
        settings["size"] = PHOTO_SIZE
        if not settings["enabled"]:
            raise ValueError("拍照功能尚未开启")
        if not instruction.strip() or len(instruction) > 2000:
            raise ValueError("instruction 必须是 1–2000 字的拍摄意图")
        if not source_msg_id or (not preview and not stream_id):
            raise ValueError("缺少原会话或真实用户消息 ID")
        context = []
        previous_photo = None
        requester = None
        source_text = instruction
        if not preview:
            source = await self.ctx.message.get_by_id(source_msg_id, stream_id=stream_id)
            if not isinstance(source, dict) or source.get("session_id") != stream_id:
                raise ValueError("源消息不属于当前会话或无法读取")
            user = source.get("message_info", {}).get("user_info", {})
            if not user.get("user_id") or str(user["user_id"]) == str(source.get("account_id")):
                raise ValueError("拍照必须由真实用户的消息触发")
            platform = str(source.get("platform") or "").strip().lower()
            if not platform:
                raise ValueError("源消息缺少平台，无法核验拍照额度")
            requester = {"platform": platform, "user_id": str(user["user_id"])}
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
                                  "instruction": previous["instruction"], "file": previous["final_image"],
                                  "snapshot": {key: previous["snapshot"].get(key) for key in ("captured_at", "outfit", "place")}}
        return await asyncio.to_thread(self.store.create, {
            "stream_id": stream_id, "source_msg_id": source_msg_id,
            "instruction": instruction.strip(), "source_text": source_text[:2000], "context": context,
            "snapshot": snapshot, "settings": settings, "preview": preview, "prompt_version": "phone-photo-v12",
            "images": [], "references": [],
            "previous_photo": previous_photo,
            "requester": requester,
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
                            intent="这次未能向用户分享照片。选择 reply 时，让 replyer 按 Mittes 本人拿手机拍照的语境简短说明这次没拍好，不讲工具或技术流程，不编造具体故障、姿势或意外经过。不要声称已有照片或已发送，不要自动重新调用拍照工具。",
                            reason="camera_failed", metadata={"background_tool_result": True, "tool_name": "take_mittes_photo",
                                                               "tool_success": False, "task_id": record["id"]},
                        )
                    except Exception:
                        _logger.exception("[相机] 失败回填未成功 %s", record["id"])

    async def process(self, record: dict[str, Any]) -> None:
        settings = record["settings"]
        # 重载后恢复的旧排队任务也使用统一尺寸，参数与归档保持一致。
        settings["size"] = PHOTO_SIZE
        await self.event(record, "planning", "读取请求快照，设计手机取景")
        base_url, key = await asyncio.to_thread(read_provider, self.plugin_dir.parents[1] / "config/model_config.toml", settings["api_provider"])
        client = self.client_factory(base_url, key, float(settings["timeout_seconds"]))
        try:
            plan, usage = await client.json(settings["director_model"], DIRECTOR, {
                "instruction": record["instruction"], "source_text": record["source_text"],
                "context": record["context"], "snapshot": record["snapshot"],
                "previous_photo": record.get("previous_photo"),
            })
            record["plan"] = plan
            record["planning_usage"] = usage
            if not isinstance(plan.get("feasible"), bool) or plan.get("target_kind") not in {"self", "object", "environment", "other_person", "unclear"}:
                raise ValueError("取景分析未返回有效的可拍判断与拍摄对象类型")
            if plan["target_kind"] == "other_person" or plan.get("rejection_code") == "other_person_privacy":
                await self._decline(record, "other_person_privacy")
                return
            if plan.get("rejection_code") == "self_privacy":
                await self._decline(record, "self_privacy")
                return
            if plan["target_kind"] == "unclear":
                await self._decline(record, "unclear_request")
                return
            if not plan["feasible"]:
                code = plan.get("rejection_code")
                if code not in DECLINE_REASONS:
                    raise ValueError("不可拍判断缺少有效原因代码")
                await self._decline(record, code)
                return
            if plan.get("rejection_code") != "":
                raise ValueError("可拍判断与拒绝原因不一致")
            for field in ("subject", "framing", "visible_details", "lighting", "prompt"):
                if not isinstance(plan.get(field), str) or not plan[field].strip():
                    raise ValueError(f"拍摄方案缺少 {field}")
            if plan.get("scene_type") not in {"indoor", "outdoor"} or not isinstance(plan.get("include_outfit"), bool):
                raise ValueError("拍摄方案的场景类型或穿搭标记无效")
            if plan.get("style_category") not in {"portrait", "food", "drink", "outdoor", "object"} or plan.get("identity_reference") not in {"none", "body", "head"}:
                raise ValueError("拍摄方案的参考图用途无效")
            if not isinstance(plan.get("contains_mittes"), bool):
                raise ValueError("拍摄方案缺少人物入镜标记")
            if plan.get("scene_context") not in {"none", "street", "terrace", "train", "cafe", "night_desk", "classroom"}:
                raise ValueError("拍摄方案缺少有效的场景参考类别")
            if plan.get("clothing_context") not in {"none", "maid", "socks", "shoes"}:
                raise ValueError("拍摄方案缺少有效的衣着参考类别")
            if plan["contains_mittes"] and plan["clothing_context"] != "none" and not plan["include_outfit"]:
                raise ValueError("衣着分享参考必须同时加载当前衣柜款式")
            if not plan["contains_mittes"] and plan["identity_reference"] != "none":
                raise ValueError("无人物拍摄方案不能使用人物外貌参考")
            if not isinstance(plan.get("use_previous_photo"), bool):
                raise ValueError("拍摄方案缺少连续照片标记")
            images = await asyncio.to_thread(self._references, record, plan)
            check_references = list(images)
            prompt = self._prompt(record)
            record["prompt"] = prompt
            folder = self.store.root / record["id"]
            for attempt in range(2):
                await self.event(record, "generating" if attempt == 0 else "repairing",
                                 "按固定画风参考生成照片" if attempt == 0 else "根据检查结果修正一次", attempt=attempt + 1)
                raw, result = await self._generate(client, record, prompt, images, attempt)
                filename = f"attempt-{attempt + 1}.png"
                info = await asyncio.to_thread(write_image, folder / filename, raw)
                record["images"].append({"file": filename, "attempt": attempt + 1, **info, **result})
                await self.event(record, "checking", "检查拍摄对象、面部遮挡、场景和画风", attempt=attempt + 1)
                check, usage = await client.json(settings["check_model"], CHECK, {
                    "instruction": record["instruction"], "source_text": record["source_text"],
                    "plan": plan, "snapshot": record["snapshot"], "reference_roles": record["references"],
                    "style_family": record["style_family"],
                }, images=[raw, *check_references])
                if not isinstance(check.get("passed"), bool) or not isinstance(check.get("issues"), list) or not isinstance(check.get("repairable"), bool):
                    raise ValueError("图片检查未返回有效判断")
                record["images"][-1]["check"] = check
                record["images"][-1]["check_usage"] = usage
                passed = check["passed"] and not check["issues"] and isinstance(check.get("description"), str) and bool(check["description"].strip())
                if passed:
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

    @staticmethod
    def _prompt(record: dict[str, Any]) -> str:
        style = PERSON_STYLE if record["plan"]["contains_mittes"] else SCENE_STYLE
        return (STYLE + "\n" + style + "\n\n本次拍摄：\n" + record["plan"]["prompt"]
                + "\n\n事实依据（拍摄时快照）：\n" + snapshot_text(record["snapshot"])
                + "\n\n参考图用途：\n" + reference_roles_text(record["references"]))

    async def _generate(self, client: CPAClient, record: dict[str, Any], prompt: str,
                        images: list[bytes], attempt: int) -> tuple[bytes, dict[str, Any]]:
        try:
            return await client.image(record["settings"], prompt, images)
        except GenerationRejected as exc:
            record.setdefault("generation_rejections", []).append({"at": timestamp(), "reason": str(exc), "prompt": prompt})
            # 每任务只允许一次复核改写；即使在后续成品修正时被拒绝也不能循环重试。
            if "prompt_optimization" in record:
                await asyncio.to_thread(self.store.save, record)
                raise
            optimization: dict[str, Any] = {"status": "reviewing", "original_scene_prompt": record["plan"]["prompt"]}
            record["prompt_optimization"] = optimization
            await self.event(record, "planning", "生图请求被明确拒绝，复核原始意图并优化生活照表述")
            rewrite, usage = await client.json(record["settings"]["director_model"], SAFE_REWRITE, {
                "instruction": record["instruction"], "source_text": record["source_text"],
                "context": record["context"], "snapshot": record["snapshot"], "plan": record["plan"],
                "rejected_prompt": prompt, "rejection": str(exc), "reference_roles": record["references"],
                "repair_issues": record.get("repair_prompt"),
            }, images=images)
            optimization.update(result=rewrite, usage=usage)
            if not isinstance(rewrite.get("retry_allowed"), bool) or not isinstance(rewrite.get("reason"), str) or not rewrite["reason"].strip():
                optimization["status"] = "invalid"
                await asyncio.to_thread(self.store.save, record)
                raise ValueError("提示词复核未返回有效判断与原因")
            if not rewrite["retry_allowed"]:
                optimization["status"] = "declined"
                await asyncio.to_thread(self.store.save, record)
                raise GenerationRejected("提示词复核未允许重试：" + rewrite["reason"]) from exc
            if not isinstance(rewrite.get("prompt"), str) or not rewrite["prompt"].strip():
                optimization["status"] = "invalid"
                await asyncio.to_thread(self.store.save, record)
                raise ValueError("提示词复核缺少有效的拍摄描述")
            record["plan"]["prompt"] = rewrite["prompt"]
            record["prompt"] = self._prompt(record)
            prompt = record["prompt"]
            if attempt:
                prompt += record["repair_prompt"] + "\n最后一张参考图是待修正的上一版照片。"
            optimization["status"] = "retrying"
            await self.event(record, "generating" if attempt == 0 else "repairing", "按复核后的生活照提示词重试一次", attempt=attempt + 1)
            try:
                result = await client.image(record["settings"], prompt, images)
            except GenerationRejected as retry_exc:
                optimization["status"] = "rejected"
                record["generation_rejections"].append({"at": timestamp(), "reason": str(retry_exc), "prompt": prompt})
                await asyncio.to_thread(self.store.save, record)
                raise
            optimization["status"] = "generated"
            return result

    async def _decline(self, record: dict[str, Any], code: str) -> None:
        guidance = DECLINE_GUIDANCE if code != "unclear_request" else (
            "选择 reply，让 replyer 简短询问用户具体想看什么。不要擅自选择拍摄对象或自动重试。"
        )
        record["decline"] = {"reason_code": code, "reason": DECLINE_REASONS[code], "planner_guidance": guidance}
        await self.event(record, "declined", DECLINE_REASONS[code])
        if record["preview"]:
            return
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=record["stream_id"],
                intent="这次不拍摄照片。\n"
                       + DECLINE_REASONS[code] + "\n" + guidance,
                reason="camera_declined",
                metadata={"background_tool_result": True, "tool_name": "take_mittes_photo",
                          "tool_success": False, "task_id": record["id"], "status": "declined", "reason_code": code},
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("planner 未确认接收不可拍摄结果")
            record["decline"]["feedback_delivered"] = True
            await asyncio.to_thread(self.store.save, record)
        except Exception:
            record["events"].append({"at": timestamp(), "stage": "feedback_warning", "text": "不可拍摄决定已保存，但回复指导回填失败；未自动重试"})
            await asyncio.to_thread(self.store.save, record)
            _logger.exception("[相机] 不可拍摄结果回填失败 %s", record["id"])

    def _references(self, record: dict[str, Any], plan: dict[str, Any]) -> list[bytes]:
        return build_references(self.plugin_dir, self.store.root, record, plan)

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
                stream_id=record["stream_id"], segments=[{"type": "text", "text": "Mittes 刚用手机拍下并已向当前会话发送用户想看的照片。后续聊天按她刚拍的照片来理解，自然谈照片中的内容，不讲工具或技术流程。照片实际可见内容：" + record["description"]}],
                source_kind="plugin_camera_description", message_id="camera-description:" + record["id"],
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("主程序未确认照片描述已回填")
        except Exception:
            record["events"].append({"at": timestamp(), "stage": "context_warning", "text": "图片已发送，但画面描述回填失败"})
            await asyncio.to_thread(self.store.save, record)
            _logger.exception("[相机] 照片描述回填失败 %s", record["id"])
