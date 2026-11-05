"""离线自检：python3 selftest.py

不联网，只验解析/匹配/排版这些纯逻辑；加 --live 再打一次真接口。
"""
import re

import sys
import types

import query

# 归一化：掌上「G02路」与车来了「G02」要能对上
assert query.norm_line("G02路") == query.norm_line("G02") == "G02"
assert query.norm_line("6路夜班") == "6夜班"
assert query.norm_line("13（临时停运）") == "13"
assert query.norm_line("机场1线") == "机场1"
assert query.norm_city("重庆城区") == query.norm_city("重庆市") == "重庆"
assert query.norm_city("香港特别行政区") == "香港"
assert query.norm_city("玉林市") == "玉林"

# 县名不能被剥成单字，否则县级单位在城市索引里整体消失（issue #1）
assert query.norm_city("容县") == "容县"
assert query.norm_city("威县") == "威县"
assert query.norm_city("丰县") == "丰县"
assert query.norm_city("玉林市") == "玉林"      # 三字以上照旧剥
assert query.norm_city("北流市") == "北流"
assert query.norm_city("城区") == "城区"        # 剥完不足两字就原样留
assert query.norm_city("长宁县") == "长宁"      # 三字县名照旧剥，简称能查到

# 从消息里取线路号 + 紧贴着的汉字段（县内线路用）
assert query.parse("玉林 G02 路有没有车") == ("G02", "玉林")   # 前缀也可能是城市，由 detect_city 分辨
assert query.parse("B1 有车吗") == ("B1", "")
assert query.parse("105路还有几站") == ("105", "")
assert query.parse("陆川 1 路有车吗") == ("1", "陆川")
assert query.parse("早上8点有车吗") is None  # 裸数字不带「路」不算线路
assert query.parse("玉林 6 路夜班还有几站") == ("6夜班", "玉林")   # 「路」后的定性词要一起吃掉
# 「查询 G02 路」的「查询」是动词，不是县名前缀——否则 detect_city 会拿它当城市去查
assert query.parse("查询 G02 路现在 512 车到哪个地方了？") == ("G02", "")
assert query.parse("查询一下 G02 路") == ("G02", "")
# 「环山二号线」不带「地铁」，号码夹在中间；前缀不能再把线名里的汉字当县名重复拼
assert query.parse("查一下西安环山二号线路线上几辆车") == ("西安环山二号", "")
assert query._lines("西安环山二号线有几辆车") == [("西安环山二号", "")]
assert query.BUS_RE.search("查一下西安环山二号线路线上几辆车")     # 否则 bus_query 返回 None，群里一个字都不回
# 「512 车」「513 号车」是车号；「11 路车」中间隔着「路」，不是车号
assert [g for g in query.VEH_RE.findall("现在 512 车到哪")[0]] == ["", "512"]
assert query.VEH_RE.findall("11 路车") == [] and query.VEH_RE.findall("513 号车")[0][1] == "513"

# 目录匹配：全等优先，前缀只在候选少时接受
cat = [("6路", "a"), ("6路夜班", "b")]
assert query._pick(cat, "6") == [("6路", "a")]
assert query._pick(cat, "9") == []
assert query._pick([("B11", 1), ("B112", 2), ("B113", 3)], "B11")[0][0] == "B11"
# 前缀候选 ≥ 3 时不再一律放弃：短的优先；全是等长候选才算真歧义（issue #3）
assert query._pick([("B1", 1), ("B12", 2), ("B13", 3), ("B14", 4)], "B1")[0][0] == "B1"
assert query._pick([("B12", 2), ("B13", 3), ("B14", 4)], "B1") == []        # B1 不在目录，不瞎猜
assert query._pick([("G01", 1), ("G02", 2), ("G03", 3)], "G0") == []        # 三个等长，真歧义
assert query._pick([("G01", 1), ("G02", 2), ("G0", 3)], "G0")[0][0] == "G0"
assert query._pick([("11路", 1), ("12路", 2), ("13路", 3)], "1") == []   # 真歧义，仍不瞎猜

# 排版：说话像群友，不报城市名、不标数据源
res = {"source": "掌上公交", "line": "1路",
       "groups": [("A站", [("桂A1", "某路口", 326)]), ("B站", [])]}
out = query.render("南宁", "1", res)
assert out.splitlines() == ["有，1路 现在 1 辆在路上",
                            "往A站：桂A1 某路口 326m"], out
# 两个方向都有车才念方向，空方向只在此时才提
out = query.render("南宁", "1", {"source": "掌上公交", "line": "1路",
                                 "groups": [("A站", [("桂A1", "某路口", 0)]), ("B站", [])]})
assert "往B站" not in out, out
# 没车时不能直说「没车」，要区分「上游缺 GPS」和「已收班」（issue #2）
assert "线路还在跑" in query.render(
    "南宁", "1", {"source": "掌上公交", "line": "1", "groups": [("", [])], "note": "线路还在跑，只是车机没报位置"})
assert "末班" in query.render(
    "南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])], "desc": "本线路末班时间已过"})
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])]}) == \
    "1 这会儿没车"
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])],
                                  "note": "末班 19:50 已经开走了"}) == \
    "1 这会儿没车，末班 19:50 已经开走了"
# 线路不存在 / 有近似线名 / 上游异常：别把机器串甩进群里
assert query.render("玉林", "999", {"error": "没这条线路"}).startswith("没查到「999」这条线")
# 没查到要给同城近邻，否则模型自己解释成「这数据源没有这个城市」
assert "这边有的是" in query.render("玉林", "9", {"error": "没这条线路"})
assert query.render("沛县", "1", {"error": "是 11路 / 17路 吗"}) == "没「1」这条，是 11路 / 17路 吗"
assert query.render("玉林", "1", {"error": "上游这会儿没响应，过会儿再问"}) == "上游这会儿没响应，过会儿再问"

# 站名最多退到 3 字：退到 2 字会让「南宁机场」搜出「南宁孔庙」这种风马牛不相及的站
assert query.find_stop("南宁市", "南宁机场")[0] is None
assert query.norm_line("南宁机场") == "南宁机场"

# @ 标记里的 QQ 号看着像「E8 路」，不能当线路号
assert query.AT_RE.sub(" ", "<@E8CBB6F8BD5822CA314B63B94976> 玉林 1 路").split() == ["玉林", "1", "路"]
assert query.parse(query.AT_RE.sub(" ", "[At:E279593D8E44520BC3824B9CC89E0859] 1 路有车吗")) == ("1", "")
assert query.hop("从广州公园前到玉林北站的话，坐什么车") == ("广州公园前", "玉林北站")

# 目录缓存一种形状：_mygolbs 存 dict、render_at 用 buslines，之前谁先跑谁写坏对方
_rows = [{"lineName": "1路", "upperOrDown": "1", "from": "A", "to": "B"}]
_cat, query._post = query._post, lambda *a, **k: {"buslines": _rows, "status": 1}
query._cache.clear()
query._mygolbs("x市", "1")                     # 先让 dict 进缓存
assert query._catalog("x市").get("buslines") == _rows
assert query._catalog("x市")["buslines"][0]["lineName"] == "1路"
query._post = _cat
query._cache.clear()

# Origin 头是掌上公交的唯一命门：带上多余的斜杠或换 http，上游回 200 + 58 字节占位串
assert query.HEADERS["Origin"] == "https://h5.mygolbs.com"
assert not query.HEADERS["Origin"].endswith("/")

# 城市识别：给假城市表，不联网
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "南宁": {"mygolbs": "南宁市"}}
assert query.detect_city("玉林 G02 路有没有车", "", "南宁市") == ("玉林", True)
assert query.detect_city("2 路有车吗", "", "南宁市") == ("南宁", False)   # 没写城市用默认
assert query.detect_city("火星 1 路有车吗", "火星", "南宁市") == (None, False)  # 认不出就别瞎答

# 引用别人的消息追问（「那 39 路往哪开」）：城市在被引用的那句里，只拿来认城市，不拿来找站名/线路号
assert query.detect_city("那39路的那一辆是去往哪个方向呢？ 南宁市这会儿有 7 条线在跑",
                         "那", "玉林市") == ("南宁", True)
_ps, query.pick_stop = query.pick_stop, lambda *a: None
_hq, query.query = query.query, lambda ck, wants, src=None, veh="": (
    {"line": "39路", "groups": [("A", [("桂A1", "x", 0)])]} if (ck, wants) == ("南宁", ["39"]) else None)
assert query.answer("那39路的那一辆是去往哪个方向呢？", "玉林市",
                    "南宁市这会儿有 7 条线在跑") == "有，39路 现在 1 辆在路上\n往A：桂A1 x"
query.pick_stop, query.query = _ps, _hq

# 「上一条问的线路」必须按会话记：A 群问 G02，B 群问 9 路，A 群的追问不能串成 9 路
_sq2, _cat2 = query._station_seq, query._catalog
query._catalog = lambda city: {"buslines": [{"lineName": n, "upperOrDown": "1", "to": "x"}
                                            for n in ("G02路", "9路")]}
