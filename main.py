"""bus activity：群里问公交的事就答——实时车辆、某站有哪几路、从 A 到 B 怎么坐。

入口有两条：LLM 判断这是公交问题就调 bus_query 工具（看语境，不靠关键词）；
明显的「G02 路有没有车」走正则快路，省一次 LLM 往返。
"""

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

    async def _text(self, msg):
        try:
            return await asyncio.to_thread(answer, msg, self.default_city)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            return "公交这会儿查不动，过一会儿再问我一次"

    # 语境匹配：是不是公交的事，交给 LLM 判断
    @filter.llm_tool(name="bus_query")
    async def bus_query(self, event: AstrMessageEvent, question: str):
        """查公交实时信息。用户问到公交、几路车、某站有哪几路、从某站到某站怎么坐、
        某路车到哪了/还有几站/几分钟到的时候调用。查不到就照原话说明，别编。

        Args:
            question(string): 用户关于公交的原话，原样传入，不要改写
        """
        text = await self._text(question)
        if not text:
            return
        logger.info(f"公交作答: {question[:30]} -> {text.splitlines()[0][:60]}")
        yield event.plain_result(text)
        event.stop_event()      # 答完就别让 LLM 再复述一遍
        yield

    # 明显的线路号/站名问法走快路，不等 LLM
    @filter.regex(r"\d{1,3}\s*路|[A-Za-z]{1,2}\d{1,3}|从.{1,10}?到.{1,10}|几路")
    async def bus(self, event: AstrMessageEvent):
        msg = event.message_str
        if not any(k in msg for k in INTENT):
            return
        text = await self._text(msg)
        if not text:
            return
        logger.info(f"公交作答: {msg[:30]} -> {text.splitlines()[0][:60]}")
        yield event.plain_result(text)
        event.stop_event()
        yield
