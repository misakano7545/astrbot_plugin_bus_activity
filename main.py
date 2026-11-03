"""bus activity：群里问「玉林 G02 路有没有车」就回实时车辆。"""

import asyncio
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 插件内相对导入；单文件调试时按顶层模块兜底
    from .query import INTENT, answer
except ImportError:
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from query import INTENT, answer


class BusActivity(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.default_city = (config or {}).get("default_city") or "玉林市"

    @filter.regex(r"\d{1,3}\s*路|[A-Za-z]{1,2}\d{1,3}")
    async def bus(self, event: AstrMessageEvent):
        """查公交实时车辆，例：玉林 G02 路有没有车"""
        msg = event.message_str
        if not any(k in msg for k in INTENT):
            return
        try:
            text = await asyncio.to_thread(answer, msg, self.default_city)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            text = "公交数据源暂时没响应，稍后再问一次"
        if text:
            yield event.plain_result(text)