query._station_seq = lambda city, line, d: [{"stationName": f"{line}站1"}, {"stationName": f"{line}站2"}]
query.stops = lambda city, kw: [{"relatedRoutes": ["G02路", "1路"] if kw.startswith("G02") else ["9路", "2路"]}]
assert query.answer("G02路的站点列表", "玉林市", key="群A").startswith("G02路")
assert query.answer("9路的所有站点列表", "玉林市", key="群B").startswith("9路")
assert query.answer("每一站能换乘啥线路", "玉林市", key="群A").startswith("G02路")
assert query.answer("每一站能换乘啥线路", "玉林市", key="群B").startswith("9路")
query._station_seq, query._catalog = _sq2, _cat2

# 运营公司/咨询电话来自 114.routeBean：company 几乎全城都有，companyMobile 只有部分城市有
_p114, query._post = query._post, lambda p, timeout=8: {"buslines": [
    {"routeBean": {"routeName": "9路", "company": "玉林公交", "companyMobile": ""}},
    {"routeBean": {"routeName": "1路", "company": "一汽巴士", "companyMobile": "一汽巴士:020-87763773"}}]}
assert query.render_company("玉林市", "9") == "9路 归玉林公交\n平台没挂咨询电话，丢东西直接打 12345 或去线路终点站的调度室问"
assert query.render_company("玉林市", "1") == "1路 归一汽巴士，咨询电话 020-87763773"
query._post = _p114

# 地铁也在同一套目录里（掌上「地铁10号线」「轨道交通1号线」，车来了没有）；
# 归一化要把「轨道交通」和「地铁」当成同一条，否则南宁查不到
assert query.parse("南宁 轨道交通1号线有哪些站") == ("地铁1号", "南宁")
assert query.parse("广州地铁1号线站点列表") == ("地铁1号", "广州")
assert query.parse("地铁广佛线有哪些站")[0] == "地铁广佛"
assert query.norm_line("轨道交通1号线") == query.norm_line("地铁1号线")
assert query.parse("8点出门") is None                  # 「8点」不能被当线路

# 逐站换乘：上下行同一个站只算一次，列表里要剔掉本线
_sq, _st, _cat = query._station_seq, query.stops, query._catalog

def _fake_seq(city, line, d):
    return [{"stationName": n} for n in (["甲", "乙"] if d == "1" else ["乙", "丙"])]

query._station_seq = _fake_seq
query.stops = lambda city, kw: [{"relatedRoutes": ["9路", "1路"]}]
query._catalog = lambda city: {"buslines": [{"lineName": "9路", "upperOrDown": "1", "to": "丙"},
                                            {"lineName": "9路", "upperOrDown": "2", "to": "甲"}]}
t = query.render_transfer("玉林市", "9")      # want 传归一化后的线路号，跟 answer 一致
assert "只有这条线" not in t and t.count("1路") == 3, t
assert t.count("站能换乘") == 1, t              # 上下行两条目录记录只念一遍
assert [l.split(". ")[1].split("（")[0] for l in t.split("\n")[1:]] == ["甲", "乙", "丙"]
query._station_seq, query.stops, query._catalog = _sq, _st, _cat

# 站点与换乘：「从」可有可无（群里就有人写「玉林北站到旅游客运站的路线」）
assert query.hop("从十字街到火车站坐什么车") == ("十字街", "火车站")
assert query.hop("从金城商厦到玉林北站有直达吗") == ("金城商厦", "玉林北站")
assert query.hop("十字街到火车站") == ("十字街", "火车站")
assert query.hop("玉林北站到玉林旅游客运站的路线") == ("玉林北站", "玉林旅游客运站")
assert query.hop("坐1路到火车站") is None           # 左边是线路号，不是站名
# 「看到/说到/想到」是动词的「到」，不是「A 站到 B 站」——群里聊「看到司机和公交公司救援」
# 曾被截成「那会就看」→「司机和公交公司救援」，还回了一句「是哪座城的」
assert query.hop("那会就看到司机和公交公司救援") is None
assert query.hop("我刚才看到 3 路车过去了") is None
assert query.hop("听说 9 路改线了") is None
assert query.hop("走到公交站就看到车了") is None
assert query.hop("玉林火车站到北流汽车总站坐哪路") == ("玉林火车站", "北流汽车总站")
assert query.hop("机场一号航站楼到元氏火车站怎么走") == ("机场一号航站楼", "元氏火车站")
# 到站分钟用上游自己的预测（routeOnStationRTimeInfoList），每站最多 2 辆；
# 站已过时上游回 busToStationCount=-1、busNumber 空，不能拿来当「即将到站」
p = {"routeOnStationRTimeInfoList": [
    {"busNumber": "桂K1", "busToStationTime": 13.0, "busToStationCount": 12},
    {"busNumber": "桂K2", "busToStationTime": 25.0, "busToStationCount": 18}]}
assert query._etas(p) == (13.0, 12, "桂K1")          # 取最快那辆
assert query._etas({"routeOnStationRTimeInfoList": [
    {"busNumber": "", "busToStationTime": 0.0, "busToStationCount": -1,
     "busToStationTips": "已过末班时刻"}]}) is None
assert query._etas({}) is None

# 区间时长上游不给，按每段里程 ÷ 均速（m/min）累
q = {"dislist": [{"d": 100}, {"d": 200}, {"d": 300}, {"d": 0}],
     "speedlist": [{"speed": 100}, {"speed": 100}, {"speed": 100}]}
assert query._run(q, 0, 1) == 1
assert query._run(q, 0, 2) == 3
assert query._run(q, 1, 3) == 5
assert query._run(q, 0, 99) == 6                      # 越界按表长截断

# 车辆距离：只有「已到站」和「在途(statusType=2)」两个状态可信
assert query._car("桂K1", "十字街", 0, "0") == "桂K1 已到十字街"
assert query._car("桂K1", "十字街", 245, "2") == "桂K1 十字街 245m"
assert query._car("桂K1", "十字街", 245, "9") == "桂K1 十字街"   # 别的状态别念距离

# 上游业务失败（HTTP 200 + status "-2"）不能当成「没这条线路」
_post, query._post = query._post, lambda *a, **k: {"status": "-2", "msg": "当前城市暂无法提供公交查询", "error": True}
query._cache.clear()
assert query._mygolbs("火星市", "1")== {"error": "当前城市暂无法提供公交查询"}
query._post = _post

# 全城扫：认不出站名时才走 sweep，「十字街有几路车」不能被它抢走
assert query.SWEEP_RE.search("还有哪条线路有公交车")
_sweep, query.render_sweep = query.render_sweep, lambda city, plates=False, only=None: f"SWEEP:{city}"
_stop, query.render_stop = query.render_stop, lambda *a: None
assert query.answer("玉林 还有哪条线路有公交车", "玉林市") == "SWEEP:玉林市"
assert query.answer("玉林 今天天气怎么样", "玉林市") == ""      # 不像公交问题就交给 LLM
query.render_sweep, query.render_stop = _sweep, _stop

# 「每辆车的车牌是啥」引用「还有 5 条在跑」追问：不能拿引用里那一路顶替，要扫全城带车牌
assert query.BUS_RE.search("那么每辆车的车牌是啥")
assert query.looks_like_ask("那么每辆车的车牌是啥")
_cat, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "9路", "upperOrDown": "1", "from": "A", "to": "B"}]}
_lm, query._live_my = query._live_my, lambda cn, n, d, order=1: {
    "hasReal": "1", "list": [{"busNumber": "桂K1", "stationName": "S1"},
                             {"busNumber": "桂K2", "stationName": "S2"}]}
assert "桂K1" in query.render_sweep("玉林市", True)
assert "桂K1" not in query.render_sweep("玉林市")
query._catalog, query._live_my = _cat, _lm
_sw2, query.render_sweep = query.render_sweep, lambda city, plates=False, only=None: f"SWEEP:{city}:{plates}"
assert query.answer("那么每辆车的车牌是啥", "玉林市",
                    "有，这会儿还在跑 5 条：6路夜班 3 辆、26路 3 辆") == "SWEEP:玉林市:True"
query.render_sweep = _sw2

# hasReal=0 是「上游没给车辆位置」，不是「没车」，不能混成一句谎话
assert query.render("x", "1", {"line": "1路", "groups": [("A", []), ("B", [])],
                               "note": "上游对这个城市不提供实时车辆位置",
                               "unknown": True}).startswith("1路 查不到实时车")
assert query.render("x", "1", {"line": "1路", "groups": [("A", []), ("B", [])],
                               "note": "末班 20:25 已经开走了"}).startswith("1路 这会儿没车")
# hasReal=0 时只回「不提供」等于没答，把能查到的首末班和站数带上
_fl, _seq, _cat = query._first_last, query._station_seq, query._catalog
_lm = query._live_my
query._live_my = lambda c, n, d, order=1: {"status": "1", "hasReal": "0", "list": []}
query._first_last = lambda c, l, d: ("05:40", "21:00")
query._station_seq = lambda c, l, d: [{"stationName": f"s{i}"} for i in range(27)]
query._catalog = lambda c: {"buslines": [{"lineName": "浦东51路", "upperOrDown": "1",
                                          "from": "人民东路学海路", "to": "迪士尼地铁站"}]}
