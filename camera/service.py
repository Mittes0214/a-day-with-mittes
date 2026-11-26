"""持久化后台拍照队列。每份请求只生成、检查和发送一次成品。"""

from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import base64
import hashlib
import json
import logging
import random
import re

from PIL import Image

from .client import CPAClient, GenerationRejected, GenerationUnknown, read_provider
from .prompt_context import FACE_COVERS, SHOT_METHODS, reference_roles_text, shot_text, snapshot_text
from .prompts import (CHECK, DECLINE_GUIDANCE, DECLINE_REASONS, DIRECTOR, DIRECTOR_DECLINES, PERSON_STYLE, PERSON_STYLE_SHARE,
                      REVIEW, REVIEW_DECLINES, SAFE_REWRITE, SCENE_STYLE, SHARE_STYLE, STYLE, STYLE_CASUAL, STYLE_FACE_SHOWN)
from .identity import message_image
from .reference_images import build_references, normalize_pose_image, pose_image, pose_upload_name
from .store import CameraStore, timestamp
from .trace import CameraTraceClient


_logger = logging.getLogger("a_day_with_mittes.camera")
PHOTO_DIMENSIONS = (1024, 1536)
PHOTO_SIZE = "1024x1536"
DEFAULTS = {
    "enabled": False, "api_provider": "CLIProxyAPI", "director_model": "gpt-6.2-sol",
    "review_model": "gpt-6.2-sol", "image_model": "gpt-image-2.5-sunburst", "check_model": "gpt-6.2-sol",
    "quality": "high", "timeout_seconds": 300,
}
# 三种照片，按稀有程度从低到高：日常分享不限量；不露脸自拍每次抽签、每个聊天每天有上限；露脸每个聊天每天一张。
PHOTO_KINDS = ("casual", "selfie", "face")
KIND_NAMES = {"casual": "日常分享", "selfie": "不露脸自拍", "face": "露脸照"}
SELFIE_CHANCE = 0.3
SELFIE_DAILY_LIMIT = 3
FACE_DAILY_LIMIT = 1
# 构图卡：日常分享一套，自拍一套（不露脸自拍和她自己拍的露脸照共用）；身边的人帮拍不用卡
CASUAL_CARDS = ("c_hold", "c_desk", "c_outfit", "c_feet", "c_lap", "c_table")
SELFIE_CARDS = ("s_arm", "s_timer", "s_high")
# 排卡时避开这个聊天最近用过的：日常分享避开两张，自拍避开一张
CASUAL_AVOID = 2
SELFIE_AVOID = 1
# 取景器只看这些快照字段；share_options 等旧字段即使存在也不传
DIRECTOR_SNAPSHOT_KEYS = ("captured_at", "slot", "title", "place", "weather", "outfit", "outfit_details",
                          "outfit_optional", "appearance", "story", "company", "helpers")
# 成品检查只对照这些事实
CHECK_FACT_KEYS = ("place", "outfit", "outfit_details", "outfit_optional", "appearance")


def message_text(message: dict[str, Any]) -> str:
    return str(message.get("processed_plain_text") or "").strip() or " ".join(
        str(part.get("data", "")) for part in message.get("raw_message", []) if part.get("type") == "text"
    )


class PoseImageError(ValueError):
    """用户指的姿势参考图取不到：消息不对、没有图或图打不开。"""


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


def requester_label(record: dict[str, Any]) -> str:
    return str(record.get("requester_name") or "").strip() or "群友"


