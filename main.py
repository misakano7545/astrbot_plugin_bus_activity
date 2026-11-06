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
    from .query import BUS_RE, CITYNUM_RE, AT_RE, answer, detect_city, looks_like_ask
except ImportError:
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from query import BUS_RE, CITYNUM_RE, AT_RE, answer, detect_city, looks_like_ask

# 模型嘴里那句「只接了玉林的数据源」是旧插件的谎话，发出去之前整句删掉
FALSE_CLAIM = re.compile(r"数据源|只接了|只有玉林|玉林的接口|"
                         r"查不了.{0,8}(公交|线路)|(公交|线路).{0,8}查不了")
# 插件自己的硬结论（换乘方案查不到、站表没找全、让你补城市）。
# 模型在群里见过这些说法，会把它们当成自己查过的结果复述出来——这一轮插件压根没给数据时就不许它说
PLUGIN_SPEAK = re.compile(r"站表里没(?:有|找全|找到)|没查到从|的换乘方案|说个城市名我再查"
                          r"|(?:公交|巴士|线路)?接口只认|只认线路和站|认不出?公司|不认公司")
SENT = re.compile(r"[^。！？\n]*[。！？\n]?")



class BusActivity(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.default_city = (config or {}).get("default_city") or "玉林市"

    @filter.regex(re.compile(BUS_RE.pattern + "|" + CITYNUM_RE.pattern))
    async def bus_fast(self, event: AstrMessageEvent):
        """问公交就直接查直接答，不经过模型判断——模型会漏答、会编「只接了玉林」。"""
        if not BUS_RE.search(event.message_str):
            # 只有「城市名 + 裸号」这一支（「西安616」）：城市得真在表里，不然「今天616」也进来
            if not detect_city(AT_RE.sub(" ", event.message_str), "", "")[1]:
                return
        if not looks_like_ask(event.message_str):
            return  # 带线路号的闲聊（「11路那车真挤」）交给模型，别抢成公交问答
        # 引用别人的消息追问（「那 39 路往哪开」）时，城市只在被引用的那句里
        ctx = " ".join(m.message_str for m in event.get_messages()
                       if getattr(m, "message_str", None))
        try:
            text = await asyncio.to_thread(answer, event.message_str, self.default_city, ctx,
                                           event.unified_msg_origin)
        except Exception as e:      # 没兜住的异常会变成群里那段「:( …出现异常」traceback
            logger.warning(f"公交直答失败: {e}")
            return
        if not text:
            return  # 没认出城市/站名/线路，交给 LLM 那一路
        logger.info(f"公交直答: {event.message_str[:30]} -> {text[:60]}")
        event._bus_hit = True       # 这一轮的数据是插件给的，模型复述插件腔就不算编
        yield event.plain_result(text)
        event.stop_event()

    @filter.on_decorating_result()
    async def drop_source_claims(self, event: AstrMessageEvent):
        """引用历史里模型自己说过「只有玉林数据」，它会一直复读——发之前把这句掐掉。"""
        res = event.get_result()
        if not res or not res.chain:
            return
        # 没查过就别说查过：模型会把插件说过的「站表里没找全，说个城市名我再查」当成自己的结果复述
        if not getattr(event, "_bus_hit", False):
            for comp in res.chain:
                text = getattr(comp, "text", "")
                if text and PLUGIN_SPEAK.search(text):
                    comp.text = "".join(
                        s for s in SENT.findall(text) if not PLUGIN_SPEAK.search(s)
                    ).strip()
        for comp in res.chain:
            text = getattr(comp, "text", "")
            if text and FALSE_CLAIM.search(text):
                comp.text = "".join(
                    s for s in SENT.findall(text) if not FALSE_CLAIM.search(s)
                ).strip()
        res.chain = [c for c in res.chain if getattr(c, "text", None) != ""]

    @filter.llm_tool(name="bus_query")
    async def bus_query(self, event: AstrMessageEvent, question: str) -> str:
        """查公交实时信息。用户这一条消息本身问到公交、几路车、某站有哪几路、从某站到某站怎么坐、
        某路车到哪了、还有几站、几分钟到、这里还有哪条线路有车的时候才调用。

        **只在用户这一条消息本身在问公交时才调**：他在闲聊、评价你刚才答得好不好、
        说「不用限定格式」「随便聊」，或者只是在跟别人说话时，绝对不要调用，
        也绝对不要把之前查过的线路和车辆再发一遍——那时候正常聊天就行。

        覆盖 598 个城市（含北京、上海、哈尔滨、南宁、各省会与大部分县市），
        不要用别的办法查公交（也别自己去请求公交网站），
        也不要在没调用这个工具的情况下回答「查不了」「只支持某某城市」——先查再说。

        返回的是查询结果原文，请按用户这一句的问法用人话转述：他关心什么就先说什么，
        车牌、站名、分钟和站数必须用返回里的原值，不要自己换算，也不要补充没查到的信息。
        返回「没查到」时照实说，别编线路和车辆；「没查到从…到…的换乘方案」「X 站表里没有这两个站」
        这类结论只能来自工具的返回，工具没这么说就不许自己写。

        Args:
            question(string): 用户关于公交的原话，原样传入，不要改写；
                他点名「从车来了查」「用掌上公交查」时这两个词必须留着
        """
        try:
            text = await asyncio.to_thread(answer, question, self.default_city, "",
                                           event.unified_msg_origin)
        except Exception as e:
            logger.warning(f"公交查询失败: {e}")
            return "查询接口这会儿没响应，照实说没查到、过会儿再试。"
        logger.info(f"公交查询: {question[:30]} -> {(text or '没查到')[:60]}")
        if text:
            event._bus_hit = True
            return text
        # 答不上来也必须给一句话：返 None 模型手里没料就直接沉默，群里看着像机器人死了。
        # 但别一律说「问一句是哪座城」——用户只是在吐槽刚才答得乱，模型会顺势编成
        # 「没查到从「…」到「…」的换乘方案」这种公交味的假结论（群里那次就是这样）
        return (f"「{question[:24]}」没查到对应的公交数据。"
                "如果用户是在问公交（写了城市、线路号或站名），问一句是哪座城的哪条线、哪一站；"
                "如果只是在聊天、吐槽、评价你刚才答得怎么样，就正常接话，别提线路和车辆。")