_got = query._mygolbs("上海市", "浦东51")
assert _got["unknown"] and "05:40 起、末班最晚 21:00" in _got["note"] and "27 站" in _got["note"], _got
query._live_my, query._first_last, query._station_seq, query._catalog = _lm, _fl, _seq, _cat

# issue #8：前缀是线路名的一部分，明写城市也不丢；前缀自己就是城市时以它为准
_cap, _q = [], query.query
query.query = lambda ck, wants, src=None, veh="": (_cap.append((ck, wants)), {"error": "没这条线路"})[1]
_cit = query.cities
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "容县": {"mygolbs": "容县市"}}
_rc, query.render_company = query.render_company, lambda *a: None
_rs, query.render_stops = query.render_stops, lambda *a: None
_ra, query.render_at = query.render_at, lambda *a: None
_cg, query._catalog = query._catalog, \
    lambda cn: {"buslines": [{"lineName": "陆川1路"}] if cn == "玉林市" else []}
query.answer("玉林 容县 11 路有车吗", "玉林市")
assert _cap[-1] == ("容县", ["11"]), _cap[-1]          # 别拿玉林的目录查裸号 11
query.answer("玉林 陆川 1 路有车吗", "玉林市")
assert _cap[-1] == ("玉林", ["陆川1"]), _cap[-1]       # 目录里有陆川1路
query.query, query.cities, query._catalog = _q, _cit, _cg
query.render_company, query.render_stops, query.render_at = _rc, _rs, _ra

# 没写城市、默认城市又查不到：要说明「按谁查的」，别让用户以为这条线不存在
_q, query.query = query.query, lambda ck, wants, src=None, veh="": {"error": "没这条线路"}
_c, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}}
_a, query.render_at = query.render_at, lambda *a: None          # 别真去打网络
_txt = query.answer("999 路有车吗", "玉林市")
assert _txt.startswith("没查到「999」这条线") and "我按玉林查的" in _txt, _txt
_ex = query.answer("南宁 999 路有车吗", "玉林市")   # 明写了城市就不念这句
assert "我按" not in _ex, _ex
query.query, query.cities, query.render_at = _q, _c, _a

# 掌上这座城没实时（hasReal=0，北京/成都）→ 下一问先问车来了，别再空等一轮
_rq = query.query
_cit, query.cities = query.cities, lambda: {"测试": {"mygolbs": "X市", "chelaile": "999"}}
_mg, query._mygolbs = query._mygolbs, lambda c, w: {"source": "掌上公交", "line": "1路",
                                                   "groups": [("", [])], "note": "", "unknown": True}
_cl, query._chelaile = query._chelaile, lambda c, w: {"source": "车来了", "line": "1",
                                                      "groups": [("", [("A", "B", 1)])], "desc": ""}
query.PREFER_CL.pop("测试", None)
_rq("测试", ["1"])
assert query.PREFER_CL.get("测试"), "掌上 unknown 该记下来"
_seq = []
query._mygolbs = lambda c, w: (_seq.append(1), {"source": "掌上公交", "line": "1路",
                                                "groups": [("", [])], "note": "", "unknown": True})[1]
_rq("测试", ["1"])
assert not _seq, "第二问该直接先问车来了"
query.cities, query._mygolbs, query._chelaile = _cit, _mg, _cl
query.PREFER_CL.pop("测试", None)

# 「从车来了的那个看一下数据」：没写线路号就续上一条问的车，且点名车来了要排前面
_cap2 = []
_q2, query.query = query.query, lambda ck, w, src=None, veh="": (
    _cap2.append((ck, list(w), src)), {"error": "没这条线路"})[1]
_ct2, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
_ra2, query.render_at = query.render_at, lambda *a: None
query.answer("9 路有车吗", "玉林市", key="t9")
query.answer("你从车来了的这个看一下数据怎么样？", "玉林市", key="t9")
assert _cap2[-1] == ("玉林", ["9"], "chelaile"), _cap2[-1]
# 引用别人的消息问「你用车来了查一下」：线路号在被引用那句里，也要走车来了
query.answer("你用车来了查一下", "玉林市", "有，G01路 现在 4 辆在路上", "t9")
assert _cap2[-1] == ("玉林", ["G01"], "chelaile"), _cap2[-1]
query.query, query.cities, query.render_at = _q2, _ct2, _ra2

# 口语报线路号：「八路」「十路」「二十三路」
assert query.parse("巩义市八路公交车现在有车吗") == ("8", "巩义市")
assert query.parse("玉林 十路有车吗") == ("10", "玉林")
assert query.parse("南宁 二十三路") == ("23", "南宁")
assert query.parse("8点出门") is None
# 「wb2api」「tx收紧」这种英文缩写里夹的数字，别当成线路号
assert query.parse("用的wb客户端多用户切换和wb2api都好几天没更新了，tx是不是收紧了？") is None
# 「131区间」不带「路」也是线路号；站名不许退到城市名本身（「石家庄」→ 石家庄站）
assert query.parse("石家庄131区间有没有车") == ("131区间", "石家庄")
assert query.parse("石家庄131路区间车现在有没有在跑") == ("131区间", "石家庄")
assert query.parse("石家庄131路有没有车") == ("131", "石家庄")

# 「广州地铁芳村站附近有多少条公交线」：城市名和「地铁」得剥掉，否则站名永远搜不到
_st = query.stops
query.stops = lambda cn, kw: [{"stationName": "芳村", "relatedRoutes": ["19路"]}] if kw == "芳村站" else []
assert query.pick_stop("广州市", "广州地铁芳村站附近有多少条公交线") == "芳村"
# 「泉州丰州公交站现在有什么车」：站牌上就叫「丰州」，没有「公交站」这个后缀，而且只有 2 字
query.stops = lambda cn, kw: (
    [{"stationName": "丰州", "relatedRoutes": ["6路"]}] if kw == "丰州"
    else [{"stationName": "辋川公交站", "relatedRoutes": ["惠安13路"]}] if kw == "公交站" else [])
assert query.pick_stop("泉州市", "泉州丰州公交站现在有什么车") == "丰州"
# 2 字兜底要跟截出来的那两字比，不能拿整串比——否则「丰州公交站」永远匹不到「丰州」
assert query.find_stop("泉州市", "丰州公交站")[1] == "丰州"
query.stops = _st
# 「泉州公交 惠南工业区四期站」切出来的「泉州公交」只剩城市名，别拿去搜站（会匹到「中化泉州公司」）
assert list(query._cands("泉州市", "泉州公交 惠南工业区四期站现在有什么车")) == ["惠南工业区四期站"]
# 「陆川汽车总站」平台写作「汽车总站（陆川）」：县名挪进括号，前缀认得出是哪一条
_st2, query.stops = query.stops, lambda cn, kw: [
    {"stationName": "汽车总站", "relatedRoutes": ["9路"]},
    {"stationName": "汽车总站（陆川）", "relatedRoutes": ["陆川1路"]},
] if kw == "汽车总站" else []
assert query.pick_stop("玉林市", "陆川汽车总站有哪几条公交线路") == "汽车总站（陆川）"
assert query.pick_stop("玉林市", "汽车总站有哪几路车") == "汽车总站"   # 没写县名还是市区那个
query.stops = _st2
# 「汽车总站（陆川）」也长成「汽车总站（…）」，可陆川汽车总站是另一个县的站，差 36.8 公里
_rows = [
    {"stationName": "汽车总站", "lat": "22.6269", "lng": "110.1439", "relatedRoutes": ["9路"]},
    {"stationName": "汽车总站（二医院）", "lat": "22.6279", "lng": "110.1456", "relatedRoutes": ["G02路"]},
    {"stationName": "汽车总站（陆川）", "lat": "22.3135", "lng": "110.2595", "relatedRoutes": ["陆川1路"]},
]
assert query._merge_same(_rows, _rows[0])["relatedRoutes"] == ["9路", "G02路"]
# 问一片站台时要分组说清哪路车在哪个站台等，同一路车只挂一个站台
_cat3, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "9路", "from": "甲", "to": "乙"}, {"lineName": "G02路", "from": "丙", "to": "丁"}]}
_st3, query.stops = query.stops, lambda cn, kw: _rows if kw == "汽车总站" else []
_out = query.render_stop("玉林", "玉林市", "汽车总站有哪几路车")
assert "汽车总站（二医院）（1 条）" in _out and "汽车总站（1 条）" in _out \
    and "陆川1路" not in _out, _out