def request_quote(record: dict[str, Any], limit: int = 30) -> str:
    """请求原话去掉引用前缀和 @，只留对方说的那句。"""
    text = re.sub(r"^\[回复了[^\]]*\]\s*", "", str(record.get("source_text") or ""))
    text = re.sub(r"@\S+\s*", "", text).strip() or str(record.get("instruction") or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def downgrade_note(record: dict[str, Any]) -> str:
    """对方想看的种类高于这次拍的种类时，提醒 planner 这是她自己选的，回复里自然带过。"""
    plan = record.get("plan") or {}
    asked, kind = plan.get("asked_for"), plan.get("photo_kind")
    if asked not in PHOTO_KINDS or kind not in PHOTO_KINDS or PHOTO_KINDS.index(asked) <= PHOTO_KINDS.index(kind):
        return ""
    return (f"对方原本想要{KIND_NAMES[asked]}，这次她自己选了拍{KIND_NAMES[kind]}；回复时自然带过，"
            "当作她自己的选择，不提额度、次数或概率。")


def delivery_note(record: dict[str, Any]) -> str:
    share = record.get("share")
    downgrade = downgrade_note(record)
    if share:  # 身边的人此刻帮她拍的照片：交代清楚是谁、怎么拍的
        note = (f"↑ 这张是{share['source']}刚刚帮 Mittes 拍的、她再分享给{requester_label(record)}的照片。"
                f"照片的来历（回复时照此说明）：{share['caption'].rstrip('。.！!')}。" + downgrade)
    elif downgrade:
        note = f"↑ 这张是给{requester_label(record)}拍的。" + downgrade
    else:
        note = f"↑ 这张是给{requester_label(record)}拍的，"
    return note + f"回应「{request_quote(record)}」(msg_id={record['source_msg_id']})"


def photo_origin(record: dict[str, Any]) -> str:
    """已发出照片的来历，一句话：自己刚拍的，还是身边的人帮拍的。"""
    share = record.get("share")
    if share:
        return f"是{share['source']}帮 Mittes 拍的照片，来历：{share['caption'].rstrip('。.！!')}"
    return "是 Mittes 刚用手机拍的"


DAILY_SHARE_STREAM = "daily-share"


def quiet(record: dict[str, Any]) -> bool:
    """本地预览和每日分享配图都不回到聊天：不发图、不回填、不触发 planner。"""
    return bool(record.get("preview") or record.get("daily_share"))


def pending_line(record: dict[str, Any]) -> str:
    return f"正在拍摄：{requester_label(record)}要的「{request_quote(record)}」（拍好会自动发出）"


def _text_fields(obj: Any, fields: tuple[str, ...], name: str, *, allow_empty: tuple[str, ...] = ()) -> None:
    if not isinstance(obj, dict):
        raise ValueError(f"拍摄方案缺少 {name}")
    for field in fields:
        value = obj.get(field)
        if not isinstance(value, str) or (field not in allow_empty and not value.strip()):
            raise ValueError(f"拍摄方案缺少 {name}.{field}")




def apply_kind(record: dict[str, Any], plan: dict[str, Any]) -> None:
    """照片种类、谁来拍和遮脸方式要对得上：自己拍时用构图卡，露脸照也可以由此刻身边的人帮拍。"""
    kind, mode = plan["photo_kind"], plan.get("mode")
    method = plan["shot"]["method"]
    cover = plan["face_cover"]["method"]
    if mode == "shoot":
        if method == "helper":
            raise ValueError("自己拍的照片不能写成别人帮拍")
        expected = {"casual": {"out_of_frame", "none"}, "selfie": {"crop"}, "face": {"shown"}}[kind]
        if cover not in expected:
            raise ValueError(f"{KIND_NAMES[kind]}的遮脸方式不能是 {cover}")
        if kind != "casual" and not plan["contains_mittes"]:
            raise ValueError("自拍必须有她入镜")
        return
    if mode != "assist":
        raise ValueError("拍摄方案的拍摄方式无效")
    if kind != "face" or cover != "shown":
        raise ValueError("身边的人帮拍的只能是露脸照")
    if method != "helper":
        raise ValueError("别人帮拍的照片拍法必须是 helper")
    if plan["card"]:
        raise ValueError("别人帮拍的照片不用构图卡")
    share = plan.get("share") if isinstance(plan.get("share"), dict) else {}
    if share.get("candid") not in {"requested", "asked"}:
        raise ValueError("当场帮拍只能是她请对方拍或知道在拍")
    if not plan["contains_mittes"]:
        raise ValueError("别人帮拍的照片必须有 Mittes 入镜")
    caption = str(share.get("caption") or "").strip()
    if not caption:
        raise ValueError("别人帮拍的照片缺少来历说明")
    source = str(share.get("source") or "")
    if source == "Sweyn" or source == record["requester_persona"]:
        raise ValueError("Sweyn 和提问的人自己都不当拍照的人")
    current = record["snapshot"]
    if source not in current["helpers"]:
        raise ValueError(f"此刻身边没有 {source}")
    record["share"] = {"kind": "assist", "source": source, "candid": share["candid"], "slot": current["slot"],
                       "taken_at": "", "caption": caption}


def validate_pose(record: dict[str, Any], plan: dict[str, Any]) -> None:
    """有姿势参考时：只能是她此刻自己拍、本人入镜，方案写清模仿什么、因持机或场景改了什么。"""
    pose = plan.get("pose_reference")
    if not record.get("pose_reference"):
        if isinstance(pose, dict) and any(str(value).strip() for value in pose.values()):
            raise ValueError("没有姿势参考图，方案却写了姿势参考")
        return
    _text_fields(pose, ("imitate", "adapt"), "pose_reference", allow_empty=("adapt",))
    if plan["mode"] != "shoot" or not plan["contains_mittes"]:
        raise ValueError("模仿姿势的照片只能是 Mittes 此刻自己拍、本人入镜")
    if not plan["pose"].strip():
        raise ValueError("模仿姿势的照片缺少姿势与动作")


def validate_shot(plan: dict[str, Any]) -> None:
    """结构化取景方案：每个字段各管一件事，程序按固定顺序排成拍摄说明。"""
    _text_fields(plan, ("subject", "prompt"), "plan", allow_empty=("prompt",))
    _text_fields(plan.get("shot"), ("method", "phone", "height", "angle", "distance"), "shot")
    if plan["shot"]["method"] not in SHOT_METHODS:
        raise ValueError("拍摄方案的拍法无效")
    _text_fields(plan.get("frame"), ("top", "bottom", "sides", "focus"), "frame")
    _text_fields(plan.get("face_cover"), ("method", "detail"), "face_cover", allow_empty=("detail",))
    if plan["face_cover"]["method"] not in FACE_COVERS:
        raise ValueError("拍摄方案的遮脸方式无效")
    if plan["face_cover"]["method"] != "none" and not plan["face_cover"]["detail"].strip():
        raise ValueError("拍摄方案缺少遮脸细节")
    if not isinstance(plan.get("pose"), str):
        raise ValueError("拍摄方案缺少 pose")
    if not isinstance(plan.get("visible"), list) or not all(isinstance(item, str) and item.strip() for item in plan["visible"]):
        raise ValueError("拍摄方案的可见内容无效")
    _text_fields(plan.get("background"), ("content", "people"), "background")
    _text_fields(plan.get("lighting"), ("source", "direction", "tone"), "lighting")


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
                     snapshot: dict[str, Any], *, preview: bool = False,
                     daily_share: dict[str, Any] | None = None,
                     pose_msg_id: str = "", pose_image_index: int = 0) -> dict[str, Any]:
        """daily_share：每日分享配图。没有请求消息和提问人，按「发给其他人」的规矩拍，
        检查通过后停在 daily_share 状态交给每日分享审核，不发到任何聊天，不占额度。
        pose_msg_id：用户要她模仿某张图的姿势时，那张图所在的消息；图转成 PNG 存进 uploads/，生图时作姿势参考。"""
        settings = {key: await self.get_config(f"photo.{key}", default) for key, default in DEFAULTS.items()}
        settings["size"] = PHOTO_SIZE
        if not settings["enabled"]:
            raise ValueError("拍照功能尚未开启")
        if not instruction.strip() or len(instruction) > 2000:
            raise ValueError("instruction 必须是 1–2000 字的拍摄意图")
        if daily_share is not None:
            preview = False
            stream_id = DAILY_SHARE_STREAM
        if not source_msg_id or (not preview and not stream_id):
            raise ValueError("缺少原会话或真实用户消息 ID")
        context = []
        requester = None
        requester_name = ""
        requester_persona = ""
        source_text = instruction
        if not preview and daily_share is None:
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
            requester_name = str(user.get("user_cardname") or user.get("user_nickname") or "").strip()
            # bot_config 的 [plugin] permission 就是店长 Sweyn 在各平台的账号
            owners = await self.ctx.config.get("plugin.permission", [])
            if f"{platform}:{requester['user_id']}" in owners:
                requester_persona = "Sweyn"
            source_text = message_text(source)
            recent = await self.ctx.message.get_recent(stream_id, limit=12)
            if not isinstance(recent, list):
                raise RuntimeError("无法读取拍照请求的相关对话")
            for message in recent:
                text = message_text(message)
                if text:
                    context.append({"text": text[:1200], "user_id": str(message.get("message_info", {}).get("user_info", {}).get("user_id", ""))})
        record = {
            "stream_id": stream_id, "source_msg_id": source_msg_id,
            "instruction": instruction.strip(), "source_text": source_text[:2000], "context": context,
            "snapshot": snapshot, "settings": settings, "preview": preview, "prompt_version": "phone-photo-v19",
            "images": [], "references": [],
            "previous_photo": None,
            "requester": requester, "requester_name": requester_name, "requester_persona": requester_persona,
            "daily_share": daily_share, "pose_reference": None,
        }
        if not preview and daily_share is None:
            # 同一条消息再次调用时直接给出已有结果，不重复预审
            existing = await asyncio.to_thread(self.store.find, stream_id, source_msg_id)
            if existing is not None:
                return existing
            # 预审在入队前同步完成：此刻不方便拍、或光看请求就拍不成时，planner 当轮就拿到她怎么回，不会先答应再收回
            record["review"] = await self._review(record)
            if record["review"]["decision"] == "decline":
                code = record["review"]["decline_code"]
                reason = DECLINE_REASONS[code]
                if code == "not_now":
                    reason = "Mittes 此刻不方便拍照：" + record["review"]["moment"]
                record["decline"] = {
                    "reason_code": code, "reason": reason,
                    "planner_guidance": "怎么回：" + record["review"]["acting"] + "\n" + DECLINE_GUIDANCE,
                    "feedback_delivered": True,
                }
                return await asyncio.to_thread(self.store.create_declined, record)
        if pose_msg_id:
            if daily_share is not None or preview:
                raise ValueError("姿势参考只用于聊天里的拍照请求")
            record["pose_reference"] = await self._save_pose(stream_id, pose_msg_id, pose_image_index)
        return await asyncio.to_thread(self.store.create, record)

    async def _review(self, record: dict[str, Any]) -> dict[str, Any]:
        """预审：此刻能不能拍、光看请求是不是拍不成；拍不了时写她此刻在做什么和怎么回。调用记录写进 model_calls 第一项。"""
        settings = record["settings"]
        snapshot = record["snapshot"]
        data = {
            "instruction": record["instruction"], "source_text": record["source_text"], "context": record["context"],
            "now": snapshot["captured_at"], "slot": snapshot["slot"], "title": snapshot["title"],
            "place": snapshot["place"], "company": snapshot["company"], "story": snapshot["story"],
        }
        call = {"index": 1, "stage": "review", "model": settings["review_model"], "started_at": timestamp(),
                "input": {"system": REVIEW, "text": json.dumps(data, ensure_ascii=False), "data": data, "images": []}}
        record["model_calls"] = [call]
        base_url, key = await asyncio.to_thread(read_provider, settings["api_provider"])
        client = self.client_factory(base_url, key, float(settings["timeout_seconds"]))
        try:
            result, usage = await client.json(settings["review_model"], REVIEW, data)
        finally:
            await client.close()
        call.update(status="completed", completed_at=timestamp(), output={"result": result, "usage": usage})
        decision, code = result.get("decision"), result.get("decline_code")
        moment, acting = result.get("moment"), result.get("acting")
        if decision not in {"allow", "decline"} or not isinstance(moment, str) or not moment.strip() or not isinstance(acting, str):
            raise ValueError("预审未返回有效的 decision/moment/acting")
        if decision == "decline" and (code not in REVIEW_DECLINES or not acting.strip()):
            raise ValueError("预审拒绝时缺少有效的拒绝原因或回复指导")
        if decision == "allow" and (code or acting.strip()):
            raise ValueError("预审放行时不能带拒绝原因或回复指导")
        return {"decision": decision, "decline_code": code or "", "moment": moment.strip(), "acting": acting.strip(), "usage": usage}

    async def _save_pose(self, stream_id: str, msg_id: str, image_index: int) -> dict[str, Any]:
        message = await self.ctx.message.get_by_id(msg_id, chat_id=stream_id, include_binary_data=True)
        if not isinstance(message, dict) or str(message.get("session_id") or "") not in {"", stream_id}:
            raise PoseImageError(f"消息 {msg_id} 不在当前会话里或无法读取")
        try:
            raw = await asyncio.to_thread(normalize_pose_image, message_image(message, image_index))
        except (ValueError, OSError) as exc:
            raise PoseImageError(str(exc)) from exc
        name = pose_upload_name(raw)
        target = self.store.root / "uploads" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, raw)
        return {"file": name, "sha256": hashlib.sha256(raw).hexdigest(), "msg_id": msg_id, "image_index": image_index}

    def _photo_options(self, record: dict[str, Any]) -> dict[str, list[str]]:
        """这次可用的照片种类，各自带上打乱好的构图卡。露脸每个聊天每天一张；不露脸自拍每次抽签，
        每个聊天每天有上限；日常分享总是可用。店长、本地预览和每日分享不受额度限制。"""
        exempt = record["quota"]["exempt"]
        used = {} if exempt else self.store.kinds_sent_today(record["stream_id"])
        recent = [] if exempt else self.store.recent_cards(record["stream_id"])

        def shuffled(cards: tuple[str, ...], avoid: int) -> list[str]:
            recent_kind = [card for card in recent if card in cards][:avoid]
            order = [card for card in cards if card not in recent_kind]
            random.shuffle(order)
            return order + recent_kind  # 最近用过的排到最后，此刻只适合它时仍可选

        options = {"casual": shuffled(CASUAL_CARDS, CASUAL_AVOID)}
        selfie = shuffled(SELFIE_CARDS, SELFIE_AVOID)
        if exempt or (random.random() < SELFIE_CHANCE and used.get("selfie", 0) < SELFIE_DAILY_LIMIT):
            options["selfie"] = selfie
        if exempt or used.get("face", 0) < FACE_DAILY_LIMIT:
            options["face"] = selfie
        return options

    def _previous_photo(self, record: dict[str, Any]) -> dict[str, Any] | None:
        previous = self.store.latest_sent(record["stream_id"])
        if previous is None or previous["id"] == record["id"]:
            return None
        return {"id": previous["id"], "description": previous["description"],
                "instruction": previous["instruction"], "file": previous["final_image"],
                "snapshot": {key: previous["snapshot"].get(key) for key in ("captured_at", "outfit", "place")}}

    def _director_input(self, record: dict[str, Any]) -> dict[str, Any]:
        """取景器只拿取景要用的：请求、对话、此刻的事实和可用照片；帮拍拍法、上一张照片、姿势参考按需才给。"""
        options = record["photo_options"]
        snapshot = {key: record["snapshot"][key] for key in DIRECTOR_SNAPSHOT_KEYS
                    if key in record["snapshot"] and not (key == "outfit_optional" and not record["snapshot"][key])}
        data: dict[str, Any] = {"instruction": record["instruction"], "source_text": record["source_text"],
                                "context": record["context"], "snapshot": snapshot, "photo_options": options}
        if "face" in options:
            data["recent_candids"] = self.store.recent_candids()
        if record.get("previous_photo"):
            data["previous_photo"] = {key: value for key, value in record["previous_photo"].items() if key != "file"}
        if record.get("pose_reference"):
            data["pose_reference"] = True
        return data

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
                if not quiet(record):
                    try:
                        await self.ctx.maisaka.proactive.trigger(
                            stream_id=record["stream_id"],
                            intent="这次照片没拍好，没能分享给对方，也没有发出照片；不要替她编具体故障、姿势或意外经过。选择 reply 回应对方，不要自动重新调用拍照工具。",
                            reason="camera_failed", metadata={"background_tool_result": True, "tool_name": "take_mittes_photo",
                                                               "tool_success": False, "task_id": record["id"]},
                        )
                    except Exception:
                        _logger.exception("[相机] 失败回填未成功 %s", record["id"])

    async def process(self, record: dict[str, Any]) -> None:
        settings = record["settings"]
        # 重载后恢复的旧排队任务也使用统一尺寸，参数与归档保持一致。
        settings["size"] = PHOTO_SIZE
        if not quiet(record):
            # 上一张照片在开始处理时才取：队列串行，此刻前一张已经发完。
            # 提交时取会漏掉排在前面、尚未发出的那张（11-04 拍脚照拿到的是自拍而不是穿搭照）。
            record["previous_photo"] = await asyncio.to_thread(self._previous_photo, record)
        await self.event(record, "planning", "读取请求快照，设计手机取景")
        # 本地试拍可以指定可用照片；其余每次按额度和抽签现算
        if "photo_options" not in record:
            record["photo_options"] = await asyncio.to_thread(self._photo_options, record)
        pose_images = None
        if record.get("pose_reference"):
            # 取景器先看这张图；任务目录里留一份 pose.png，流程日志按文件名指到它
            raw_pose = await asyncio.to_thread(pose_image, self.store.root, record["pose_reference"])
            await asyncio.to_thread((self.store.root / record["id"]).mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread((self.store.root / record["id"] / "pose.png").write_bytes, raw_pose)
            pose_images = [raw_pose]
        base_url, key = await asyncio.to_thread(read_provider, settings["api_provider"])
        client = CameraTraceClient(self.client_factory(base_url, key, float(settings["timeout_seconds"])), record, self.store)
        try:
            data = await asyncio.to_thread(self._director_input, record)
            plan, usage = await client.json(settings["director_model"], DIRECTOR, data, images=pose_images)
            record["plan"] = plan
            record["planning_usage"] = usage
            if not isinstance(plan.get("feasible"), bool):
                raise ValueError("取景器未返回有效的可拍判断")
            if not plan["feasible"]:
                code = plan.get("rejection_code")
                if code not in DIRECTOR_DECLINES:
                    raise ValueError("取景器拒绝时缺少有效原因代码")
                moment, acting = plan.get("moment"), plan.get("acting")
                if not isinstance(moment, str) or not moment.strip() or not isinstance(acting, str) or not acting.strip():
                    raise ValueError("取景器拒绝时缺少她此刻的状况或回复指导")
                reason = DECLINE_REASONS[code]
                if code == "helper_absent":
                    name = str(plan.get("absent_helper") or "").strip()
                    if not name or name in record["snapshot"]["helpers"]:
                        raise ValueError("点名的人不在身边，但方案没写明是谁或其实在场")
                    reason = f"{name} 现在不在 Mittes 身边。"
                await self._decline(record, code, reason, "怎么回：" + acting.strip() + "\n" + DECLINE_GUIDANCE)
                return
            if plan.get("rejection_code") != "":
                raise ValueError("可拍判断与拒绝原因不一致")
            options = record["photo_options"]
            if plan.get("photo_kind") not in options:
                raise ValueError(f"取景器选了这次不可用的照片种类：{plan.get('photo_kind')}")
            if plan.get("asked_for") not in PHOTO_KINDS:
                raise ValueError("取景器没写对方原本想要的照片种类")
            if not isinstance(plan.get("card"), str):
                raise ValueError("拍摄方案缺少构图卡")
            if plan.get("mode") == "shoot" and plan["card"] not in options[plan["photo_kind"]]:
                raise ValueError(f"构图卡 {plan['card']} 不属于{KIND_NAMES[plan['photo_kind']]}")
            if not isinstance(plan.get("must_show"), list) or not all(isinstance(item, str) for item in plan["must_show"]):
                raise ValueError("拍摄方案的 must_show 无效")
            if not isinstance(plan.get("fact_changes"), list) or not all(isinstance(item, str) for item in plan["fact_changes"]):
                raise ValueError("拍摄方案的 fact_changes 无效")
            validate_shot(plan)
            if not isinstance(plan.get("include_outfit"), bool):
                raise ValueError("拍摄方案缺少穿搭标记")
            if plan.get("identity_reference") not in {"none", "body"}:
                raise ValueError("拍摄方案的外貌参考用途无效")
            if not isinstance(plan.get("contains_mittes"), bool):
                raise ValueError("拍摄方案缺少人物入镜标记")
            if plan.get("scene_context") not in {"none", "street", "terrace", "train", "cafe", "night_desk", "classroom"}:
                raise ValueError("拍摄方案缺少有效的场景参考类别")
            if plan.get("clothing_context") not in {"none", "socks", "shoes"}:
                raise ValueError("拍摄方案缺少有效的衣着参考类别")
            if plan["contains_mittes"] and plan["clothing_context"] != "none" and not plan["include_outfit"]:
                raise ValueError("衣着分享参考必须同时加载当前衣柜款式")
            if plan["contains_mittes"] == (plan["face_cover"]["method"] == "none"):
                raise ValueError("遮脸方式与人物入镜标记不一致")
            apply_kind(record, plan)
            validate_pose(record, plan)
            if not plan["contains_mittes"] and plan["identity_reference"] != "none":
                raise ValueError("无人物拍摄方案不能使用人物外貌参考")
            if not isinstance(plan.get("use_previous_photo"), bool):
                raise ValueError("拍摄方案缺少连续照片标记")
            expression = plan.get("expression")
            if not isinstance(expression, dict) or not isinstance(expression.get("visible"), bool):
                raise ValueError("拍摄方案缺少有效的表情可见性判断")
            for field in ("look", "basis"):
                if not isinstance(expression.get(field), str):
                    raise ValueError(f"表情方案缺少有效的 {field}")
            if expression["visible"]:
                if not plan["contains_mittes"] or not all(expression[field].strip() for field in ("look", "basis")):
                    raise ValueError("可见表情需要人物入镜及完整的神情和依据")
            elif any(expression[field].strip() for field in ("look", "basis")):
                raise ValueError("嘴部不入镜时不能安排表情")
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
                facts = {key: record["snapshot"][key] for key in CHECK_FACT_KEYS
                         if key in record["snapshot"] and not (key == "outfit_optional" and not record["snapshot"][key])}
                check, usage = await client.json(settings["check_model"], CHECK, {
                    "plan": plan, "facts": facts, "reference_roles": record["references"],
                    "style_family": record["style_family"],
                }, images=[raw, *check_references])
                if not isinstance(check.get("passed"), bool) or not isinstance(check.get("issues"), list) or not isinstance(check.get("repairable"), bool):
                    raise ValueError("图片检查未返回有效判断")
                if not isinstance(check.get("notes", []), list):
                    raise ValueError("图片检查的备注格式无效")
                record["images"][-1]["check"] = check
                record["images"][-1]["check_usage"] = usage
                passed = check["passed"] and not check["issues"] and isinstance(check.get("description"), str) and bool(check["description"].strip())
                if passed:
                    record["final_image"] = filename
                    record["description"] = check["description"]
                    await self.event(record, "ready", "检查通过，成品已保存")
                    if record.get("daily_share"):
                        await self.event(record, "daily_share", "交给每日分享，等待面板审核，不发到聊天")
                    elif record["preview"]:
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
        share = record.get("share")
        style = (PERSON_STYLE_SHARE if share else PERSON_STYLE) if record["plan"]["contains_mittes"] else SCENE_STYLE
        scene = shot_text(record["plan"])
        # 只有可见的神情才写进本次拍摄：一句话的神情和由来，不写嘴部动作，嘴型交给生图模型。
        expression = record["plan"].get("expression")
        if expression and expression["visible"]:
            scene += "\n此刻的神情：" + expression["look"].strip()
        changes = [change.strip() for change in record["plan"].get("fact_changes") or [] if change.strip()]
        if changes:
            scene += "\n近况变化（以此为准，覆盖下方快照里的对应项）：" + "；".join(changes)
        if share:
            head = SHARE_STYLE.format(source=share["source"])
        elif record["plan"]["face_cover"]["method"] == "shown":
            head = STYLE_FACE_SHOWN
        else:
            head = STYLE_CASUAL if record["plan"]["photo_kind"] == "casual" else STYLE
        return (head + "\n" + style + "\n\n本次拍摄：\n" + scene
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

    async def _decline(self, record: dict[str, Any], code: str, reason: str, guidance: str) -> None:
        record["decline"] = {"reason_code": code, "reason": reason, "planner_guidance": guidance}
        await self.event(record, "declined", reason)
        if quiet(record):
            return
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=record["stream_id"],
                intent="这次不拍摄照片。\n"
                       + reason + "\n" + guidance,
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
        # 图片本身已同步进上下文，这里只补它回应的是哪条请求。排队时送达和新的调用可能同时出现，
        # 没有这一行 planner 只能猜（11-04 把穿搭照当成了拍脚照）。
        note = delivery_note(record)
        try:
            result = await self.ctx.maisaka.context.append(
                stream_id=record["stream_id"], segments=[{"type": "text", "data": note}], visible_text=note,
                source_kind="plugin_camera_delivery", message_id="camera-delivery:" + record["id"],
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("主程序未确认照片对应请求已回填")
        except Exception:
            record["events"].append({"at": timestamp(), "stage": "context_warning", "text": "图片已发送，但对应请求回填失败"})
            await asyncio.to_thread(self.store.save, record)
            _logger.exception("[相机] 照片对应请求回填失败 %s", record["id"])
