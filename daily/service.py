"""每日分享：每天零点从当日日程挑一个瞬间，用相机流程拍配图，写小红书风格配文，
等面板审核；通过后到那一段结束才发到 QQ 空间。

全程单线推进、状态落盘：重载后进行中的步骤接着做；发布结果不明时不重发。
生成、检查、配文任何一步失败都不发，不拿别的图或模板文字顶替。
"""

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import base64
import logging
import random
import re

import httpx

from ..camera.client import read_provider
from ..camera.service import DEFAULTS as CAMERA_DEFAULTS, PhotoService, photo_origin
from ..schedule.store import JST, to_minutes
from .prompts import CAPTION, PICK
from .store import RETRYABLE, WORKING, DailyShareStore, timestamp


_logger = logging.getLogger("a_day_with_mittes.daily_share")
SETTINGS = {
    "enabled": False, "run_at": "00:00",
    "snowluma_host": "127.0.0.1", "snowluma_port": 3000, "snowluma_token": "",
    "publish_timeout_seconds": 180.0,
}
# 通过审核后，在所选时段结束后再等这么多分钟（随机）才发，像是事后整理的
PUBLISH_DELAY_MINUTES = (10, 40)
TITLE_LIMIT, BODY_RANGE, TAG_RANGE, TAG_LIMIT = 20, (40, 400), (1, 6), 12
CAMERA_FAILED = {"declined", "failed", "interrupted", "generation_unknown", "delivery_unknown", "send_failed"}


class PublishUnconfirmed(RuntimeError):
    """发布请求可能已执行，但没有拿到确定回执。"""


def validate_caption(caption: Any) -> dict[str, Any]:
    if not isinstance(caption, dict):
        raise ValueError("配文不是 JSON 对象")
    title, body, tags = caption.get("title"), caption.get("body"), caption.get("tags")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_LIMIT + 4:
        raise ValueError(f"标题须为 1–{TITLE_LIMIT} 字")
    if not isinstance(body, str) or not BODY_RANGE[0] <= len(body.strip()) <= BODY_RANGE[1]:
        raise ValueError(f"正文须为 {BODY_RANGE[0]}–{BODY_RANGE[1]} 字")
    if not isinstance(tags, list) or not TAG_RANGE[0] <= len(tags) <= TAG_RANGE[1]:
        raise ValueError(f"话题须为 {TAG_RANGE[0]}–{TAG_RANGE[1]} 个")
    clean = []
    for tag in tags:
        tag = str(tag).strip().lstrip("#＃").strip() if isinstance(tag, str) else ""
        if not tag or len(tag) > TAG_LIMIT or re.search(r"\s", tag):
            raise ValueError("话题须为不含空格的短词")
        clean.append(tag)
    body = re.sub(r"\n{3,}", "\n\n", body.strip().replace("\r\n", "\n"))
    return {"title": title.strip(), "body": body, "tags": clean}


def post_text(caption: dict[str, Any]) -> str:
    """QQ 空间说说正文：标题、正文、话题，按小红书笔记的排法。"""
    return f"{caption['title']}\n\n{caption['body']}\n\n" + " ".join("#" + tag for tag in caption["tags"])


def slot_bounds(day: date, slot: str) -> tuple[datetime, datetime]:
    """时段在真实时间上的起止。跨零点的 24:00-26:00 自然落到次日。"""
    start, end = slot.split("-")
    base = datetime(day.year, day.month, day.day, tzinfo=JST)
    return base + timedelta(minutes=to_minutes(start)), base + timedelta(minutes=to_minutes(end))


