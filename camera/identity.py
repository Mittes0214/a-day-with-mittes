"""别人发图问「这是你吗」：先认是不是她发出去的照片（相机照片、旧自拍）或当前 QQ 头像，认不出再按外貌比对。

照片经 QQ 转发会被重新压缩、缩放，字节摘要对不上，所以用 256 位差值哈希（dHash）比。
实测（2026-11-24，227 张相机成品）：压成 JPEG q55 并缩到 40% 后距离 ≤ 8；
不同照片之间除重发件外最近为 25。阈值取 16。裁切会让哈希失效，裁过的图交给外貌比对。
"""

from contextlib import closing
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import asyncio
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import time

from PIL import Image, ImageOps
import httpx

from .client import CPAClient, read_provider


TOKYO = ZoneInfo("Asia/Tokyo")
HASH_SIZE = 16
MATCH_DISTANCE = 16
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
AVATAR_TIMEOUT_SECONDS = 20
AVATAR_CACHE_SECONDS = 600
RESULT_CACHE_SECONDS = 1800
COMPARE_TIMEOUT_SECONDS = 90
COMPARE_MAX_EDGE = 1024
COMPARE_JPEG_QUALITY = 90
VERDICTS = ("match", "similar", "not_match", "uncertain")

COMPARE = """判断一张图里的人物是不是 Mittes。只返回 JSON。
图片1是用户发来、要判断的图；后面的图片都是 Mittes 本人的外貌参考（同一个人，不同衣服和发型）。
图片1可能是 AI 图、同人画、截图或别人的头像，画风和细节常有偏差。

先在图片1里找出所有人物，逐个和参考比，只要有一个人对得上就按她判断；不要把同框的别人的长相当成她的差异。

主特征（判断只看这三条）：
1. 头发：过腰长发，以银白为主，内层、发梢和脸侧自然过渡到浅樱粉；头顶有一束呆毛。
2. 眼睛：偏暖的红瞳，杏眼。
3. 脸：小而柔和的二次元少女脸。
发型（双马尾、麻花辫、披发、盘发）会随场合变化，不算差异。

不算差异的：猫耳、猫尾、铃铛项圈、丝带等道具和发饰（这些是衣服道具，不是她的身体特征，戴不戴都一样）、
服装、姿势、表情、背景、光线、画风（Q版、厚涂、像素等）、构图，以及发色和瞳色的浓淡冷暖。

判级：
match = 三条主特征都对得上（细节有偏差照样 match）
similar = 有一条主特征明显不对，但整体仍像她
not_match = 主特征本质不同（例如整头黑发、金发、短发、蓝瞳），或明显是另一个角色
uncertain = 没有清晰人物、遮挡严重、太小看不清，或证据互相冲突

返回 verdict(match|similar|not_match|uncertain)、confidence(0~1)、
differences(数组，最多 3 条，每条 12 字内，只写主特征上的差异，match 时一般为空)、
reason(一句简短中文理由，只说看到的外貌依据)。不要判断或猜测现实人物身份。"""

VERDICT_LABELS = {
    "match": "长相是 Mittes",
    "similar": "很像 Mittes，但有出入",
    "not_match": "不是 Mittes",
    "uncertain": "看不清，无法判断",
}

ADVICE = {
    "match": (
        "长相对得上，可以认下画的是她；这张不在她的照片记录里，说不出是什么时候拍的，不要编拍摄时间地点。"
        "不要拿猫耳、配饰、衣服或画风的差别否认，也不要刚认下又改口。"
    ),
    "similar": "先说像，再点出一处具体差别（{differences}），不要整段否认，也不要直接认成是自己。",
    "not_match": "第一句就明确说不是她；不要用「也算是我吧」「行吧那是我」这类含糊认领的说法。",
    "uncertain": "看不清就说看不清，不要猜，也不要硬认。",
}


def _clause(text: str) -> str:
    """去掉句末标点，方便接进整句。"""
    return text.strip().rstrip("。.！!；;，,")


