"""服饰图片资产，以及从 planner 工具调用到 replyer 的轮次关联。"""

from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncio
import copy
import tomllib


REFERENCE_TITLE = "【Mittes 的服饰参考资料】"
TURN_ARG = "_mittes_outfit_turn"
QUERY_ARG = "_mittes_outfit_query"
QUERIES_ARG = "_mittes_outfit_queries"
IMAGES_ARG = "_mittes_outfit_images"


class OutfitImages:
    """按套装名取固定设定图；首次读取和编码在线程里完成，后续复用。"""

    def __init__(self, manifest: Path) -> None:
        self._manifest = manifest
        self._paths: dict[str, Path] = {}
        self._encoded: dict[str, str] = {}

    def load(self, outfit_names: set[str]) -> None:
        with self._manifest.open("rb") as handle:
            images = tomllib.load(handle)["images"]
        if set(images) != outfit_names:
            raise ValueError("服饰图片映射与 wardrobe.toml 的套装名称不一致")
        paths = {name: self._manifest.parent / path for name, path in images.items()}
        for path in paths.values():
            if not path.is_file():
                raise FileNotFoundError(f"缺少服饰设定图：{path}")
        self._paths = paths
        self._encoded.clear()

    async def image(self, name: str) -> str:
        if name not in self._paths:
            raise ValueError(f"「{name}」没有固定服饰设定图")
        if name not in self._encoded:
            self._encoded[name] = await asyncio.to_thread(self._read_image, self._paths[name])
        return self._encoded[name]

    @staticmethod
    def _read_image(path: Path) -> str:
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"服饰设定图不是 PNG：{path}")
        return b64encode(data).decode("ascii")


@dataclass
class _TurnQueries:
    turn_id: str
    queries: list[str] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)


class OutfitQueryBridge:
    """每个会话只保留当前逻辑轮的查询；replyer 开始时另存不可变的套装快照。

    before_request 每次工具续轮都会触发，不能在那里清空。使用 after_response
    已绑定的 logical_turn_id 区分新一轮，并将内部关联参数写进工具实参。
    不依赖 planner 猜套装、抄标记，也不把这些参数写进资料正文。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _TurnQueries] = {}

    def tag_calls(self, session_id: str, output_items: list[Any]) -> list[Any]:
        updated = copy.deepcopy(output_items)
        if not session_id:
            return updated
        for item in updated:
            if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
                continue
            call = item["tool_call"]
            turn_id = str(item["meta"].get("logical_turn_id") or "")
            if not turn_id:
                continue
            turn = self._sessions.get(session_id)
            if turn is None or turn.turn_id != turn_id:
                turn = _TurnQueries(turn_id)
                self._sessions[session_id] = turn
            args = call["args"]
            if call["func_name"] == "get_mittes_outfit":
                query_id = call["call_id"]
                if query_id not in turn.queries:
                    turn.queries.append(query_id)
                args[TURN_ARG] = turn_id
                args[QUERY_ARG] = query_id
            elif call["func_name"] == "reply":
                # 同一批调用按执行顺序关联：只包括这个 reply 之前的查询。
                args[TURN_ARG] = turn_id
                args[QUERIES_ARG] = list(turn.queries)
                args.pop(IMAGES_ARG, None)
        return updated

    def record(self, session_id: str, turn_id: str, query_id: str, name: str) -> None:
        turn = self._sessions.get(session_id)
        if turn is not None and turn.turn_id == turn_id and query_id in turn.queries:
            turn.results[query_id] = name

    def planner_references(self, session_id: str, items: list[Any]) -> list[tuple[int, str]]:
        """只补充请求中成功查询对应的图片；图片不进入可转发的聊天历史。"""
        turn = self._sessions.get(session_id)
        if turn is None:
            return []
        references: list[tuple[int, str]] = []
        for index, item in enumerate(items):
            if (
                item.get("item_type") == "FunctionCallOutputItem"
                and item.get("tool_name") == "get_mittes_outfit"
                and item.get("success") is True
                and item.get("meta", {}).get("logical_turn_id") == turn.turn_id
            ):
                name = turn.results.get(item["call_id"])
                if name is not None:
                    references.append((index, name))
        return references

    def snapshot(self, session_id: str, reply_args: dict[str, Any]) -> dict[str, Any]:
        """第一次 replyer 请求固定图片名单；后续重试不再读会话的可变状态。"""
        result = dict(reply_args)
        if IMAGES_ARG in result:
            return result
        turn = self._sessions.get(session_id)
        names: list[str] = []
        if turn is not None and turn.turn_id == result.get(TURN_ARG):
            for query_id in result.get(QUERIES_ARG, []):
                name = turn.results.get(query_id)
                if name is not None and name not in names:
                    names.append(name)
        result[IMAGES_ARG] = names
        return result

    def clear(self) -> None:
        self._sessions.clear()