query._catalog, query.stops = _cat3, _st3
# 平台没给坐标时退回按名字匹，不能因为缺字段就把线路全丢了
assert query._merge_same([_rows[0], dict(_rows[1], lat=None)], _rows[0])["relatedRoutes"] == ["9路", "G02路"]
# 站表不许截断：23 条就报 23 条，否则用户再问「剩下的呢」认不出站名
_cat, _stops = query._catalog, query.stops
query._catalog = lambda cn: {"buslines": [{"lineName": f"{i}路", "from": "甲", "to": "乙"} for i in range(23)]}
query.stops = lambda cn, kw: [{"stationName": "谈固站", "relatedRoutes": [f"{i}路" for i in range(23)]}]
_out = query.render_stop("石家庄市", "石家庄市", "谈固站有哪几路公交车")
assert _out.count("\n") == 23 and "还有" not in _out, _out
query._catalog, query.stops = _cat, _stops
# 「G02-513 现在在哪」问的是一辆车，不是整条线
assert [g for g in query.VEH_RE.search("G02-513现在在哪里了").groups() if g] == ["513"]
assert [g for g in query.VEH_RE.search("G02 513号车在哪").groups() if g] == ["513"]
assert query.VEH_RE.search("玉林 9路有车吗") is None
_res = {"line": "G02", "groups": [("甲", [("513", "A站", 100)]), ("乙", [("517", "B站", 200)])]}
assert "513" in query.render("玉林", "G02", _res, "513") and "517" not in query.render("玉林", "G02", _res, "513")
assert "没跑 999" in query.render("玉林", "G02", _res, "999")
# 两家都翻过：没这辆车时要把两家在跑的一起报，别让人以为只查了一家
_o = query.render("玉林", "G02", dict(_res, veh_others={"掌上公交": ["桂K1"], "车来了": ["517"]}), "999")
assert "掌上公交 桂K1" in _o and "车来了 517" in _o, _o
# 「用掌上公交查G02路」：数据源那层壳不能当城市名，否则前缀取成「上公交」
assert query.parse(query.SRCPRE.sub("", "用掌上公交查G02路")) == ("G02", "")
assert query.parse(query.SRCPRE.sub("", "车来了查石家庄41路")) == ("41", "石家庄")
# 「车来了，看一下 G02…」：数据源后面跟逗号再跟动作词，一样得剥干净
assert query.parse(query.SRCPRE.sub("", "车来了，看一下 G02 现在上线的有几台车")) == ("G02", "")
assert query.parse(query.SRCPRE.sub("", "车来了查询玉林职业技术学院有没有公交车")) is None
# 带线路号的闲聊（「11路那车真挤」）不该被快路抢答，纯报号照答
assert not query.looks_like_ask("今天坐9路回家累死了")
assert not query.looks_like_ask("11路那车真挤")
assert not query.looks_like_ask("你觉得G02这名字好听吗")
assert query.looks_like_ask("11路")
assert query.looks_like_ask("玉林9路")
assert query.looks_like_ask("9路有车吗")
assert query.looks_like_ask("我在市政广场能不能等到G02")
# 一句问两条线（「樟13 / 12路」「11路和12路」）要都答，别只答第一条
assert [n for n, _ in query._lines("福州永泰闽运\n樟13\n12路目前运行情况")] == ["12", "樟13"]
assert [n for n, _ in query._lines("11路和12路")] == ["11", "12"]
assert [p for _, p in query._lines("11路和12路")] == ["", ""]      # 「路和」不是县名
# 「11路还有多久到双溪口」：TAIL 的「还有」会把目的地一起吃到行尾，得先按「到」切
assert "双溪口" in list(query._cands("泉州市", "泉州公交11路还有多久到双溪口"))
# 上游刚挂过也不能让闲聊收到「上游没响应」——那句是「彻底变成问公交工具」的由来
_uf = query._up_fail[0]
query._up_fail[0] = query.time.time()
assert query.answer("这大肥鱼彻底变成问公交工具了", "玉林市") == ""
assert query.answer("晚上吃点啥呢", "玉林市") == ""
query._up_fail[0] = _uf
# 「车来了查X校区的线路」：数据源和动词不能留在站名里；点名车来了就扫车来了的站表
assert list(query._cands("玉林市", "车来了查广西医科大玉林校区的线路")) == ["广西医科大玉林校区"]
# 「玉林北站有哪几路车」：城市名剥掉只剩「北站」搜不到，得再拿原样那句试一次
_st2 = query.stops
query.stops = lambda cn, kw: [{"stationName": "玉林北站", "relatedRoutes": ["G01路"]}] if kw == "玉林北站" else []
assert query.pick_stop("玉林市", "玉林北站有哪几路车") == "玉林北站"
query.stops = _st2
# 上一条问过 9 路，再来一句带站点的「车来了查XX校区」：别拿上一条的线路号答
_ql, _cit2, _rst = query.query, query.cities, query.render_stop
query.cities = lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
query.query = lambda ck, wants, src=None, veh="": (_ for _ in ()).throw(AssertionError("不该走线路"))
query.render_stop = lambda ck, cn, m, src=None: "医科大学玉林校区有 3 条线路：G01"
query._last("k9")["line"] = "9"
assert query.answer("车来了查广西医科大玉林校区的线路", "玉林市", key="k9").startswith("医科大学玉林校区")
query.query, query.cities, query.render_stop = _ql, _cit2, _rst
_cl, _ct3 = query.chelaile, query.cities


class _CL:
    lines = staticmethod(lambda cid: [("99路", "a"), ("G01路", "b"), ("G02路", "c")])
    stations = staticmethod(lambda cid, lid: {"a": ["玉林火车站"], "b": ["医科大学玉林校区"],
                                             "c": ["医科大学玉林校区"]}[lid])


query.cities = lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
query.chelaile = _CL
query._cache.clear()        # 车来了目录 1 小时缓存，不然读到上面几次真接口的结果
_out = query.render_stop("玉林", "玉林市", "车来了查广西医科大玉林校区的线路", "chelaile")
assert "G01路" in _out and "G02路" in _out and "99路" not in _out, _out
query.chelaile, query.cities = _cl, _ct3
query.stops = _st
assert query.norm_line("131路区间") == query.norm_line("131区间")
assert [query._cn_num(x) for x in ("一", "九", "十", "十二", "二十三", "两")] == [1, 9, 10, 12, 23, 2]

# 「没有数据」的说法不能撞上 main.py 的 FALSE_CLAIM，否则整句被吞、用户收不到
for _bad in ("巩义市这座城暂时没有公交数据，掌上和车来了都没覆盖",
             "这条线路查不了"):
    _ok = not re.search(r"数据源|只接了|只有玉林|玉林的接口|"
                        r"查不了.{0,8}(公交|线路)|(公交|线路).{0,8}查不了", _bad)
    assert _ok == (not _bad.startswith("这条")), _bad

# 图片消息的 base64 里有「yJ0」，别当成线路号（答成「没查到了YJ0这条线」）
_IMG = '<attachmentType="image/jpeg",attachmentIndex=0,description="eyJ0ZXh0IjoiW+WbvueJh10ifQ==">'
assert query.answer(_IMG, "玉林市") == "", query.answer(_IMG, "玉林市")
assert query.answer("[图片]", "玉林市") == ""

# 「用车来了看看有G02」里的「了看看有」不是县名——别把动词当城市前缀，答成「是哪儿的公交」
assert query.parse("用车来了看看有G02都有哪几台车") == ("G02", "")
assert query.parse("玉林 用车来了看看G02") == ("G02", "")
assert query.parse("玉林 陆川 1 路") == ("1", "陆川")      # 真县名前缀还得留着

# 问首末班时刻 → 回答时间，别拿「末班已开走」糊过去
_cat = query._catalog
query._catalog = lambda c: {"buslines": [
    {"lineName": "G02路", "upperOrDown": "1", "from": "甲站", "to": "乙站"},
    {"lineName": "G02路", "upperOrDown": "2", "from": "乙站", "to": "甲站"}]}
_fl = query._first_last
query._first_last = lambda c, n, d: ("06:40", "18:40") if d == "1" else ("07:45", "19:50")
_pl = query._price
query._price = lambda c, n: "2元/人次"
_t = query.render_times("玉林市", "G02")
assert _t.count("首班") == 2 and "07:45" in _t and "乙站 → 甲站" in _t, _t
assert "票价 2元/人次" in _t, _t            # 问首末班顺带把票价报上
assert query.render_times("玉林市", "999") is None
# 问票价：单独问也答，且「往X方向」只念这个方向
assert query.render_price("玉林市", "G02") == "G02路 票价 2元/人次"
assert query.render_price("玉林市", "999") is None
query._price = lambda c, n: ""
assert "没标票价" in query.render_price("玉林市", "G02")
query._catalog, query._first_last, query._price = _cat, _fl, _pl

# 没带线路号问公司/电话：「玉林公交公司电话是多少」以前返空，模型拿到空就自己编
_cc = query._post
query._post = lambda p, timeout=8: {"buslines": [
    {"routeBean": {"routeName": "1路", "company": "甲公交", "companyMobile": ""}},
    {"routeBean": {"routeName": "2路", "company": "乙巴士", "companyMobile": "乙巴士:020-12345678"}}]}
_x = query.render_city_company("某市")
assert "2 家公交公司" in _x and "乙巴士 020-12345678" in _x and "甲公交" in _x, _x
_y = query.render_city_company("某市", "某市乙巴士电话")     # 点了名只报这家
assert "1 家" in _y and _y.count("020-12345678") == 1, _y
assert query.render_city_company(None) is None
assert not query.CITY_CO_RE.search("我们公司今年业绩咋样")      # 闲聊不能被公司查询抢走
assert query.CITY_CO_RE.search("玉林公交公司电话")
query._post = _cc