def dhash(raw: bytes) -> int:
    """256 位差值哈希；动图只取第一帧，透明底按白底算。"""
    with Image.open(BytesIO(raw)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGBA")
    flat = Image.new("RGBA", image.size, "white")
    flat.alpha_composite(image)
    gray = flat.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for y in range(HASH_SIZE):
        row = gray[y * (HASH_SIZE + 1):(y + 1) * (HASH_SIZE + 1)]
        for x in range(HASH_SIZE):
            bits = bits << 1 | (row[x] > row[x + 1])
    return bits


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_jpeg(raw: bytes) -> bytes:
    """统一方向、白底铺平、缩小并转 JPEG，控制视觉比对的请求体积。"""
    with Image.open(BytesIO(raw)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGBA")
    image.thumbnail((COMPARE_MAX_EDGE, COMPARE_MAX_EDGE), Image.Resampling.LANCZOS)
    flat = Image.new("RGBA", image.size, "white")
    flat.alpha_composite(image)
    output = BytesIO()
    flat.convert("RGB").save(output, format="JPEG", quality=COMPARE_JPEG_QUALITY)
    return output.getvalue()


def message_image(message: Dict[str, Any], image_index: int) -> bytes:
    """从带二进制数据的序列化消息里取第 image_index 张图（图片和表情按出现顺序一起数）。"""
    segments = [segment for segment in message.get("raw_message") or []
                if isinstance(segment, dict) and segment.get("type") in {"image", "emoji"}]
    if not segments:
        raise ValueError("目标消息中没有图片")
    if not 0 <= image_index < len(segments):
        raise ValueError(f"目标消息只有 {len(segments)} 张图片，image_index={image_index} 超出范围")
    encoded = str(segments[image_index].get("binary_data_base64") or "").strip()
    if not encoded:
        raise ValueError("目标图片的二进制数据不可用")
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("目标图片的 Base64 数据无效") from exc


def image_mime(raw: bytes) -> Tuple[str, str]:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("图片格式不受支持，仅支持 JPEG、PNG、GIF 或 WebP")


def validate_qq(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\d{1,20}", value):
        raise ValueError("目标 QQ 号必须是纯数字")
    return value


async def fetch_qq_avatar(qq: str) -> bytes:
    async with httpx.AsyncClient(timeout=AVATAR_TIMEOUT_SECONDS, follow_redirects=True, trust_env=False) as client:
        response = await client.get(AVATAR_URL.format(qq=qq))
        response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"QQ 头像接口返回了非图片内容：{content_type or '未知类型'}")
    if not response.content:
        raise ValueError("QQ 头像接口返回了空图片")
    return response.content


class PhotoArchive:
    """她公开过的相机照片：发到聊天里的成品，和已发到 QQ 空间的每日分享配图。
    11 月以前旧自拍功能发的 5 张形象照没有相机记录，哈希另存在 ``legacy_selfies.json``。

    哈希按成品 sha256 缓存在 ``photo_hashes.db``，新照片在下次查询时补算。
    """

    def __init__(self, camera_root: Path, daily_db: Path) -> None:
        self.camera_root = camera_root
        self.daily_db = daily_db
        self.db = camera_root / "photo_hashes.db"

    def initialize(self) -> None:
        self.camera_root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS hashes (sha256 TEXT PRIMARY KEY, dhash TEXT NOT NULL)")

    def published(self) -> List[Dict[str, Any]]:
        qzone: set[str] = set()
        if self.daily_db.is_file():
            with closing(sqlite3.connect(f"file:{self.daily_db}?mode=ro", uri=True)) as con:
                for (raw,) in con.execute("SELECT record FROM posts WHERE status='published'"):
                    task_id = json.loads(raw).get("camera_task_id")
                    if task_id:
                        qzone.add(task_id)
        with closing(sqlite3.connect(f"file:{self.camera_root / 'camera.db'}?mode=ro", uri=True)) as con:
            rows = con.execute("SELECT id, record FROM tasks WHERE status IN ('sent','daily_share') "
                               "ORDER BY created_at").fetchall()
        photos: List[Dict[str, Any]] = []
        for task_id, raw in rows:
            record = json.loads(raw)
            channel = "qzone" if record["status"] == "daily_share" else "chat"
            # 重发的是旧照片的副本，以原件为准；没发出去的每日分享配图不算公开过
            if record.get("reuse") or (channel == "qzone" and task_id not in qzone):
                continue
            final = record.get("final_image")
            info = next((image for image in record.get("images") or [] if image.get("file") == final), None)
            if not final or info is None or not info.get("sha256"):
                continue
            plan = record.get("plan") or {}
            snapshot = record["snapshot"]
            photos.append({
                "id": task_id, "path": self.camera_root / task_id / final, "sha256": info["sha256"],
                "taken_at": datetime.fromisoformat(snapshot["captured_at"]).astimezone(TOKYO).strftime("%Y-%m-%d %H:%M"),
                "place": snapshot.get("place", ""), "title": snapshot.get("title", ""),
                "description": record.get("description", ""), "channel": channel,
                "stream_id": record["stream_id"], "requester_name": record.get("requester_name", ""),
                "helper": (record.get("share") or {}).get("source", ""),
                "contains_mittes": bool(plan.get("contains_mittes")),
            })
        return photos

    def match(self, target: int) -> Optional[Tuple[Dict[str, Any], int]]:
        """找哈希距离最近、且在阈值内的公开照片。"""
        photos = self.published()
        with closing(sqlite3.connect(self.db)) as con, con:
            known = dict(con.execute("SELECT sha256, dhash FROM hashes"))
            for photo in photos:
                if photo["sha256"] not in known and photo["path"].is_file():
                    known[photo["sha256"]] = format(dhash(photo["path"].read_bytes()), "x")
                    con.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?)", (photo["sha256"], known[photo["sha256"]]))
        best: Optional[Tuple[Dict[str, Any], int]] = None
        for photo in photos:
            if photo["sha256"] not in known:
                continue
            gap = distance(target, int(known[photo["sha256"]], 16))
            if gap <= MATCH_DISTANCE and (best is None or gap < best[1]):
                best = (photo, gap)
        return best


def describe_photo(photo: Dict[str, Any], stream_id: str) -> str:
    who = f"{photo['helper']}帮她拍的" if photo["helper"] else "她自己用手机拍的"
    if photo["channel"] == "qzone":
        where = "发在她的 QQ 空间说说里"
    elif photo["stream_id"] == stream_id:
        where = f"当时就发在这个聊天里（给{photo['requester_name'] or '群友'}拍的）"
    else:
        where = "之前发在别的聊天里"
    subject = "" if photo["contains_mittes"] else "画面里拍的是身边的东西或景色，没有她本人；"
    return (f"东京时间 {photo['taken_at']} 在{photo['place']}（{photo['title']}）{who}，{where}。"
            f"{subject}照片里能看到：{_clause(photo['description'])}")


class IdentityService:
    """``identify_self_image`` 与 ``view_qq_avatar`` 的实现。"""

    def __init__(self, ctx: Any, plugin_dir: Path, data_dir: Path,
                 get_config: Callable[..., Any], client_factory: Any = CPAClient,
                 avatar_fetcher: Callable[[str], Any] = fetch_qq_avatar) -> None:
        self.ctx = ctx
        self.plugin_dir = plugin_dir
        self.archive = PhotoArchive(data_dir / "camera", data_dir / "daily_share" / "daily_share.db")
        self.get_config = get_config
        self.client_factory = client_factory
        self.avatar_fetcher = avatar_fetcher
        self._avatar: Optional[Tuple[float, int]] = None
        self._results: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._reference_images: Optional[List[bytes]] = None
        legacy = json.loads((plugin_dir / "camera" / "legacy_selfies.json").read_text())["photos"]
        self._legacy = [(entry["title"], int(entry["dhash"], 16)) for entry in legacy]

    async def start(self) -> None:
        await asyncio.to_thread(self.archive.initialize)

    async def _bot_qq(self) -> str:
        return validate_qq(str(await self.ctx.config.get("bot.qq_account", "")))

    async def _message(self, stream_id: str, msg_id: str, *, binary: bool) -> Dict[str, Any]:
        if not stream_id:
            raise ValueError("无法获取当前会话")
        message = await self.ctx.message.get_by_id(msg_id, chat_id=stream_id, include_binary_data=binary)
        if not isinstance(message, dict):
            raise ValueError(f"当前会话中找不到消息 {msg_id}")
        if str(message.get("session_id") or "") not in {"", stream_id}:
            raise ValueError("目标消息不属于当前会话")
        return message

    # ---------- 头像 ----------

    async def view_avatar(self, stream_id: str, msg_id: str, qq_account: str) -> Dict[str, Any]:
        """取 QQ 头像作为工具图片结果返回，不向聊天发送。"""
        if qq_account:
            qq, label = validate_qq(qq_account), f"QQ {qq_account} "
        elif msg_id:
            message = await self._message(stream_id, msg_id, binary=False)
            if str(message.get("platform") or "qq").lower() != "qq":
                raise ValueError("目标消息不是 QQ 消息，无法获取 QQ 头像")
            user = (message.get("message_info") or {}).get("user_info") or {}
            qq = validate_qq(str(user.get("user_id") or ""))
            name = str(user.get("user_cardname") or user.get("user_nickname") or "").strip()
            label = f"用户「{name}」" if name else f"QQ {qq} "
        else:
            qq, label = await self._bot_qq(), "Mittes "
        raw = await self.avatar_fetcher(qq)
        fmt, mime = image_mime(raw)
        return {
            "success": True, "name": "view_qq_avatar",
            "content": f"已获取{label}当前的 QQ 头像，图片已提供给模型查看，未发送到聊天。",
            "qq_account": qq,
            "content_items": [{
                "type": "image", "data": base64.b64encode(raw).decode(), "mime_type": mime,
                "name": f"QQ头像-{qq}.{fmt}", "description": f"{label}当前的 QQ 头像",
                "metadata": {"context_key": f"qq_avatar:{qq}", "source_url": AVATAR_URL.format(qq=qq)},
            }],
        }

    async def _avatar_hash(self) -> int:
        now = time.monotonic()
        if self._avatar is None or now - self._avatar[0] > AVATAR_CACHE_SECONDS:
            raw = await self.avatar_fetcher(await self._bot_qq())
            self._avatar = (now, await asyncio.to_thread(dhash, raw))
        return self._avatar[1]

    # ---------- 认图 ----------

    async def identify(self, stream_id: str, msg_id: str, image_index: int) -> Dict[str, Any]:
        message = await self._message(stream_id, msg_id, binary=True)
        raw = message_image(message, image_index)
        digest = hashlib.sha256(raw).hexdigest()
        cached = self._results.get(digest)
        if cached is not None and time.monotonic() - cached[0] < RESULT_CACHE_SECONDS:
            result = cached[1]
        else:
            result = await self._identify(raw)
            self._results = {key: value for key, value in self._results.items()
                             if time.monotonic() - value[0] < RESULT_CACHE_SECONDS}
            self._results[digest] = (time.monotonic(), result)
        return {"success": True, "name": "identify_self_image", "content": self._render(result, stream_id),
                "result": result["result"], "target_msg_id": msg_id, "image_index": image_index,
                **{key: result[key] for key in ("photo_id", "verdict", "confidence") if key in result}}

    async def _identify(self, raw: bytes) -> Dict[str, Any]:
        target = await asyncio.to_thread(dhash, raw)
        found = await asyncio.to_thread(self.archive.match, target)
        avatar = distance(target, await self._avatar_hash()) <= MATCH_DISTANCE
        if found is not None:
            photo, gap = found
            return {"result": "photo", "photo": {k: v for k, v in photo.items() if k != "path"},
                    "photo_id": photo["id"], "distance": gap, "avatar": avatar}
        legacy = min(((title, distance(target, value)) for title, value in self._legacy), key=lambda item: item[1])
        if legacy[1] <= MATCH_DISTANCE:
            return {"result": "legacy_selfie", "title": legacy[0], "distance": legacy[1], "avatar": avatar}
        if avatar:
            return {"result": "avatar"}
        return {"result": "compare", **await self._compare(raw)}

    def _references(self) -> List[bytes]:
        """外貌参考：07 发色与身形、人物画风主基准（也是她本人），校验摘要后缓存。"""
        if self._reference_images is not None:
            return self._reference_images
        root = (self.plugin_dir / "camera" / "references").resolve()
        manifest = json.loads((root / "references.json").read_text())
        entries = [manifest["identity"]["body"],
                   next(entry for entry in manifest["references"] if entry["category"] == "portrait")]
        images = []
        for entry in entries:
            target = (root / entry["file"]).resolve()
            if not target.is_relative_to(root):
                raise ValueError("外貌参考图路径越界")
            data = target.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("外貌参考图摘要不一致：" + entry["file"])
            images.append(to_jpeg(data))
        self._reference_images = images
        return images

    async def _compare(self, raw: bytes) -> Dict[str, Any]:
        provider = await self.get_config("photo.api_provider", "CLIProxyAPI")
        model = await self.get_config("photo.check_model", "gpt-6.2-sol")
        base_url, key = await asyncio.to_thread(read_provider, provider)
        images = [await asyncio.to_thread(to_jpeg, raw), *await asyncio.to_thread(self._references)]
        client = self.client_factory(base_url, key, COMPARE_TIMEOUT_SECONDS)
        try:
            data, _usage = await client.json(model, COMPARE, {"images": len(images)}, images=images)
        finally:
            await client.close()
        verdict = data.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"外貌比对返回了未知结论：{verdict!r}")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("外貌比对返回的置信度格式错误")
        differences = data.get("differences")
        reason = data.get("reason")
        if not isinstance(differences, list) or not isinstance(reason, str) or not reason.strip():
            raise ValueError("外貌比对返回的差异或理由格式错误")
        return {"verdict": verdict, "confidence": float(confidence),
                "differences": [str(item).strip() for item in differences if str(item).strip()][:3],
                "reason": reason.strip()}

    @staticmethod
    def _render(result: Dict[str, Any], stream_id: str) -> str:
        if result["result"] == "photo":
            avatar = "她现在的 QQ 头像也是用这张照片做的。" if result["avatar"] else ""
            return ("这张就是 Mittes 公开过的照片：" + describe_photo(result["photo"], stream_id) + "。" + avatar
                    + "直接认下，可以说出是什么时候、在哪拍的；说照片内容时以上面能看到的为准，不用再比对长相。")
        if result["result"] == "legacy_selfie":
            avatar = "她现在的 QQ 头像也是这张。" if result["avatar"] else ""
            return (f"这张是 Mittes 以前发过的自拍「{result['title']}」（11 月以前发的形象照，没有记录具体拍摄时间和地点）。"
                    + avatar + "直接认下是她自己的自拍；不要编具体的拍摄时间地点，也不用再比对长相。")
        if result["result"] == "avatar":
            return "这张是 Mittes 现在的 QQ 头像。直接认下这是她的头像，不用再比对长相。"
        verdict = result["verdict"]
        advice = ADVICE[verdict].format(differences="、".join(result["differences"]) or "有出入的地方")
        return (f"这张不是 Mittes 发过的照片，也不是她现在的头像。按外貌比对：{VERDICT_LABELS[verdict]}，"
                f"置信度 {round(result['confidence'] * 100)}%；依据：{_clause(result['reason'])}。{advice}")
