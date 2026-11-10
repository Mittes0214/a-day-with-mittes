"""重置回执进入 Planner 与 Replyer 共用的聊天历史。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import importlib

import pytest


Plugin = importlib.import_module("plugins.01_a_day_with_mittes.plugin").ADayWithMittesPlugin


@pytest.mark.parametrize("result", [{"success": True}, {"success": False, "error": "unavailable"}])
def test_photo_reset_context(result):
    context = AsyncMock(return_value=result)
    plugin = SimpleNamespace(
        reset_photo_quota=AsyncMock(return_value=True),
        ctx=SimpleNamespace(send=SimpleNamespace(text=AsyncMock()),
                            maisaka=SimpleNamespace(context=SimpleNamespace(append=context))),
    )
    async def run():
        if result["success"]:
            assert await Plugin.cmd_pik_reset(plugin, "chat") == (True, "重置成功", True)
        else:
            with pytest.raises(RuntimeError, match="写入上下文失败"):
                await Plugin.cmd_pik_reset(plugin, "chat")
        plugin.reset_photo_quota.assert_awaited_once()
        plugin.ctx.send.text.assert_awaited_once_with("重置成功", "chat")
        context.assert_awaited_once()
        args = context.call_args
        assert args.args[0] == "chat"
        assert args.args[1] == [{"type": "text", "data": args.kwargs["visible_text"]}]
        assert "/pik reset" in args.kwargs["visible_text"]
        assert "重置成功" in args.kwargs["visible_text"]
        assert "用户、聊天和露脸照" in args.kwargs["visible_text"]
        assert "replyable" not in args.kwargs
    asyncio.run(run())
