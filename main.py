"""bus activity：群里问公交的事，插件查真实数据，LLM 按语境说话。

只留一个 LLM 工具入口——问什么、怎么答都交给 LLM，
插件不抢着发消息，也不预设句子格式。
"""

import asyncio
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 插件内相对导入；单文件调试时按顶层模块兜底
    from .query import answer
except ImportError:
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from query import answer


class BusActivity(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.default_city = (config or {}).get("default_city") or "玉林市"

    @filter.llm_tool(name="bus_query")
    async def bus_query(self, event: AstrMessageEvent, question: str) -> str:
        """查公交实时信息。用户问到公交、几路车、某站有哪几路、从某站到某站怎么坐、
        某路车到哪了、还有几站、几分钟到、这里还有哪条线路有车的时候调用。

        这是唯一的公交数据来源，不要用别的办法查公交（也别自己去请求公交网站），
        查不到就照实说查不到。

        返回的是查询结果原文，请按用户这一句的问法用人话转述：他关心什么就先说什么，
        车牌、站名、分钟和站数必须用返回里的原值，不要自己换算，也不要补充没查到的信息。
        返回「没查到」时照实说，别编线路和车辆。

        Args:
            question(string): 用户关于公交的原话，原样传入，不要改写
        """
        try:
            text = await asyncio.to_thread(answer, question, self.default_city)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            return "查询接口这会儿没响应，照实说没查到、过会儿再试。"
        logger.info(f"公交查询: {question[:30]} -> {(text or '没查到')[:60]}")
        return text or "没查到这条线路或这个站点，照实说没查到。"
