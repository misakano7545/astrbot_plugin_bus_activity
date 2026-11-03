"""bus activity：群里问「玉林 G02 路有没有车」就回实时车辆，也管「A 站到 B 站怎么坐」。"""

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

    # 线路号 / 「从A到B」/「几路车」都算进公交话题
    @filter.regex(r"\d{1,3}\s*路|[A-Za-z]{1,2}\d{1,3}|从.{1,10}?到.{1,10}|几路")
    async def bus(self, event: AstrMessageEvent):
        """查公交实时车辆，例：玉林 G02 路有没有车 / 从十字街到火车站坐什么车"""
        msg = event.message_str
        if not any(k in msg for k in INTENT):
            return
        try:
            text = await asyncio.to_thread(answer, msg, self.default_city)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            text = "公交这会儿查不动，过一会儿再问我一次"
        if text:
            logger.info(f"公交作答: {msg[:30]} -> {text.splitlines()[0][:60]}")
            yield event.plain_result(text)