# 「首末站」问的是起讫站，不是首末班时间；下行是反过来的同一条，别念两遍
_ce = query._catalog
query._catalog = lambda c: {"buslines": [
    {"lineName": "6路", "upperOrDown": "1", "from": "人民大北路口", "to": "成均镇卫生院"},
    {"lineName": "6路", "upperOrDown": "2", "from": "成均镇卫生院", "to": "人民大北路口"}]}
assert query.ENDS_RE.search("玉林6路的首末站是啥")
assert query.ENDS_RE.search("玉林6路的起点和终点") and query.ENDS_RE.search("玉林6路起终点")
assert not query.ENDS_RE.search("玉林6路首末班")
assert query.render_ends("玉林市", "6") == "6路 首末站：人民大北路口 ↔ 成均镇卫生院"
query._catalog = _ce

# 群里 @机器人 的三种写法都要先抹掉，否则「@大肥鱼 绵阳25」整句进不了 parse
assert query.AT_RE.sub("", "@大肥鱼 绵阳25").strip() == "绵阳25"
assert query.AT_RE.sub("", "[At:E2795AB] 绵阳25").strip() == "绵阳25"
assert query.AT_RE.sub("", "<@E8CBB1> 绵阳25").strip() == "绵阳25"
assert query.AT_RE.sub("", "玉林9路有车吗") == "玉林9路有车吗"   # 正文里的空格不动

# 「牡丹江21」：城市名＋线号不带「路」，整句只有城市名和数字才认；多一个字就是句子
_cit = query.cities
query.cities = lambda: {"牡丹江": {"chelaile": "131", "mygolbs": "牡丹江市"}, "玉林": {}}
assert query.parse("牡丹江21") == ("21", "牡丹江")
assert query.parse("牡丹江21首末站") == ("21", "牡丹江")   # 号后面跟一句问法也能认
assert query.parse("[At:123] 牡丹江21？") == ("21", "牡丹江")
assert query.parse("玉林2023年GDP") is None        # 年份不能被当成 2023 路
assert query.parse("今天好热") is None
query.cities = _cit

# 一条线号十几个支线（绵阳 25 路）：同终点合并，方向过多时只念有车的，抬头不带括号
_g = [("观太", []), ("富乐汽车站", [("3049", "观太街口", "360")]),
      ("广兴村", []), ("信义镇", []), ("段家桥", []), ("桅杆垭", [("3034", "云华山", "1400")])]
_z = query.render("绵阳", "25", {"line": "25路(五大队观太)", "groups": _g})
assert "25路 现在 2 辆在路上" in _z, _z
assert "还没车" not in _z, _z                    # 13 个支线不能整屏「往X还没车」
assert "(五大队观太)" not in _z, _z

# 「西安阿房宫线」问的是地铁5号线/西户线：公交站旁边那条「（地铁站）」记录得单独报一行
_st, _ct = query.stops, query._catalog
query.stops = lambda c, k: [
    {"stationName": "阿房宫南地铁站", "lat": "34.250833", "lng": "108.82542",
     "relatedRoutes": ["1123路"]},
    {"stationName": "阿房宫南（地铁站）", "lat": "34.250590", "lng": "108.824334",
     "relatedRoutes": ["5号线", "西户线"]}]
query._catalog = lambda c: {"buslines": [
    {"lineName": "1123路", "upperOrDown": "1", "from": "甲", "to": "乙"}]}
_z = query.render_stop("西安", "西安市", "阿房宫线")
assert "是地铁站，经停 5号线、西户线" in _z, _z
assert _z.count("阿房宫南（地铁站）") == 1, _z        # 平台会把同一站存两条，别念两遍
query.stops, query._catalog = _st, _ct

# 实例是 UTC：时钟要按北京时间报（23:37 那种是 UTC，实际 07:37）
import time as _tm
_utc = _tm.time
query.time.time = lambda: 1745000000.0          # 某个 UTC 时刻
assert len(query._clock(0)) == 5 and query._clock(0) == \
    _tm.strftime("%H:%M", _tm.gmtime(1745000000.0 + 8 * 3600)), query._clock(0)
assert query._clock(1500) == _tm.strftime("%H:%M", _tm.gmtime(1745000000.0 + 8 * 3600 + 1500 * 60))
query.time.time = _utc

# 夜班车早上问：末班没过 ≠ 在路上。玉林 6路夜班 10:16 被答成「线路还在跑，只是车机没报位置」
_nw = query._now
_t = _tm.struct_time
for _h, _m, _want in ((10, 18, "before"), (20, 0, "open"), (23, 50, "after")):
    query._now = lambda h=_h, m=_m: _t((2026, 11, 5, h, m, 0, 3, 309, 0))
    assert query._window("17:47", "22:40") == _want, (_h, _m, query._window("17:47", "22:40"))
query._now = _nw
# 一个线号两个方向各有首末班，都要报出来
query._now = lambda: _t((2026, 11, 5, 10, 18, 0, 3, 309, 0))
_wn = query._window_note([("马尔代夫水上乐园", "17:47", "22:40"), ("人民大北路口", "18:47", "23:10")])
assert "首班还没到" in _wn and "17:47–22:40" in _wn and "18:47–23:10" in _wn, _wn
assert query._window_note([("甲", "06:30", "18:20")]) == ""      # 窗口内不抢「车机没报位置」
query._now = _nw

# 「G02到高新产业园，去玉林北站」：上车站是产业园，玉林北站只是方向，不是上车站
_fs = query.find_stop
query.find_stop = lambda c, raw: ({"stationName": raw} if raw in ("高新产业园", "玉林北站") else None, raw)
assert query.pick_stop("玉林市", "你看一下G02到高新产业园，去玉林北站要等多久？") == "高新产业园"
assert query._dest_after("玉林市", "你看一下G02到高新产业园，去玉林北站要等多久？",
                         "高新产业园") == "玉林北站"
assert query._dest_after("玉林市", "我在玉林北站，G02现在到那里我要多少分钟", "玉林北站") is None
query.find_stop = _fs

# 平台把「汽车总站」拆成两条，前一条的线路表是残的——G02 只挂在「汽车总站（二医院）」名下
_rows = [{"stationName": "汽车总站", "relatedRoutes": ["玉林总站-福绵新滔工业园"]},
         {"stationName": "汽车总站（二医院）", "relatedRoutes": ["G02路", "9路"]},
         {"stationName": "北流汽车总站", "relatedRoutes": ["北流105路"]}]
_m = query._merge_same(_rows, _rows[0])
assert "G02路" in _m["relatedRoutes"] and "北流105路" not in _m["relatedRoutes"], _m
assert _m["stationName"] == "汽车总站", _m          # 合并只并线路，不改站名
# 隔壁站和地铁站不是同一个站，不能并进公交清单（芳村一度从 18 条涨到 63 条）
_rows = [{"stationName": "芳村", "relatedRoutes": ["1路"]},
         {"stationName": "芳村（地铁站）", "relatedRoutes": ["地铁1号线"]},
         {"stationName": "芳村花园南门总站", "relatedRoutes": ["106路"]}]
assert query._merge_same(_rows, _rows[0])["relatedRoutes"] == ["1路"], \
    query._merge_same(_rows, _rows[0])

# issue #10：末班比较不跨零点（字符串比较时 '00:02' < '22:40'）
import types as _t
_lt, _real = query.time, sys.modules["time"]
def _at(hh, mm):
    s = _real.localtime()
    query.time = _t.SimpleNamespace(localtime=lambda *a: s.__replace__(tm_hour=hh, tm_min=mm),
                                    gmtime=lambda *a: s.__replace__(tm_hour=hh, tm_min=mm),
                                    strftime=_real.strftime, time=_real.time)
for _now, _last, _want in [((0, 2), "22:40", True), ((0, 10), "00:30", False),
                           ((23, 50), "24:10", False), ((0, 20), "24:10", True),
                           ((23, 0), "00:10", False), ((12, 0), "12:30", False)]:
    _at(*_now)
    assert query._past_last(_last) is _want, (_now, _last, query._past_last(_last))
# 有首班就按运营窗口判：末班 23:44 的线，早上 9 点不能报「末班已开走」
for _now, _first, _last, _want in [((9, 0), "05:30", "23:44", False),
                                   ((23, 50), "05:30", "23:44", True),
                                   ((0, 30), "05:30", "23:44", True),
                                   ((20, 0), "17:47", "23:10", False),
                                   # 13:00 问 17:47 才开班的车：离首班更近，是没开不是收班
                                   ((13, 0), "17:47", "23:10", False),
                                   ((18, 50), "17:47", "23:10", False),
                                   ((0, 30), "22:00", "01:00", False)]:
    _at(*_now)
    assert query._past_last(_last, _first) is _want, (_now, _first, _last, query._past_last(_last, _first))
# 距首班还剩多久改成用 _window 判，_mins_to 已删
_at(13, 0)
assert query._window("17:47", "22:40") == "before"      # 13:00 还没到 17:47 的首班
assert query._window("06:30", "18:20") == "open"
query.time = _lt