class DailyShareService:
    def __init__(self, camera: PhotoService, data_dir: Path,
                 get_config: Callable[..., Awaitable[Any]],
                 day_material: Callable[[date], dict[str, Any] | None],
                 snapshot_at: Callable[[datetime], dict[str, Any]],
                 now: Callable[[], datetime] = lambda: datetime.now(JST),
                 publisher: Callable[[dict[str, Any], str, bytes], Awaitable[dict[str, Any]]] | None = None) -> None:
        self.camera = camera
        self.store = DailyShareStore(data_dir / "daily_share")
        self.get_config = get_config
        self.day_material = day_material
        self.snapshot_at = snapshot_at
        self.now = now
        self.publisher = publisher or self._publish_qzone
        self.task: asyncio.Task[None] | None = None
        self._last_auto_day = ""

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize)
        await asyncio.to_thread(self.store.recover)
        self.task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def settings(self) -> dict[str, Any]:
        return {key: await self.get_config(f"daily_share.{key}", default) for key, default in SETTINGS.items()}

    async def camera_settings(self) -> dict[str, Any]:
        return {key: await self.get_config(f"photo.{key}", default) for key, default in CAMERA_DEFAULTS.items()}

    # ── 面板操作（经 admin_jobs 进来，只改状态，耗时的步骤交给后台循环）──
    async def generate(self, day: date, reason: str) -> dict[str, Any]:
        if not (await self.camera_settings())["enabled"]:
            raise ValueError("拍照功能尚未开启")
        if self.day_material(day) is None:
            raise ValueError("这一天还没有日程")
        return await asyncio.to_thread(self.store.start, day.isoformat(), reason)

    async def recaption(self, day: date, note: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.transition, day.isoformat(), {"pending_review"},
                                       "captioning", "按面板意见重写配文" + (f"：{note}" if note else ""), note=note)

    async def approve(self, day: date, caption: dict[str, Any]) -> dict[str, Any]:
        caption = validate_caption(caption)
        record = await asyncio.to_thread(self.store.get, day.isoformat())
        if record is None or not record.get("pick"):
            raise ValueError("这一天还没有待审核的分享")
        _, end = slot_bounds(day, record["pick"]["slot"])
        publish_at = max(end + timedelta(minutes=random.randint(*PUBLISH_DELAY_MINUTES)), self.now())
        edited = caption != record.get("caption")
        return await asyncio.to_thread(
            self.store.transition, day.isoformat(), {"pending_review"}, "approved",
            f"面板审核通过{'（改过文字）' if edited else ''}，定于 {publish_at.astimezone(JST):%m-%d %H:%M} 发到 QQ 空间",
            caption=caption, caption_edited=edited or record.get("caption_edited", False),
            publish_at=publish_at.isoformat())

    async def withdraw(self, day: date) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.transition, day.isoformat(), {"approved"},
                                       "pending_review", "撤回审核，回到待审", publish_at="")

    async def reject(self, day: date) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.transition, day.isoformat(), {"pending_review", "approved"},
                                       "rejected", "面板审核不通过，不发布", publish_at="")

    # ── 后台循环 ──
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("[每日分享] 循环异常，稍后继续")
            await asyncio.sleep(20)

    async def tick(self) -> None:
        settings = await self.settings()
        if settings["enabled"]:
            await self._auto_start(settings)
        for record in await asyncio.to_thread(self.store.active):
            try:
                await self._advance(record, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.exception("[每日分享] %s 推进失败", record["day"])
                if record["status"] == "publishing":
                    await self._event(record, "publish_unknown", f"发布结果不明，未自动重发：{type(exc).__name__}: {str(exc)[:300]}")
                elif record["status"] in WORKING:
                    record["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                    await self._event(record, "failed", record["error"])

    async def _auto_start(self, settings: dict[str, Any]) -> None:
        """日历日到了 run_at 就生成这一天的分享（日程前一天中午就有了）。只做今天，不补以前。"""
        now = self.now()
        hour, minute = (int(part) for part in str(settings["run_at"]).split(":"))
        today = now.date()
        if now.time() < dt_time(hour, minute) or self._last_auto_day == today.isoformat():
            return
        if await asyncio.to_thread(self.store.get, today.isoformat()) is not None:
            self._last_auto_day = today.isoformat()
            return
        if self.day_material(today) is None:
            return  # 日程还没生成好，下一轮再看
        self._last_auto_day = today.isoformat()
        try:
            await self.generate(today, "每日定时生成")
        except ValueError as exc:
            _logger.warning("[每日分享] %s 未能开始：%s", today, exc)

    async def _event(self, record: dict[str, Any], stage: str, text: str) -> None:
        record["status"] = stage
        record["events"].append({"at": timestamp(), "stage": stage, "text": text})
        await asyncio.to_thread(self.store.save, record)
        _logger.info("[每日分享] %s %s %s", record["day"], stage, text)

    async def _advance(self, record: dict[str, Any], settings: dict[str, Any]) -> None:
        status = record["status"]
        if status == "picking":
            await self._pick(record)
        elif status == "shooting":
            await self._check_camera(record)
        elif status == "captioning":
            await self._caption(record)
        elif status == "approved" and datetime.fromisoformat(record["publish_at"]) <= self.now():
            await self._publish(record, settings)

    async def _client(self) -> tuple[Any, dict[str, Any]]:
        settings = await self.camera_settings()
        base_url, key = await asyncio.to_thread(read_provider, settings["api_provider"])
        return self.camera.client_factory(base_url, key, float(settings["timeout_seconds"])), settings

    async def _pick(self, record: dict[str, Any]) -> None:
        day = date.fromisoformat(record["day"])
        material = self.day_material(day)
        if material is None:
            raise ValueError("这一天的日程不见了")
        recent = await asyncio.to_thread(self.store.recent, record["day"])
        client, settings = await self._client()
        try:
            pick, usage = await client.json(settings["director_model"], PICK, {
                **material,
                "recent_shares": [{"day": r["day"], "title": r["pick"].get("title", ""), "subject": r["pick"]["subject"]}
                                  for r in recent],
            })
        finally:
            await client.close()
        record["pick_usage"] = usage
        if not isinstance(pick.get("skip"), bool) or not isinstance(pick.get("reason"), str):
            raise ValueError("选题没有返回有效的 skip 与 reason")
        if pick["skip"]:
            record["pick"] = pick
            await self._event(record, "skipped", "今天没有适合分享的瞬间：" + pick["reason"][:200])
            return
        segment = next((s for s in material["segments"] if s["slot"] == pick.get("slot")), None)
        if segment is None:
            raise ValueError("选题选了不存在的时段")
        if segment["kind"] == "睡眠" or to_minutes(segment["slot"].split("-")[1]) > 24 * 60:
            raise ValueError("选题选了睡眠或跨零点的时段")
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(pick.get("time") or "")):
            raise ValueError("选题的时刻格式无效")
        start, end = slot_bounds(day, segment["slot"])
        base = datetime(day.year, day.month, day.day, tzinfo=JST)
        moment = base + timedelta(minutes=to_minutes(pick["time"]))
        if not start <= moment < end:
            raise ValueError("选题的时刻不在所选时段内")
        for field in ("subject", "angle"):
            if not isinstance(pick.get(field), str) or not pick[field].strip():
                raise ValueError(f"选题缺少 {field}")
        pick["title"] = segment["title"]
        record["pick"] = pick
        record["moment"] = moment.isoformat()
        snapshot = self.snapshot_at(moment)
        instruction = (f"每日分享：Mittes 要把今天的这一刻发到 QQ 空间，像小红书的生活笔记配图。"
                       f"想分享的画面：{pick['subject'].strip()}。拍摄时刻就是快照的 captured_at。")
        camera_record = await self.camera.submit(
            instruction, f"daily-share:{record['day']}:{record['attempt']}", "", snapshot,
            daily_share={"day": record["day"], "attempt": record["attempt"]})
        record["camera_task_id"] = camera_record["id"]
        await self._event(record, "shooting",
                          f"选了 {pick['time']}「{segment['title']}」：{pick['subject'][:80]}，交给相机拍摄")

    async def _check_camera(self, record: dict[str, Any]) -> None:
        camera = await asyncio.to_thread(self.camera.store.get, record["camera_task_id"])
        if camera["status"] == "daily_share":
            record["photo"] = {
                "task_id": camera["id"], "file": camera["final_image"], "description": camera["description"],
                "origin": photo_origin(camera), "share": camera.get("share"),
                "contains_mittes": (camera.get("plan") or {}).get("contains_mittes"),
            }
            await self._event(record, "captioning", "配图检查通过，开始写配文")
        elif camera["status"] in CAMERA_FAILED:
            reason = (camera.get("decline") or {}).get("reason") or (camera["events"][-1]["text"] if camera["events"] else "")
            record["error"] = f"配图没有拍成（{camera['status']}）：{reason[:300]}"
            await self._event(record, "failed", record["error"])

    def _photo_bytes(self, record: dict[str, Any]) -> bytes:
        return (self.camera.store.root / record["photo"]["task_id"] / record["photo"]["file"]).read_bytes()

    async def _caption(self, record: dict[str, Any]) -> None:
        camera = await asyncio.to_thread(self.camera.store.get, record["photo"]["task_id"])
        snapshot = camera["snapshot"]
        raw = await asyncio.to_thread(self._photo_bytes, record)
        recent = await asyncio.to_thread(self.store.recent, record["day"], 5)
        client, settings = await self._client()
        try:
            caption, usage = await client.json(settings["director_model"], CAPTION, {
                "description": record["photo"]["description"], "photo_origin": record["photo"]["origin"],
                "angle": record["pick"]["angle"], "subject": record["pick"]["subject"],
                "snapshot": {key: snapshot.get(key) for key in (
                    "captured_at", "title", "place", "weather", "story", "topic", "company", "outfit")},
                "recent_titles": [r["caption"]["title"] for r in recent],
                "note": record.get("note", ""),
            }, images=[raw])
        finally:
            await client.close()
        record["caption"] = validate_caption(caption)
        record["caption_edited"] = False
        record["caption_usage"] = usage
        await self._event(record, "pending_review", "配文写好，等待面板审核")

    async def _publish(self, record: dict[str, Any], settings: dict[str, Any]) -> None:
        # 先原子地占住发布状态：面板此刻撤回或不通过时，这里会失败而不是照发
        record = await asyncio.to_thread(self.store.transition, record["day"], {"approved"},
                                         "publishing", "到点，发到 QQ 空间")
        raw = await asyncio.to_thread(self._photo_bytes, record)
        try:
            result = await self.publisher(settings, post_text(record["caption"]), raw)
        except PublishUnconfirmed as exc:
            await self._event(record, "publish_unknown", str(exc))
            return
        if result.get("status") == "ok" and result.get("retcode") == 0:
            record["published"] = {"at": timestamp(), "tid": str((result.get("data") or {}).get("tid") or "")}
            await self._event(record, "published", "已发到 QQ 空间")
            return
        record["error"] = f"SnowLuma 拒绝发布：{result.get('wording') or result.get('message') or result.get('retcode')}"
        await self._event(record, "publish_failed", record["error"])

    @staticmethod
    async def _publish_qzone(settings: dict[str, Any], content: str, image: bytes) -> dict[str, Any]:
        """经 SnowLuma OneBot HTTP 的 send_qzone_msg 发带图说说（与 02 插件同一通道）。写入不重试。"""
        url = f"http://{settings['snowluma_host']}:{int(settings['snowluma_port'])}/send_qzone_msg"
        token = str(settings["snowluma_token"] or "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        payload = {"content": content, "images": ["base64://" + base64.b64encode(image).decode()]}
        timeout = httpx.Timeout(float(settings["publish_timeout_seconds"]), connect=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.ConnectError as exc:
            return {"status": "failed", "retcode": -1, "wording": f"连不上 SnowLuma：{exc}"}
        except httpx.HTTPError as exc:
            raise PublishUnconfirmed(f"等待 SnowLuma 回执未完成（{type(exc).__name__}），说说可能已发出，请先核对空间") from exc
        if response.status_code >= 500:
            raise PublishUnconfirmed(f"SnowLuma 返回 HTTP {response.status_code}，发布结果未确认")
        if response.is_error:
            return {"status": "failed", "retcode": response.status_code, "wording": response.text[:200]}
        try:
            result = response.json()
        except ValueError as exc:
            raise PublishUnconfirmed("SnowLuma 回执无法解析，请先核对空间") from exc
        if not isinstance(result, dict):
            raise PublishUnconfirmed("SnowLuma 回执格式错误，请先核对空间")
        return result


__all__ = ["DailyShareService", "PublishUnconfirmed", "RETRYABLE", "post_text", "validate_caption"]
