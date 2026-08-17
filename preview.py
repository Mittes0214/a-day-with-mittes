"""把每次 LLM 调用落盘成 WebUI 推理过程页可读的记录。

WebUI 那个页面**没有上报接口**，它直接扫
``logs/maisaka_prompt/{stage}/{session}/{毫秒时间戳}.json``；
payload 的格式（``schema_version: 6``）写死在主程序
``src/webui/routers/reasoning_process.py`` 里，主程序的 ``PromptPreviewLogger``
只负责落盘和清理、不负责拼装。所以这里自带一份拼装代码，
写法参考 ``plugins/16_mittes_qzone/preview.py``，区别是本插件只有纯文本调用，
图片相关处理全部去掉。

隐形耦合点：schema v6 是本插件与主程序之间**没有接口保障**的约定。
上游改了字段名，这里写出的文件会被 WebUI 静默忽略——页面上空着且不报错。
同步主程序版本时要主动来查这里，详见 ``doc/插件适配记录/``。
"""

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import json
import re
import time
import uuid


JST = ZoneInfo("Asia/Tokyo")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# WebUI 侧的 payload 格式版本，见 src/webui/routers/reasoning_process.py:170,331。
# 注意别跟 CONTEXT_ITEM_SCHEMA_VERSION（item 级，值为 1）搞混。
_SCHEMA_VERSION = 6


class PromptPreview:
    """本插件的推理记录落盘器。"""

    def __init__(self, enabled: bool, max_records: int, base_dir: Path | None = None) -> None:
        """初始化落盘器。

        Args:
            enabled: 是否落盘；主程序的预览开关管不到插件，这里必须自带一个。
            max_records: 每个目录最多保留多少条记录。
            base_dir: 记录根目录，默认取主程序的 ``logs/maisaka_prompt``。
        """
        project_root = Path(__file__).resolve().parents[2]
        self.enabled = enabled
        self.max_records = max(20, max_records)
        self.base_dir = base_dir or project_root / "logs" / "maisaka_prompt"

    def record(
        self,
        *,
        stage: str,
        session: str,
        request_kind: str,
        prompt: str | list[dict[str, Any]],
        result: dict[str, Any],
        selection_reason: str,
        output_title: str,
    ) -> Path | None:
        """写入一条推理记录。

        Args:
            stage: WebUI 上的分类目录名，本插件统一用 ``a_day_with_mittes``。
            session: 会话目录名；批量生成没有聊天会话，用 ``schedule_batch``。
            request_kind: 调用类型，用于在同一分类里区分不同用途。
            prompt: 本次请求的 prompt。
            result: ``ctx.llm.generate`` 的返回。
            selection_reason: 列表页展示的一句话说明。
            output_title: 详情页里输出区块的标题。

        Returns:
            Path | None: 写入的文件路径；未启用时返回 ``None``。
        """
        if not self.enabled:
            return None

        target_dir = self.base_dir / _safe_name(stage) / _safe_name(session)
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = self._build_payload(
            request_kind=request_kind,
            prompt=prompt,
            result=result,
            selection_reason=selection_reason,
            output_title=output_title,
        )
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        base_stem = str(int(time.time() * 1000))
        suffix = 0
        while True:
            stem = base_stem if suffix == 0 else f"{base_stem}_{suffix}"
            path = target_dir / f"{stem}.json"
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(content)
                break
            except FileExistsError:
                suffix += 1

        self._trim(target_dir)
        return path

    def _build_payload(
        self,
        *,
        request_kind: str,
        prompt: str | list[dict[str, Any]],
        result: dict[str, Any],
        selection_reason: str,
        output_title: str,
    ) -> dict[str, Any]:
        """拼装 schema v6 payload。"""
        response = str(result.get("response") or result.get("content") or result.get("text") or "")
        # 时间戳统一 JST，跟日程时间保持一致，否则 WebUI 里的时间对不上
        now = datetime.now(JST).isoformat()

        output_items: list[dict[str, Any]] = []
        if response:
            output_items.append(
                {
                    "item_type": "AssistantMessageItem",
                    "meta": _new_meta(now),
                    "parts": [{"type": "text", "text": response}],
                }
            )

        return {
            "schema_version": _SCHEMA_VERSION,
            "request": {
                "kind": _safe_name(request_kind),
                "selection_reason": selection_reason,
            },
            "metadata": {
                "model_name": str(result.get("model_name") or result.get("model") or ""),
                "prompt_tokens": _nonnegative_int(result.get("prompt_tokens")),
                "completion_tokens": _nonnegative_int(result.get("completion_tokens")),
                "total_tokens": _nonnegative_int(result.get("total_tokens")),
            },
            "presentation": {"output_title": output_title},
            "request_items": _serialize_prompt(prompt, now),
            "output_items": output_items,
            "tool_definitions": [],
            "generation_attempts": [],
        }

    def _trim(self, target_dir: Path) -> None:
        """超出保留数量时删掉最老的记录。"""
        files = sorted(
            (path for path in target_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json"),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        overflow = len(files) - self.max_records
        for path in files[: max(0, overflow)]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue


def _serialize_prompt(prompt: str | list[dict[str, Any]], timestamp: str) -> list[dict[str, Any]]:
    """prompt → WebUI 的 request_items。"""
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    items: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            raw = {"role": "user", "content": str(raw)}
        role = str(raw.get("role") or "user").strip().lower()
        item_type = {
            "assistant": "AssistantMessageItem",
            "system": "SystemMessageItem",
        }.get(role, "UserMessageItem")
        items.append(
            {
                "item_type": item_type,
                "meta": _new_meta(timestamp),
                "parts": [{"type": "text", "text": str(raw.get("content") or "")}],
            }
        )
    return items


def _new_meta(timestamp: str) -> dict[str, Any]:
    """合成一个 item meta。"""
    return {
        "item_id": uuid.uuid4().hex,
        "logical_turn_id": None,
        "timestamp": timestamp,
    }


def _safe_name(value: str) -> str:
    """把任意字符串收敛成可以当目录名的形式。"""
    normalized = _SAFE_NAME_RE.sub("_", str(value or "").strip()).strip("._")
    return normalized or "unknown"


def _nonnegative_int(value: Any) -> int:
    """token 计数容错，缺失时按 0 处理。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