# issue #11：香港「1」对应五家公司，按名字分组各查各的，别取前两条
_cl, _mod = query.chelaile.lines, query.chelaile.live
query.chelaile.lines = lambda cid: [("1（九巴）", "a1"), ("1（九巴）", "a2"),
                                    ("1（城巴）", "b1"), ("1（城巴）", "b2")]
query.chelaile.live = lambda cid, lid: {"to": f"往{lid}", "desc": "", "state": 0,
                                        "buses": [("车" + lid, "站", 100)]}
query._cache.clear()
_res = query._chelaile("085", "1")
assert [g[0] for g in _res["groups"]] == ["往a1（九巴）", "往a2（九巴）", "往b1（城巴）", "往b2（城巴）"]
assert _res["line"] == "1", _res["line"]              # 抬头不能写成「1（九巴）」
query.chelaile.lines, query.chelaile.live = _cl, _mod

# issue #9：车来了兜底别把单条线的 desc（「本线路末班时间已过」）当成全城原因
_cit2, _cl2, _lv2 = query.cities, query.chelaile.lines, query.chelaile.live
query.cities = lambda: {"北海": {"mygolbs": "北海市", "chelaile": "267"}}
query.chelaile.lines = lambda cid: [("1", "a"), ("2", "b")]
query.chelaile.live = lambda cid, lid: {"to": "x", "desc": "本线路末班时间已过",
                                        "state": -3, "buses": []}
query._cache.clear()
assert query._sweep_chelaile("北海市") == "北海市这会儿全线都没车报位置，末班都过了"
query.chelaile.live = lambda cid, lid: {"to": "x", "desc": "本线路末班时间已过",
                                        "state": -3 if lid == "a" else -5, "buses": []}
query._cache.clear()
assert "本线路" not in query._sweep_chelaile("北海市")   # 说法不一致就别拿一条解释全城
query.cities, query.chelaile.lines, query.chelaile.live = _cit2, _cl2, _lv2
query._cache.clear()

# 没直达车要给出换乘方案：「从玉福路口到玉林北站」坐 6路 到汽车总站换 G02
_seq = [query._catalog, query._station_seq]  # 存原函数，别拿数字占位
query._catalog = lambda cn: {"buslines": [
    {"lineName": "6路", "upperOrDown": "2", "from": "甲", "to": "人民大北路口"},
    {"lineName": "G02路", "upperOrDown": "1", "from": "汽车总站（二医院）", "to": "玉林北站"}]}
query._station_seq = lambda cn, line, d: {
    ("6路", "2"): [{"stationName": n, "station_lat": la, "station_lon": lo} for n, la, lo in [
        ("人民大北路口", 22.64, 110.15), ("玉福路口", 22.6069, 110.1267),
        ("电脑城（新民路）", 22.6200, 110.1400), ("汽车总站（大北路）", 22.6281824, 110.1440855)]],
    ("G02路", "1"): [{"stationName": n, "station_lat": la, "station_lon": lo} for n, la, lo in [
        ("汽车总站（二医院）", 22.6279276, 110.1456687), ("玉林北站", 22.70, 110.34)]],
}.get((line, d), [])
_a = {"stationName": "玉福路口", "relatedRoutes": ["6路"]}
_b = {"stationName": "玉林北站", "relatedRoutes": ["G02路"]}
_plan = query._hop_plan("玉林市", _a, "玉福路口", _b, "玉林北站", {"6路"}, {"G02路"})
# 电脑城离 G02 那站 900 多米，不算同一个换乘点；只认 300 米以内的
assert "汽车总站（大北路）" in _plan and "G02路" in _plan and "电脑城" not in _plan, _plan
# 换乘点必须在起点之后：把玉福路口挪到最后一个，就不该有方案
query._station_seq = lambda cn, line, d: [{"stationName": n, "station_lat": la, "station_lon": lo}
                                          for n, la, lo in [("汽车总站（大北路）", 22.6281824, 110.1440855),
                                                            ("玉福路口", 22.6069, 110.1267)]] \
    if (line, d) == ("6路", "2") else [
        {"stationName": n, "station_lat": la, "station_lon": lo} for n, la, lo in [
            ("汽车总站（二医院）", 22.6279276, 110.1456687), ("玉林北站", 22.70, 110.34)]]
assert query._hop_plan("玉林市", _a, "玉福路口", _b, "玉林北站", {"6路"}, {"G02路"}) == ""
# 起点这站的车接不上时，到附近 1.5 公里内找能接上的站（机场 T1 只有 142/156，专线只停 T2）
_st6 = query.stops
_t1 = {"stationName": "机场一号航站楼站", "lat": "38.2754228233", "lng": "114.69786839",
       "relatedRoutes": ["142路", "156路"]}
_t2 = {"stationName": "机场二号航站楼站", "lat": "38.2822972819", "lng": "114.6916457667",
       "relatedRoutes": ["142路", "156路", "机场公交专线1路"]}
_yd = {"stationName": "元氏火车站", "lat": "37.76", "lng": "114.53", "relatedRoutes": ["1001路"]}
query.stops = lambda cn, kw: [_t2] if kw == "机场" else []
query._station_seq = lambda cn, line, d: {
    ("机场公交专线1路", "2"): [{"stationName": n, "station_lat": la, "station_lon": lo} for n, la, lo in [
        ("机场二号航站楼站", 38.2822972819, 114.6916457667), ("南焦客运站", 37.99, 114.51)]],
    ("1001路", "2"): [{"stationName": n, "station_lat": la, "station_lon": lo} for n, la, lo in [
        ("南焦客运站", 37.99, 114.51), ("元氏火车站", 37.76, 114.53)]],
}.get((line, d), [])
_p2 = query._hop_plan("石家庄市", _t1, "机场一号航站楼", _yd, "元氏火车站",
                      {"142路", "156路"}, {"1001路"})
assert "机场公交专线1路" in _p2 and "1001路" in _p2, _p2
assert "走 993 米到 机场二号航站楼站" in _p2, _p2      # 说实话：得走到 T2 才有专线
query.stops = _st6
query._catalog, query._station_seq = _seq[0], _seq[1]
query._cache.clear()

# 「从北流汽车站到玉林北站」两站分属北流和玉林，但同一条 北流105路 直达：
# 得先按目录真查，查不到才说跨城，别拿城市名先判死
_rh, query.render_hop = query.render_hop, lambda ck, nm, got: "北流105路 直达（18 站）"
_cit3, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}, "北流": {"mygolbs": "北流市"}}
assert query.answer("从北流汽车站到玉林北站坐啥公交车", "玉林市") == "北流105路 直达（18 站）"
query.render_hop = lambda ck, nm, got: ""      # 目录里查不到（真跨城）才走跨城那句
assert "跨城" in query.answer("从北流汽车站到玉林北站坐啥公交车", "玉林市")
query.render_hop, query.cities = _rh, _cit3

# 「从机场一号航站楼到元氏火车站」没写城市：默认城查不到就沿用上一条问过的城
_rh2, _st7, _cit4 = query.render_hop, query.stops, query.cities
query.render_hop = lambda ck, nm, got: "" if ck == "玉林" else "石家庄 换乘"
query.stops = lambda cn, kw: []
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "石家庄": {"mygolbs": "石家庄市"}}
query._last("k-city")["city"] = "石家庄"
assert query.answer("从机场一号航站楼到元氏火车站怎么走", "玉林市", key="k-city") == "石家庄 换乘"
# 不带上文也能猜：站名里的「元氏」是元氏县，归石家庄
assert query.answer("从机场一号航站楼到元氏火车站怎么走", "玉林市", key="k-city2") == "石家庄 换乘"
query.render_hop, query.stops, query.cities = _rh2, _st7, _cit4

# 「玉林汽车站」平台没这个站：退到「车站」会匹上「玉林火车站」，退到「汽车站」查不到。
# 通用字尾巴不参与模糊匹，宁可说不认得也不能换个站答
_st5, query.stops = query.stops, lambda cn, kw: [
    {"stationName": "玉林火车站", "relatedRoutes": ["1路"]},
    {"stationName": "玉林火车站（出站口）", "relatedRoutes": ["1路"]},
] if kw in ("车站", "火车站") else []
assert query.find_stop("玉林市", "玉林汽车站")[0] is None
assert query.find_stop("玉林市", "玉林火车站")[0]["stationName"].startswith("玉林火车站")
# 站名认不出时别拿跨城那套顶包：跨城得两头都站得住
assert "跨城" not in query.answer("从北流汽车总站到玉林汽车站坐啥公交线路", "玉林市")
query.stops = _st5

