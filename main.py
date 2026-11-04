"""bus activity：群里问公交的事，插件查真实数据。

两条入口：
1. 正则快路——句子里有公交相关词就先查，查到了直接发。模型爱偷懒、爱说
   「只接了玉林的数据」「查不了」，还会被 429 卡住，指望它每次老实调工具不现实。
2. LLM 工具 bus_query——话说得含糊、快路没查到时兜底。
"""

import asyncio
import re
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 插件内相对导入；单文件调试时按顶层模块兜底
    from .query import answer
except ImportError:
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from query import answer

# 模型嘴里那句「只接了玉林的数据源」是旧插件的谎话，发出去之前整句删掉
FALSE_CLAIM = re.compile(r"数据源|只接了|只有玉林|玉林的接口|"
                         r"查不了.{0,8}(公交|线路)|(公交|线路).{0,8}查不了")
SENT = re.compile(r"[^。！？\n]*[。！？\n]?")

# 像在问公交才去查：查不到自然返回空串，不耽误别的聊天
BUS_RE = re.compile(
    r"公交|巴士|公共汽车|公车|地铁|轨道交通|线路|几路|哪路|路车|末班|首班|到站|几站|"
    r"怎么坐|直达|换乘|路线|站点|车来了|掌上公交|\d{1,3}\s*路|(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{1,3}(?![A-Za-z0-9])"
)


class BusActivity(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.default_city = (config or {}).get("default_city") or "玉林市"

    @filter.regex(BUS_RE)
    async def bus_fast(self, event: AstrMessageEvent):
        """问公交就直接查直接答，不经过模型判断——模型会漏答、会编「只接了玉林」。"""
        # 引用别人的消息追问（「那 39 路往哪开」）时，城市只在被引用的那句里
        ctx = " ".join(m.message_str for m in event.get_messages()
                       if getattr(m, "message_str", None))
        text = await asyncio.to_thread(answer, event.message_str, self.default_city, ctx,
                                       event.unified_msg_origin)
        if not text:
            return  # 没认出城市/站名/线路，交给 LLM 那一路
        logger.info(f"公交直答: {event.message_str[:30]} -> {text[:60]}")
        yield event.plain_result(text)
        event.stop_event()

    @filter.on_decorating_result()
    async def drop_source_claims(self, event: AstrMessageEvent):
        """引用历史里模型自己说过「只有玉林数据」，它会一直复读——发之前把这句掐掉。"""
        res = event.get_result()
        if not res or not res.chain:
            return
        for comp in res.chain:
            text = getattr(comp, "text", "")
            if text and FALSE_CLAIM.search(text):
                comp.text = "".join(
                    s for s in SENT.findall(text) if not FALSE_CLAIM.search(s)
                ).strip()
        res.chain = [c for c in res.chain if getattr(c, "text", None) != ""]

    @filter.llm_tool(name="bus_query")
    async def bus_query(self, event: AstrMessageEvent, question: str) -> str:
        """查公交实时信息。用户问到公交、几路车、某站有哪几路、从某站到某站怎么坐、
        某路车到哪了、还有几站、几分钟到、这里还有哪条线路有车的时候调用。

        覆盖 598 个城市（含北京、上海、哈尔滨、南宁、各省会与大部分县市），
        不要用别的办法查公交（也别自己去请求公交网站），
        也不要在没调用这个工具的情况下回答「查不了」「只支持某某城市」——先查再说。

        返回的是查询结果原文，请按用户这一句的问法用人话转述：他关心什么就先说什么，
        车牌、站名、分钟和站数必须用返回里的原值，不要自己换算，也不要补充没查到的信息。
        返回「没查到」时照实说，别编线路和车辆。

        Args:
            question(string): 用户关于公交的原话，原样传入，不要改写
        """
        try:
            text = await asyncio.to_thread(answer, question, self.default_city, "",
                                           event.unified_msg_origin)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            return "查询接口这会儿没响应，照实说没查到、过会儿再试。"
        logger.info(f"公交查询: {question[:30]} -> {(text or '没查到')[:60]}")
        return text or "没查到这条线路或这个站点，照实说没查到。"