# 「从北流到玉林汽车总站」：「北流」是地名不是站名，挑那一带能跟对面同线的站
assert query.hop("从北流到玉林汽车总站坐哪个公交线路") == ("北流", "玉林汽车总站")
_st4, query.stops = query.stops, lambda cn, kw: [
    {"stationName": "北流城南站", "relatedRoutes": ["北流105路"]},
    {"stationName": "北流客运中心", "relatedRoutes": ["北流105路", "玉林至北流城际公交线路"]},
] if kw == "北流" else []
_p = query._place_stop("玉林市", "北流", {"relatedRoutes": ["玉林至北流城际公交线路"]})
assert _p and _p["stationName"] == "北流客运中心", _p          # 跟对面同线的那个，不是城南站
assert query._place_stop("玉林市", "北流", {"relatedRoutes": ["2路"]}) is None   # 没有就不猜
query.stops = _st4
# 「汽车总站」按同名认，不能被「北流汽车总站」顶掉站序
_sq, query._station_seq = query._station_seq, lambda cn, line, d: [
    {"stationName": n} for n in (["北流汽车总站", "中段", "汽车总站", "末站"] if d == "2"
                                 else ["汽车总站", "中段", "北流汽车总站"])]
assert query._goes("玉林市", "X", "北流汽车总站", "汽车总站") == ("2", 0, 2)
assert query._goes("玉林市", "X", "汽车总站", "北流汽车总站") == ("1", 0, 2)
query._station_seq = _sq

# 「帮我查一下东胜区8路公交」：PREFIX_RE 只吃 4 个字，动词没剥干净时前缀会剩「下东胜区」，
# 拿去查就成了一条不存在的线；东胜区是鄂尔多斯的区，掌上不认区名当城市名
assert query.parse("帮我查一下东胜区8路公交") == ("8", "东胜区")
assert query.parse("帮我查东胜区8路公交") == ("8", "东胜区")     # 「查」不是动词表里的词，靠往右收到「东胜区」
assert query.parse("陆川1路有车吗") == ("1", "陆川")            # 真县名不能被收没
# 刚问过鄂尔多斯再问陆川1路：不能靠「沿用上一条城市」答成鄂尔多斯 1路
_c5, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}, "鄂尔多斯": {"mygolbs": "鄂尔多斯市"}}
_last5 = query._last("k-lc")
_last5["city"] = "鄂尔多斯"
_ans5 = query.answer("陆川1路有车吗", "玉林市", key="k-lc") or ""
assert "陆川" in _ans5, _ans5
query.cities = _c5

# 「佛山公交342路」→「伦教街道办事处公交线路」：站名提问也要沿用上一条问过的城，
# 默认城玉林的站表里没有伦教街道办事处，别去那儿找
_c6, _rs6, _cit6 = query.cities, query.render_stop, query.cities
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "佛山": {"mygolbs": "佛山市"}}
query.render_stop = lambda ck, cn, msg, src=None: "" if ck == "玉林" else "佛山 11 条"
query._last("k-fl")["city"] = "佛山"
assert query.answer("伦教街道办事处公交线路", "玉林市", key="k-fl") == "佛山 11 条"
query.cities, query.render_stop = _c6, _rs6
assert query.DISTRICTS.get("东胜区") == "鄂尔多斯市"
assert query.norm_city(query.DISTRICTS["东胜区"]) == "鄂尔多斯"
# 目录里这条线叫「东胜8路」，行政后缀得能省掉
_seen = []
_q, _r, _cat = query.query, query.render_at, query._catalog
query.query = lambda ck, wants, src=None, veh="": (_seen.append(list(wants)) or {"line": "东胜8路", "groups": []})
query.render_at = lambda *a, **k: ""
query._catalog = lambda cn: {"buslines": [{"lineName": "东胜8路"}]}
query._one("鄂尔多斯", "鄂尔多斯市", "8", "东胜区", "帮我查一下东胜区8路公交", None, True, {})
query.query, query.render_at, query._catalog = _q, _r, _cat
assert "东胜8" in _seen[0], _seen

# 站名猜不出是哪座城：问一句，别回空让模型自己圆话
assert "是哪座城的" in query.answer("伦教街道办事处公交线路", "玉林市", key="k-ask")
assert query.answer("晚上吃点啥呢", "玉林市", key="k-ask2") == ""      # 闲聊不许反问
_c7, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}, "佛山": {"mygolbs": "佛山市"}}
assert query.detect_city("南海区8路", "南海区", "玉林市")[0] == "佛山"  # 区县名在句子里也能猜
query.cities = _c7

# 用户常写的「XX市XX线」：不带「路」也得认，BUS_RE 也要认，否则 bus_query 返回 None
assert query.parse("昆明市219线有几趟车") == ("219", "昆明市")
assert query.BUS_RE.search("昆明市219线有几趟车")
# 「往呈贡广场方向」只答这个方向
assert query._dir("我在昆明市的雨花公交枢纽站，219路往呈贡广场方向什么时候能到") == "呈贡广场"
assert query._dir("219路现在有几辆车") == ""
_rd = query.render("昆明", "219", {"line": "219路",
        "groups": [("呈贡广场", [("08820", "回回营", 0, "")]), ("雨花公交枢纽站", [("08825", "和谐路口", 600, "")])]},
        dir="雨花公交枢纽站")
assert "08825" in _rd and "08820" not in _rd, _rd

# 「这四条线路哪条在跑」范围是那个站，不是全城；「玉林北站定制夜班车」线名不是数字+路
_cat4, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("G01路", "玉林北站定制夜班车")]}
_lm4, query._live_my = query._live_my, lambda cn, n, d, order=1: {
    "hasReal": "1", "list": [{"busNumber": "桂K1", "stationName": "S"}]
    if n.startswith("玉林") else []}
_s4 = query.render_sweep("玉林市", False, ["G01路", "玉林北站定制夜班车"])
assert "玉林北站定制夜班车 1 辆" in _s4 and "G01路 没车报位置" in _s4, _s4
assert query._msg_line("玉林市", "那玉林北站定制夜班车呢？") == "玉林北站定制夜班车"
assert query._msg_line("玉林市", "玉林北站有哪几条公交线？") is None
query._catalog, query._live_my = _cat4, _lm4

# 重庆「0492」是夜班线，和白天线的「492路」是两条线：带前导零的四位号不加「路」也认
assert query.parse("重庆0492") == ("0492", "重庆")
assert query.BUS_RE.search("重庆0492")
assert query.parse("重庆492路") == ("492", "重庆")      # 两位数写法不能串到 0492
# 上海 343 是夜宵线，口语带「夜宵线」三个字照样认成 343 去目录里查
assert query.parse("上海343夜宵线现在有没有车") == ("343", "上海")
assert query.BUS_RE.search("上海343夜宵线现在有没有车")
assert not query.BUS_RE.search("晚上吃夜宵去")
# 四位数线号（石家庄 1002/1101）：num 只吃 3 位时，正则会从第二位起匹成「002」「101」
assert query.parse("石家庄1002路现在有没有车") == ("1002", "石家庄")
assert query.parse("石家庄1101路现在有没有车") == ("1101", "石家庄")
assert query.BUS_RE.search("石家庄1101路有没有车")
# 带字母的线号最高五位（C621A），字母在前在后都有目录在用：B601/121A/1001M
assert query.parse("佛山C621A路有车吗") == ("C621A", "佛山")
assert query.parse("广州121A路有车吗") == ("121A", "广州")
assert query.parse("深圳B601路") == ("B601", "深圳")
assert query.parse("苏州1001M路") == ("1001M", "苏州")
assert query.parse("2026年怎么坐车") is None          # 纯数字不带路，不是线号
# 「9KB」是文件大小，不是线号：字母前后各最多一个（真实线号没有「数字+两个字母」的）
assert query.parse("9KB") is None
assert query.parse("500MB") is None
assert query.parse("99KB") is None
assert not query.BUS_RE.search("9KB")
assert query.parse("玉林9路") == ("9", "玉林")          # 放宽字母数别把普通线号带坏
# 「9999x 8123」是算式：乘号连着两个数字就不是线号（9KB 收窄后 9999X 仍匹得上）
assert query.parse("9999x 8123等于多少") is None
assert query.parse("9999x8123") is None
assert query.parse("800×600") is None
assert query.answer("9999x 8123等于多少", "玉林市") == ""   # 别回「没查到 9999X 这条线」
# 「3D 打印」「5G 网络」：一位数字＋量纲字母不是线号；香港 10M/11D 是两位数的真线号
for _u in ("3D打印", "4K视频", "5G网络慢", "2K屏", "4G信号差"):
    assert query.parse(_u) is None, _u
assert query.parse("10M") == ("10M", "")
assert query.parse("11D") == ("11D", "")
assert query.parse("现在用这个免费的hy4，慢的批爆") is None   # 旧版匹成 ('HY4','个免费')

# 站名里带裸县名（「元氏火车站」→ 元氏县 → 石家庄）：别落回默认城玉林交白卷
_c8, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"},
                                           "石家庄": {"mygolbs": "石家庄市"}}
assert query._district_city("机场一号航站楼元氏火车站") == "石家庄"
assert query._district_city("玉林北站") is None          # 撞名的裸名不收，别乱猜
_rh8, query.render_hop = query.render_hop, lambda ck, nm, got: f"HOP:{ck}" if ck == "石家庄" else None
_rp8, query.render_plan = query.render_plan, lambda nm, a, b: ""
assert query.answer("从机场一号航站楼到元氏火车站怎么走", "玉林市", key="k-d8") == "HOP:石家庄"
query.cities, query.render_hop, query.render_plan = _c8, _rh8, _rp8
_c9, query.cities = query.cities, lambda: {"玉林": {}, "石家庄": {}, "北流": {}}
# 「石家庄从谈固到元氏客运站」：城名和「从」连在一起时才剥；「北流汽车总站」的城名是站名本身
assert query.hop("石家庄从谈固到元氏客运站怎么走") == ("谈固", "元氏客运站")
assert query.hop("从北流到玉林汽车总站") == ("北流", "玉林汽车总站")
assert query.hop("从广州公园前到玉林北站的话，坐什么车") == ("广州公园前", "玉林北站")
query.cities = _c9
# 站序里写「元氏车站」、站表里叫「元氏客运站」，是同一个站；谈固站≠谈固东站
assert query._same_station("元氏客运站", "元氏车站")
assert not query._same_station("谈固站", "谈固东站")

# 换乘先问掌上自己的方案（CMD=111）：多条、按总时长排序、带步行距离
_p111, query._post = query._post, lambda p, timeout=8: (
    {"status": 1, "info": [{
        "stationNum": "21站", "totalTime": "1小时6分钟", "totalWalkDistance": "371米",
        "routeUpDownSimple1": [{"routeName": "28路", "upStationName": "玉林火车站"}],
        "routeUpDownSimple2": [{"routeName": "北流105路", "upStationName": "北流客运中心"}],
    }, {
        "stationNum": "31站", "totalTime": "1小时14分钟",
        "routeUpDownSimple1": [{"routeName": "G01路", "upStationName": "玉林火车站"}],
        "routeUpDownSimple2": [{"routeName": "玉林至北流城际公交线路", "upStationName": "玉林高中初中部"}],
    }]} if p.get("ENDPOINTNAME") == "北流汽车总站" else
    {"status": 1, "info": [{"stationNum": "27站", "totalTime": "49分钟",
                            "routeUpDownSimple1": [{"routeName": "G02路", "upStationName": "玉林汽车总站"}]}]})
assert query.render_plan("玉林市", "玉林火车站", "北流汽车总站") == (
    "从玉林火车站到北流汽车总站：\n"
    "坐 28路，到北流客运中心换 北流105路，21站、约1小时6分钟、走路371米\n"
    "或者坐 G01路，到玉林高中初中部换 玉林至北流城际公交线路，31站、约1小时14分钟")
assert query.render_plan("玉林市", "玉林汽车总站", "玉林北站") == ""   # 直达交回 render_hop，那边带车在哪
query._post = _p111
query._cache.clear()

# 「香港城巴1」：同一个号五家公司各有车，点了名只报这一家；没点名照旧合并
assert query.parse("香港城巴1") == ("1", "香港城巴")        # 香港人不带「路」字
assert query.parse("城巴1") == ("1", "城巴")
_cl, query.chelaile = query.chelaile, type("C", (), {
    "lines": staticmethod(lambda cid: [("1（九巴）", "l1"), ("1（九巴）", "l2"),
                                       ("1（城巴）", "c1"), ("1（城巴）", "c2")]),
    "live": staticmethod(lambda cid, lid: {"to": f"{lid}方向", "buses": [("14", "某站", 2300)], "desc": ""}),
})
query._cache.clear()
_hi = query._chelaile("085", "城巴1")
assert _hi["line"] == "1（城巴）" and [g[0] for g in _hi["groups"]] == ["c1方向", "c2方向"], _hi
assert query._chelaile("085", "1")["line"] == "1"      # 没点名：两家都报，抬头去掉公司
query.chelaile = _cl
query._cache.clear()

# 只给车牌：省简称+字母+5 位（闽AYC505）或 6 位（闽A17997D 新能源）
assert query.PLATE_RE.search("查询一下闽AYC505有没有在路上").group(1) == "闽AYC505"
assert query.PLATE_RE.search("福州闽A17997D").group(1) == "闽A17997D"
assert query.PLATE_RE.search("玉林9路") is None
assert query.BUS_RE.search("闽AYC505")           # 光给车牌也得让快路接单
assert query.PLATE["闽A"] == "福州市"
assert query._plate_key("闽A·17997d") == query._plate_key("A17997D") == "A17997D"
# 找不到要报扫了多少，别报成「全城都没有」；找到要报线路、方向和站位
_cat, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "9路", "from": "甲站", "to": "乙站", "upperOrDown": "1"}]}
_pi, query._plate_index = query._plate_index, lambda cn: ({"K12345": ("9路", "1", "火车站")}, 50, 80)
query._cache.clear()
assert query.render_plate("玉林市", "桂K12345") == "桂K12345 在 9路 上，往乙站方向，刚过 火车站"
assert "只扫了前 50 条" in query.render_plate("玉林市", "桂K99999")
query._plate_index = lambda cn: ({}, 80, 80)
query._cache.clear()
assert "80 条方向全扫了" in query.render_plate("玉林市", "桂K99999")
query._plate_index, query._catalog = _pi, _cat
query._cache.clear()

# 引用消息：里面的线路号不能把正文的车牌问题顶掉（引用「G02 现在 2 辆」问「桂K12345 自编号」）
qm = query.QUOTE_RE.search("[引用消息(: 有，G02路 现在 2 辆在路上)] 桂K12345自编号是啥")
assert qm and "G02路 现在 2 辆" in qm.group(1)
assert query.SELFNO_RE.search("桂K12345自编号是啥") and not query.SELFNO_RE.search("桂K12345")
assert query.parse("有，G02路 现在 2 辆在路上")          # 引用原文本身会被 parse 认成线，才需要先剥掉

# 自编号：掌上没这个字段，只能报车来了挂的号，且说清对不上
_cl2, query.chelaile = query.chelaile, types.SimpleNamespace(
    lines=lambda cid: [("9", "L1")],
    live=lambda cid, lid: {"buses": [("514", "甲站", 100), ("397", "乙站", 900)]})
_ci, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
query._plate_index = lambda cn: ({"K12345": ("9路", "1", "火车站")}, 50, 80)
query._cache.clear()
_sn = query.render_selfno("玉林", "玉林市", "桂K12345")
assert "只报车牌不报自编号" in _sn and "514、397" in _sn, _sn
query._plate_index = lambda cn: ({}, 80, 80)
query._cache.clear()
assert "没扫到" in query.render_selfno("玉林", "玉林市", "桂K99999")
query._plate_index, query.cities, query.chelaile = _pi, _ci, _cl2
query._cache.clear()

# 「A 咋去 B」「A 怎么去 B」：没「到」字也要认成换乘问句；「我在X咋去Y」把「我在」剥掉
assert query.hop("玉林火车站咋去北流万达") == ("玉林火车站", "北流万达")
assert query.hop("玉林火车站怎么去北流万达") == ("玉林火车站", "北流万达")
assert query.hop("我在十字街咋去火车站") == ("十字街", "火车站")
assert query.hop("怎么去北流万达") is None              # 没出发站，不猜
assert query.hop("玉林9路咋去") is None                # 左边是线路号
assert query.hop("从玉林火车站到北流万达") == ("玉林火车站", "北流万达")   # 老的「到」式没坏

# 「柳州快1」：快/慢 是线名的一部分（目录里叫「快1号线」），不是城市名的尾巴
_cs = query.cities
query.cities = lambda: {"柳州": {"mygolbs": "柳州市"}, "玉林": {}}
assert query.parse("柳州快1") == ("快1", "柳州")
assert query.parse("柳州快1路") == ("快1", "柳州")
assert query.parse("柳州快9区间") == ("快9区间", "柳州")
assert query.parse("快乐1天") is None           # 「快」前面不是城市名就不认
query.cities = _cs

# 「大容山专线」是目录名「大容山旅游专线（28路）」的简称：少说一个字也要认，别退去查站名
_oc = query._catalog
query._catalog = lambda n: {"buslines": [
    {"lineName": "大容山旅游专线（28路）", "upperOrDown": "1", "to": "大容山景区"},
    {"lineName": "大容山旅游专线（28路）", "upperOrDown": "2", "to": "玉林火车站"},
    {"lineName": "9路", "upperOrDown": "1", "to": "汽车总站"}]}
assert query._msg_line("玉林市", "玉林大容山专线") == "大容山旅游专线（28路）"
assert query._msg_line("玉林市", "大容山旅游专线") == "大容山旅游专线（28路）"
assert query._msg_line("玉林市", "大容山好远") is None      # 字不全，别硬塞一条线
query._catalog = _oc

print("selftest ok")

if "--live" in sys.argv:
    import importlib

    importlib.reload(query)  # 拿掉上面被替换掉的假城市表
    for msg in ["玉林 G02 路有没有车", "香港 1 路还有几站", "大连 2 路到哪了",
                "玉林 从十字街到火车站坐什么车", "南宁 我在金湖广场，16路还有几站",
                "南宁 金湖广场有几路车", "容县 11 路有车吗", "玉林 6 路夜班还有几站"]:
        print("\n问:", msg)
        print(query.answer(msg, "玉林市"))
