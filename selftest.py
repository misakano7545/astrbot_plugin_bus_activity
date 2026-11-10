"""离线自检：python3 selftest.py

不联网，只验解析/匹配/排版这些纯逻辑；加 --live 再打一次真接口。
"""
import re

import sys
import types

import mygolbs
import watch
import query
import yulin

# 「用车来了查化州-湛江线」：带「-」的文字线名 parse 认不出，但它是点名的另一条线，
# 不能当成「追问上一条」续成上一轮的 506（放最前面：后面的桩会污染）
query.LAST["k-hz"] = {"city": "湛江", "line": "506"}
assert "化州-湛江线" in (query.answer("用车来了查化州-湛江线", "湛江市", key="k-hz") or "")

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
# 「苏州地铁一号线」：中文数字在目录里是阿拉伯（地铁1号线）
assert query._exact_line("苏州市", "苏州地铁一号线", "地铁一号", "苏州") == "地铁1号线"
# 「苏州地铁3/11号线」：一个后缀管两个号，两条都要答，且前缀「地铁」不能丢
assert [n for n, _ in query._lines("苏州地铁3/11号线")] == ["地铁3", "地铁11号"]
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
# 车来了目录里的线名是裸号，念出来像半句话：大陆加「路」，香港不加（506、B8）
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])]}) == \
    "1路 这会儿没车"
assert query.render("香港", "506", {"source": "车来了", "line": "506", "groups": [("", [])]}) == \
    "506 这会儿没车"
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])],
                                  "note": "末班 19:50 已经开走了"}) == \
    "1路 这会儿没车，末班 19:50 已经开走了"
# 线路不存在 / 有近似线名 / 上游异常：别把机器串甩进群里
assert query.render("玉林", "999", {"error": "没这条线路"}).startswith("没查到「999」这条线")
# 没查到要给同城近邻，否则模型自己解释成「这数据源没有这个城市」
_g0, query.buspedia = query.buspedia, types.SimpleNamespace(region_id=lambda cn: None)   # 桩掉建档源，只测近邻那条
assert "这边有的是" in query.render("玉林", "9", {"error": "没这条线路"})
query.buspedia = _g0
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
_cat, mygolbs.post = mygolbs.post, lambda *a, **k: {"buslines": _rows, "status": 1}
query._cache.clear()
query._mygolbs("x市", "1")                     # 先让 dict 进缓存
assert query._catalog("x市").get("buslines") == _rows
assert query._catalog("x市")["buslines"][0]["lineName"] == "1路"
mygolbs.post = _cat
query._cache.clear()

# Origin 头是掌上公交的唯一命门：带上多余的斜杠或换 http，上游回 200 + 58 字节占位串
assert mygolbs.HEADERS["Origin"] == "https://h5.mygolbs.com"
assert not mygolbs.HEADERS["Origin"].endswith("/")

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
_p114, mygolbs.post = mygolbs.post, lambda p, timeout=8: {"buslines": [
    {"routeBean": {"routeName": "9路", "company": "玉林公交", "companyMobile": ""}},
    {"routeBean": {"routeName": "1路", "company": "一汽巴士", "companyMobile": "一汽巴士:020-87763773"}}]}
assert query.render_company("玉林市", "9") == "9路 归玉林公交\n平台没挂咨询电话，丢东西直接打 12345 或去线路终点站的调度室问"
assert query.render_company("玉林市", "1") == "1路 归一汽巴士，咨询电话 020-87763773"
mygolbs.post = _p114

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
# 「查不到」的「到」不是路径的「到」：这句吐槽曾被切成「有一条线路查不」→「实时信息我就不写出来了（」
assert query.hop("有一条线路查不到实时信息我就不写出来了（）") is None
assert query.hop("这个站查不到了") is None
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
# 两家报的车不完全一样（6路实测 掌上 12 / 车来了 13）：只报一家就必然差一两辆
_u1 = {"source": "掌上公交", "line": "6路", "groups": [("A端", [("桂K1", "甲站", 100)]), ("B端", [("桂K2", "乙站", 200)])],
       "pos": {"桂K1": {"lat": 22.6000, "lng": 110.1000}, "桂K2": {"lat": 22.6500, "lng": 110.2000}}}
_u2 = {"source": "车来了", "line": "6", "groups": [("x方向", [("41", "甲站", 100), ("42", "甲站", 300), ("43", "乙站", 200)])],
       "pos": {"41": {"lat": 22.6005, "lng": 110.1005}, "42": {"lat": 22.6040, "lng": 110.1040},
               "43": {"lat": 22.6505, "lng": 110.2005}}}
_u = query._union(_u1, _u2)
assert sum(len(b) for _, b in _u["groups"]) == 3, _u      # 41/43 是同一辆车，42 才是多的那辆
assert [g[0] for g in _u["groups"]] == ["A端", "B端"], _u  # 多出来的归到最近的已报车那组
assert _u["groups"][0][1][-1][0] == "42", _u
assert query._union(_u1, {"source": "车来了", "groups": [("x", [])], "pos": {}}) is _u1  # 另一家没报车就别动

# 上游业务失败（HTTP 200 + status "-2"）不能当成「没这条线路」
_post, mygolbs.post = mygolbs.post, lambda *a, **k: {"status": "-2", "msg": "当前城市暂无法提供公交查询", "error": True}
query._cache.clear()
assert query._mygolbs("火星市", "1")== {"error": "当前城市暂无法提供公交查询"}
mygolbs.post = _post

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
_w0, query._window = query._window, lambda f, l: "open"      # 这条只验首末班+站数，运营窗口另测
_got = query._mygolbs("上海市", "浦东51")
assert _got["unknown"] and "05:40 起、末班最晚 21:00" in _got["note"] and "27 站" in _got["note"], _got
query._window = _w0
# 但过了末班不能还说「起、末班最晚」：两个方向各有时刻，得按方向报，并说清收车了
query._window = lambda f, l: "after"
query._first_last = lambda c, l, d: ("06:30", "19:30") if d == "1" else ("07:30", "20:30")
query._catalog = lambda c: {"buslines": [
    {"lineName": "9路", "upperOrDown": d, "from": "A" if d == "1" else "B", "to": to}
    for d, to in (("1", "市委党校"), ("2", "东堤车站"))]}
_g9 = query._mygolbs("湛江市", "9")
assert "末班已经开走：往市委党校 06:30–19:30；往东堤车站 07:30–20:30" in _g9["note"], _g9
assert "末班最晚" not in _g9["note"], _g9
# 过了末班就别再说「上游不给这城实时」——昆明掌上整城不给车机，车来了照样有，那句话像在说这城没数据
assert not _g9["unknown"] and "上游不给" not in _g9["note"], _g9
query._window = _w0
query._live_my, query._first_last, query._station_seq, query._catalog = _lm, _fl, _seq, _cat

# 「河池20路」：这线就一辆车在跑（07:00–17:00 / 08:00–18:00），车回场待发那阵两家同时看不到它，
# 只回「线路还在跑，只是车机没报位置」被群友读成「查不到」。把两个方向的首末班带上自证
assert query._hours_note([("岜仑路口", "07:00", "17:00"), ("龙江桥头", "08:00", "18:00")]) \
    == "往岜仑路口 07:00–17:00、往龙江桥头 08:00–18:00"
assert query._hours_note([]) == ""
assert query._hours_note([("A", "07:00", ""), ("B", "08:00", "18:00")]) == "往B 08:00–18:00"
_lm, _fl, _cat = query._live_my, query._first_last, query._catalog
_w0, query._window = query._window, lambda f, l: "open"     # 在运营窗口内才走这条路
query._live_my = lambda c, n, d, order=1: {"status": "1", "hasReal": "1",
                                           "msg": "获取实时数据成功", "list": []}
query._first_last = lambda c, l, d: ("07:00", "17:00") if d == "1" else ("08:00", "18:00")
query._catalog = lambda c: {"buslines": [
    {"lineName": "20路", "upperOrDown": "1", "from": "龙江桥头", "to": "岜仑路口"},
    {"lineName": "20路", "upperOrDown": "2", "from": "岜仑路口", "to": "龙江桥头"}]}
_g20 = query._mygolbs("河池市", "20")
assert _g20["note"] == "线路还在跑，只是车机没报位置（往岜仑路口 07:00–17:00、往龙江桥头 08:00–18:00）", _g20["note"]
assert query.render("x", "20", _g20) == "20路 这会儿没车，" + _g20["note"], query.render("x", "20", _g20)
# 拿不到首末班时别硬编括号，仍回原来那句
query._first_last = lambda c, l, d: ("", "")
assert query._mygolbs("河池市", "20")["note"] == "线路还在跑，只是车机没报位置"
query._window = _w0
query._live_my, query._first_last, query._catalog = _lm, _fl, _cat

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
# 但只跳裸号：带汉字前缀的线名只有掌上目录认（新郑12 在郑州市那 1283 条里，车来了是裸号体系）
_cap3 = []
query._mygolbs = lambda c, w: (_cap3.append(w), {"source": "掌上公交", "line": "新郑12路",
                                                "groups": [("", [])], "note": "", "unknown": True})[1]
_rq("测试", ["新郑12"])
assert _cap3 == ["新郑12"], _cap3
# 县级市（新郑、巩义）在掌上不是一个城市，线路挂在父城目录里、线名带县名前缀
assert query.norm_city(query.DIST_BARE["新郑"]) == "郑州"
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
# 「北京345快」退到「北京3」会匹到「北京三十九中学」：城市名后面只剩数字的不是站名
query.stops = lambda cn, kw: ([{"stationName": "北京三十九中学", "relatedRoutes": ["345路"]}]
                             if kw in ("北京3", "北京三十九中学") else [])
assert query.find_stop("北京市", "北京345快")[0] is None, query.find_stop("北京市", "北京345快")
# 「玉林八中」平台写作「玉州区第八中学」：简称搜到全城只有一个才认
query.stops = lambda cn, kw: ([{"stationName": "玉州区第八中学", "relatedRoutes": ["G01路"]}]
                             if kw == "八中" else [])
assert query.find_stop("玉林市", "玉林八中")[0]["stationName"] == "玉州区第八中学"
assert query.find_stop("玉林市", "玉林九中")[0] is None      # 搜不到就不认
# 「市一职校（玉林技师学院）」和「玉林技师学院（市一职中）」是同一所学校的两个门，
# 各挂各的线：括号里的别名也算这个名字，否则只答出挂在这条记录上的 1 条
_zy = [{"stationName": n, "lat": lat, "lng": lng, "relatedRoutes": rs} for n, lat, lng, rs in (
    ("市一职校（玉林技师学院）", "22.6522835", "110.2035112", ["玉林至北流城际公交线路"]),
    ("玉林技师学院（市一职中）", "22.6518827", "110.2012555", ["7路", "G01路"]),
    ("玉林技师学院", "22.6512149", "110.1989097", ["7路", "G01路"]))]
assert {n for n, _ in query._same_rows(_zy, _zy[0])} == {
    "市一职校（玉林技师学院）", "玉林技师学院（市一职中）", "玉林技师学院"}
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
_ow3, query._oneway_lines = query._oneway_lines, lambda *a: {}
_out = query.render_stop("玉林", "玉林市", "汽车总站有哪几路车")
assert "汽车总站（二医院）（1 条）" in _out and "汽车总站（1 条）" in _out \
    and "陆川1路" not in _out, _out
query._catalog, query.stops, query._oneway_lines = _cat3, _st3, _ow3
# 平台没给坐标时退回按名字匹，不能因为缺字段就把线路全丢了
assert query._merge_same([_rows[0], dict(_rows[1], lat=None)], _rows[0])["relatedRoutes"] == ["9路", "G02路"]
# 站表不许截断：23 条就报 23 条，否则用户再问「剩下的呢」认不出站名
_cat, _stops, _ow4 = query._catalog, query.stops, query._oneway_lines
query._oneway_lines = lambda *a: {}
query._catalog = lambda cn: {"buslines": [{"lineName": f"{i}路", "from": "甲", "to": "乙"} for i in range(23)]}
query.stops = lambda cn, kw: [{"stationName": "谈固站", "relatedRoutes": [f"{i}路" for i in range(23)]}]
_out = query.render_stop("石家庄市", "石家庄市", "谈固站有哪几路公交车")
assert _out.count("\n") == 23 and "还有" not in _out, _out
query._catalog, query.stops, query._oneway_lines = _cat, _stops, _ow4
# 单向停靠：这站只有一个方向停时，站表/等车/换乘都得说清「只往哪边」，不然人会一直等反方向
_seq9, query._station_seq = query._station_seq, lambda cn, ln, d: (
    [{"stationName": "玉林火车站"}, {"stationName": "汽车总站"}] if ln == "2路" and d == "1"
    else [{"stationName": "苗园中秀路口（火车站）"}, {"stationName": "汽车总站"}] if ln == "2路"
    else [{"stationName": "玉林火车站"} for _ in range(2)])
_cat9, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "2路", "upperOrDown": "1", "from": "万象城", "to": "客运中心"},
    {"lineName": "2路", "upperOrDown": "2", "from": "客运中心", "to": "万象城"},
    {"lineName": "9路", "upperOrDown": "1", "from": "甲", "to": "乙"},
    {"lineName": "9路", "upperOrDown": "2", "from": "乙", "to": "甲"}]}
assert query._oneway_lines("玉林市", "玉林火车站", ["2路", "9路"]) == {"2路": "1"}
assert "只有往客运中心方向停（万象城 → 客运中心），反方向不经过这站" \
    in query._oneway_note("玉林市", "玉林火车站", "2路", "1")
assert query._oneway_note("玉林市", "玉林火车站", "9路", "1") == ""
# 站序里写成「苗园中秀路口（火车站）」的那个方向不算停这站
assert not query._seq_has("玉林市", "2路", "2", "玉林火车站")
query._station_seq, query._catalog = _seq9, _cat9
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
# 「现在513去哪里了」：句子里只夹了个车号（不写「车」也不写「G02的」），以前匹不上，
# 于是整条线两辆车一起报出来——问一辆、回两辆，用户要的那辆还混在里头
assert query._veh_from(_res, "现在513去哪里了") == "513"
assert query._veh_from(_res, "513高新产业园那辆车") == "513"
assert query._veh_from(_res, "玉林G02现在到哪了，513高新产业园那辆车") == "513"
assert query._veh_from(_res, "513路现在到哪") == ""       # 513路 是线号
assert query._veh_from(_res, "现在呢") == ""              # 没车号就交给下面「沿用上一辆」
assert query._veh_from({"groups": [("甲", [("桂K12627D", "x", 1)])]}, "12627 那辆车在哪") == "桂K12627D"
# 整块车牌、以及车牌里的数字（后面粘着 D 也要认）
_pl = {"groups": [("甲", [("桂K12623D", "x", 1)]), ("乙", [("桂K12682D", "y", 2)])]}
assert query._veh_from(_pl, "玉林G02 桂K12623D 到哪了") == "桂K12623D"
assert query._veh_from(_pl, "桂K12682D 到哪") == "桂K12682D"
assert query._veh_from(_pl, "G02还有几辆") == ""
# 号格式各城不一样：掌上可能报车牌（玉林）、也可能报纯数字自编号（石家庄 7376、郑州 15479）
# 或机器号（武汉 8A9E8AA8）；车来了可能报自编号，也可能报车牌。所以按形状无关的规则匹，
# 匹不上再按 GPS 把两家的号配一次
assert query._id_kind("桂K12627D") == "plate" and query._id_kind("513") == "selfno"
assert query._id_kind("8A9E8AA8") == "code" and query._id_kind("000105") == "selfno"
assert query._id_eq("桂K12627D", "K12627D") and query._id_eq("桂K12627D", "12627")
assert query._id_eq("000105", "105") and not query._id_eq("513", "514")
_plate_res = {"groups": [("甲", [("桂K12627D", "x", 1)]), ("乙", [("桂K12623D", "y", 2)])]}
_lid_keep, query._line_ids = query._line_ids, lambda ck, cn, line: {
    "mygolbs": {"桂K12627D": (22.72, 110.23, "A", "1"), "桂K12623D": (22.71, 110.22, "B", "1")},
    "chelaile": {"513": (22.7201, 110.2301, "A", "to"), "511": (22.7101, 110.2201, "B", "to")}}
assert query._veh_from(_plate_res, "现在513去哪里了", "玉林", "玉林市", "G02路") == "桂K12627D"
assert query._veh_from(_plate_res, "513高新产业园那辆车", "玉林", "玉林市", "G02路") == "桂K12627D"
_sn_res = {"groups": [("甲", [("513", "x", 1)]), ("乙", [("511", "y", 2)])]}
assert query._veh_from(_sn_res, "桂K12623D 到哪了", "玉林", "玉林市", "G02路") == "511"
assert query._veh_from(_plate_res, "现在999去哪里了", "玉林", "玉林市", "G02路") == ""   # 配不上就不瞎认
assert query._veh_from(_plate_res, "513路现在到哪", "玉林", "玉林市", "G02路") == ""    # 513路 是线号
# 同一格式最好使：石家庄那种纯数字自编号，号在清单里就直接对上，不用 GPS 配
assert query._veh_from({"groups": [("甲", [("7376", "x", 1)]), ("乙", [("7369", "y", 2)])]},
                       "7376 到哪了", "石家庄", "石家庄市", "43路") == "7376"
assert query._veh_from({"groups": [("甲", [("8A9E8AA8", "x", 1)])]},
                       "8A9E8AA8 到哪了", "武汉", "武汉市", "913路") == "8A9E8AA8"
query._line_ids = _lid_keep
# 号格式各城不同，别把「车牌」「自编号」绑死在某一家上——实测过的四种写法：
# 玉林 掌上=车牌/车来了=513；河池 车来了=000105；武汉 两家都=8A9E8AA8；南宁 两家都=车牌
assert query._id_eq("000105", "105") and query._id_eq("8A9E8AA8", "8a9e8aa8")
assert not query._id_eq("桂K12627D", "桂K12682D")
assert query._id_kind("8A9E8AA8") == "code" and query._id_kind("7376") == "selfno"
# 车来了报 000105 这种带前导零的自编号时，问「105 到哪了」也要能匹上
_lead0 = {"groups": [("甲", [("000105", "A", 1)]), ("乙", [("000111", "B", 2)])]}
assert query._veh_from(_lead0, "105 到哪了", "河池", "河池市", "8路") == "000105"
# 武汉那种机器号
_mach = {"groups": [("甲", [("8A9E8AA8", "A", 1)]), ("乙", [("9CBF001E", "B", 2)])]}
assert query._veh_from(_mach, "8a9e8aa8 现在在哪", "武汉", "武汉市", "913路") == "8A9E8AA8"
# 两家都给车牌（南宁）：同一辆对得上，别当成两个号
_twoplate = {"groups": [("甲", [("桂A00658D", "A", 1)])]}
assert query._veh_from(_twoplate, "桂A00658D 到哪了", "南宁", "南宁市", "k10路") == "桂A00658D"
# 真线名要带「路」：掌上实时只认「G02路」，拿「G02」去要 mate 会一个都配不上
_oc13, query._catalog = query._catalog, lambda cn: {"buslines": [{"lineName": "G02路"}]}
assert query._line_real("玉林市", "G02") == "G02路"
query._catalog = _oc13
# 「现在513去哪里了」「现在呢」没写线号：短句追问沿上一句那条线
assert query._vehicle_talk("现在513去哪里了") and query._vehicle_talk("现在呢")
assert query._vehicle_talk("那辆呢") and query._vehicle_talk("它到哪了")
assert not query._vehicle_talk("晚上吃点啥呢") and not query._vehicle_talk("现在几点了")
assert not query._vehicle_talk("513路现在到哪")
# 追问「现在呢」沿用上一句那辆；问「G02还有几辆」就该报整条线
_lq, query.query = query.query, lambda ck, wants, src=None, veh="": dict(_res, source="车来了")
_lr, query.render = query.render, lambda ck, want, res, veh="", d="", shown="": f"RENDER[{want}|veh={veh}|{shown}]"
_lw = {}
assert query._one("玉林", "", "G02", "", "现在513去哪里了", None, True, _lw) == "RENDER[G02|veh=513|]"
assert query._one("玉林", "", "G02", "", "现在呢", None, True, _lw) == "RENDER[G02|veh=513|]"
assert query._one("玉林", "", "G02", "", "G02还有几辆", None, True, _lw) == "RENDER[G02|veh=|]"
assert "veh" not in _lw                      # 问了整条线就不再盯那辆车
query.query, query.render = _lq, _lr
# 掌上那份是车牌、用户按自编号问：抬头把两个号都写上，追问也保持；两家的号按 GPS 配
_lq2, query.query = query.query, lambda ck, wants, src=None, veh="": dict(_plate_res, source="掌上公交")
_lr2, query.render = query.render, lambda ck, want, res, veh="", d="", shown="": f"RENDER[{want}|veh={veh}|{shown}]"
_ms2, query._selfno_mate = query._selfno_mate, lambda ck, cn, line: (
    {"K12627D": ("513", 30), "K12623D": ("511", 120)}, {})
_lid2, query._line_ids = query._line_ids, lambda ck, cn, line: {
    "mygolbs": {"桂K12627D": (22.72, 110.23, "A", "1"), "桂K12623D": (22.71, 110.22, "B", "1")},
    "chelaile": {"513": (22.7201, 110.2301, "A", "to"), "511": (22.7101, 110.2201, "B", "to")}}
_ocs, query._catalog = query._catalog, lambda cn: {"buslines": [{"lineName": "G02路"}]}
_lw2 = {}
assert query._one("玉林", "玉林市", "G02", "", "现在513去哪里了", None, True, _lw2)     == "RENDER[G02|veh=桂K12627D|513（桂K12627D）]"
assert query._one("玉林", "玉林市", "G02", "", "现在呢", None, True, _lw2)     == "RENDER[G02|veh=桂K12627D|513（桂K12627D）]"
query.query, query.render, query._selfno_mate, query._catalog = _lq2, _lr2, _ms2, _ocs
query._line_ids = _lid2

# 「玉林北站要等多久才能等到G02」：站名整段在线号前面时 parse 把它当前缀取走了，
# 取站那步从整句里抠出来的是「玉林北站要等多久才能等」，于是反口问「是哪一站等」。
# 前缀真在站表里就拿它当站名再问一遍
_rss, query.render_at = query.render_at, lambda ck, cn, w, m, veh="", tally=None, at="": (
    f"AT[{at}]" if at else None)
_fss, query.find_stop = query.find_stop, lambda cn, s: (
    ({"stationName": s}, s) if s == "玉林北站" else (None, s))
_qstr, query.query = query.query, lambda *a, **k: {"error": "没这条"}
assert query._one("玉林", "玉林市", "G02", "玉林北站", "玉林北站要等多久才能等到G02",
                  None, True, {}) == "AT[玉林北站]"
# 前缀不是站名时别硬当站问，照旧反口问他
assert query._one("玉林", "玉林市", "G02", "玉林九中", "玉林九中3路等多久",
                  None, True, {}).endswith("是哪一站等？说个站名我给几分钟")
query.render_at, query.find_stop, query.query = _rss, _fss, _qstr

# 「安宁12」：安宁是昆明下辖县级市，可车来了把它单独开成一个城市（cityId 342「安宁公交」），
# 线名带县名前缀（安宁1 / 安宁12 / 安宁K12）。以前借父城昆明的 081，只查得到昆明市区的线，
# 于是「安宁12」被答成「两家都没收，只有建档资料」——群里那位说「车来了上查的到啊」是对的
assert query.CHELAILE_OWN["安宁"][0] == "342"
_cz = query._merge_cities({"昆明市": "081"}, ["昆明市", "安宁区"])
assert _cz["安宁"]["chelaile"] == "342" and not _cz["安宁"].get("_borrow"), _cz["安宁"]
assert _cz["昆明"]["chelaile"] == "081", _cz["昆明"]
# 明写「安宁区」还是兰州的
assert _cz["安宁区"].get("mygolbs") == "兰州市", _cz["安宁区"]

# 「玉林397号车在哪」：没有线路号、只有车号。以前落到站名那条路返空，模型就编
# 「玉林没有 397 这条线」——可 397 是自编号，真在 17 路上跑
assert query._veh_tokens("玉林397号车在哪") == ["397"]
assert query._veh_tokens("397号车") == ["397"]
assert query._veh_tokens("397号线") == []       # 地铁 397 号线是线号
assert query._veh_tokens("397路") == []
assert query._veh_tokens("G02") == []

# 「石家庄43路7376到哪了」：线号在前、车号在后，车号不能被前一个「路」字挡掉
assert query._veh_tokens("石家庄43路7376到哪了") == ["7376"]
# 同一个号扫完一遍没找到，10 分钟内别重复扫（群里那五条 240/600/920… 就是这么滚出来的）
_mem = ("veh-miss", "玉林", "88888")
query._cache.pop(_mem, None)
_cs16, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
_ch16, query._city_hits = query._city_hits, lambda ck, cn, tok, line="", mk=None: ([], "全城 46 个方向都翻了", "")
_r1 = query.render_veh_city("玉林", "玉林市", "88888", "G02")
_r2 = query.render_veh_city("玉林", "玉林市", "88888", "G02")
assert "没看到 88888" in _r1 and "刚查过一遍" in _r2, (_r1, _r2)
query.cities, query._city_hits = _cs16, _ch16
query._cache.pop(_mem, None)

# 卡片/小程序/图片整段不当公交问句：里面全是 appid、时间、文件路径，
# 一旦进了解析就会当成「432路 + 2026 号车」去查，还顺手扫了全城（群里那五条就是这么来的）
_card = ' [卡片消息] 小程序 {"appid":"wx1234567890","url":"https://x?t=2026&line=432&id=520"}'
assert query.NOT_TEXT.search(_card) and query.answer(_card, "玉林市") == ""
assert query.answer('<img src="file://C:/x/nt_data/Emoji/2026-11/Ori/47d71be6.jpg" />', "玉林市") == ""
assert query.answer("[图片]", "玉林市") == "" and query.answer("[表情][图片]", "玉林市") == ""
# baka86bot 的 B 站卡片：<attachmentType=…>识别：哔哩哔哩，【4K】…杭州公交520路…（2026 / 11 全在里面）
_bili = ('<attachmentType="image/jpeg",attachmentIndex=0,description="x">识别：哔哩哔哩，'
         '【4K】【南浦交社·2026·公交】杭州公交520路（钱江新城总站-武林广场西）')
assert query.NOT_TEXT.search(_bili) and query.answer(_bili, "玉林市") == ""
# 只甩一条链接（分享视频）不是提问：不能顺着上一句的线路回「有，17路 现在 5 辆在路上」
_lk = query._last("")
_keep = dict(_lk)
_lk.update({"city": "玉林", "line": "17"})
assert query.answer("https://www.bilibili.com/video/av117584997391050?p=1", "玉林市") == ""
assert query.answer("https://example.com/a", "玉林市") == ""
# 链接后面真带了问题还是照答
assert query._lines("https://x.cn/a 杭州520路还有车吗") == [("520", "杭州")]
_lk.clear(); _lk.update(_keep)
# 【4K】是清晰度，不是 4K 路（以前会答成「有，4路 现在 5 辆在路上」）
assert query._lines("【4K】双层车") == [] and query.answer("这个视频4K的", "玉林市") == ""
assert query._lines("【1080P】520路") == [("520", "")]

# 「玉林512现在在哪」：本城没有 512 这条线，512 只能是车号（自编号）。
# parse 把「玉林512」认成线路，以前一路查到目录里没有，回「没查到「512」这条线」
_csS, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
_catS, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "17路", "upperOrDown": "1"}, {"lineName": "G02路", "upperOrDown": "1"}]}
_snS, query.render_selfno = query.render_selfno, lambda ck, cn, tok, line="", mk=None: f"[车 {tok}]"
for _m in ("玉林512现在在哪", "玉林512", "玉林512到哪了"):
    assert query.answer(_m, "玉林市") == "[车 512]", (_m, query.answer(_m, "玉林市"))
# 有这条线时照旧走线路；明写「路」也照旧按线路问
assert query.answer("玉林17现在在哪", "玉林市") != "[车 17]"
assert query.answer("玉林512路", "玉林市") != "[车 512]"
query.cities, query._catalog, query.render_selfno = _csS, _catS, _snS

# 特定车辆到特定站：「桂k11878d 什么时候到玉林北站」
# 小写车牌也要认（群里打全小写），不然整句认不出车号，落到换乘
assert query._veh_tokens("桂k11878d 什么时候到玉林北站") == ["桂K11878D"]
assert query._stop_after_dao(" 什么时候到玉林北站") == "玉林北站"
assert query._stop_after_dao(" 到玉林北站还有几站") == "玉林北站"
_seq = [{"stationName": x} for x in ("汽车总站", "市政广场", "文化广场", "玉林北站")]
_seq0, query._station_seq = query._station_seq, lambda cn, ln, d: _seq
_catE, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "G02路", "upperOrDown": "1", "to": "玉林北站", "from": "汽车总站"}]}
_rt = {"routeName": "G02路", "stationName": "玉林北站", "busToStationCount": 2,
       "busToStationTime": 6.0, "busNumber": "桂K11878D"}
def _live_e(cn, ln, d, order=1):
    lst = [{"busNumber": "桂K11878D", "stationName": "市政广场", "statusType": "2"}]
    return {"list": lst, "routeOnStationRTimeInfoList": [_rt] if order > 3 else []}
_lvE, query._live_my = query._live_my, _live_e
_o = query.render_veh_stop("玉林", "玉林市", "桂K11878D", "玉林北站", "G02")
assert "还有 2 站" in _o and "6 分钟" in _o, _o          # 上游给了这辆车单独的预测
assert _o.startswith("桂K11878D"), _o      # 抬头必须是这一台，不能写成「G02路 还有 2 站」（像在说整条线）
# 同一条线上有两台车时，只取问的那一台的预测（别把别台的 1 分钟报成它的）
_rt_other = dict(_rt, busNumber="桂K99999D", busToStationCount=1, busToStationTime=1.0)
query._live_my = lambda cn, ln, d, order=1: {
    "list": [{"busNumber": "桂K11878D", "stationName": "市政广场"}],
    "routeOnStationRTimeInfoList": ([_rt_other, _rt] if order > 3 else [])}
_o3 = query.render_veh_stop("玉林", "玉林市", "桂K11878D", "玉林北站", "G02")
assert "还有 2 站" in _o3 and "6 分钟" in _o3 and "1 分钟" not in _o3, _o3
assert "已经过了" in query.render_veh_stop("玉林", "玉林市", "桂K11878D", "汽车总站", "G02")
assert "不经过" in query.render_veh_stop("玉林", "玉林市", "桂K11878D", "飞机场", "G02")
# 上游没给这辆车的预测时按路段估，别一声不吭
_rt2 = dict(_rt, busNumber="桂K00000D")
query._live_my = lambda cn, ln, d, order=1: {"list": [{"busNumber": "桂K11878D", "stationName": "市政广场"}],
                                             "routeOnStationRTimeInfoList": [_rt2] if order > 3 else [],
                                             "dislist": [{"d": 1000}, {"d": 1000}],
                                             "speedlist": [{"speed": 300}, {"speed": 300}]}
_o2 = query.render_veh_stop("玉林", "玉林市", "桂K11878D", "玉林北站", "G02")
assert "估约" in _o2 and "分钟" in _o2, _o2
query._station_seq, query._catalog, query._live_my = _seq0, _catE, _lvE

# 「那查询527」/「527呢」：这城没有 527 这条线时，527 只能是车号（自编号），得去全城找，
# 不能返空（返空群里一声不吭，模型就自己编「玉林没有这个线路号」）
assert query._num_is_line("玉林", "玉林市", "17") is True     # 玉林真有 17 路
assert query._num_is_line("玉林", "玉林市", "527") is False
_sn0, query.render_selfno = query.render_selfno, (
    lambda ck, cn, tok, line="", mk=None: f"[全城找 {tok}]")
_csR, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
_cfR, query._catalog = query._catalog, lambda cn: {"buslines": [{"lineName": "17路", "upperOrDown": "1"}]}
for _m in ("那查询527", "527呢", "查询527"):
    _o = query.answer(_m, "玉林市")
    assert _o == "[全城找 527]", (_m, _o)
# 有这条线时走线路（不能被车号分支抢走）
assert query.answer("那查询17", "玉林市") != "[全城找 17]"
# 算术题/别的数字不能被拿去全城找车
assert query.answer("9999x 8123等于多少", "玉林市") == ""
query.render_selfno, query.cities, query._catalog = _sn0, _csR, _cfR

# 问一句「武汉地铁12号线」不能把整城标成「掌上没实时」：
# 武汉车来了只收了 22 条通学线，一旦跳过掌上，全城线路都答成「两家实时都没报来车」
_csP, query.cities = query.cities, lambda: {"武汉": {"mygolbs": "武汉市", "chelaile": "11036"}}
_mgP, query._mygolbs = query._mygolbs, lambda cn, w: {
    "source": "掌上公交", "line": "地铁12号线", "groups": [], "note": "", "unknown": True, "pos": {}}
_clP, query._chelaile = query._chelaile, lambda cid, w: None
query.PREFER_CL.pop("武汉", None)
query.query("武汉", ["地铁12号线"])
assert "武汉" not in query.PREFER_CL, query.PREFER_CL        # 地铁不算这座城没实时
# 真公交线（掌上整城 hasReal=0）才记
query._mygolbs = lambda cn, w: {"source": "掌上公交", "line": "8路", "groups": [], "note": "",
                                "unknown": True, "pos": {}}
query.query("武汉", ["8路"])
assert query.PREFER_CL.get("武汉") is True
# 就算标了「先问车来了」，车来了没这条时也必须回落掌上，不能整城查不到
_got = []
def _mg10(cn, w):
    _got.append(w)
    return {"source": "掌上公交", "line": "10路", "pos": {},
            "groups": [("汉口火车站", [("BDCE8BC0", "鹦鹉大道", 120, "2")])], "note": "", "unknown": False}
query._mygolbs = _mg10
_r10 = query.query("武汉", ["10"])
assert _got == ["10"] and any(g for _, g in _r10["groups"]), (query.PREFER_CL, _r10)
query.cities, query._mygolbs, query._chelaile = _csP, _mgP, _clP
query.PREFER_CL.pop("武汉", None)

# 上游回的错不能按一小时缓存：武汉 325 那次目录偏了一下，之后一小时内全城都「目录里没这条」
_bad = {"status": -2, "msg": "当前城市暂无法提供公交查询", "error": True}
query.mygolbs.cache.pop(("t-err", "k"), None)
query.mygolbs.cached("t-err", "k", 3600, lambda: _bad)
_t = query.mygolbs.cache[("t-err", "k")][0]
assert query.time.time() - _t > 3000, query.time.time() - _t          # 只剩 30 秒有效期，不是一小时
assert query.mygolbs.cached("t-err", "k", 3600, lambda: {"status": "1"}) is _bad  # 30 秒内仍复用
query.mygolbs.cache.pop(("t-err", "k"), None)
# 目录里缺这条线时不能直接说「没这条」：再问一次关键词搜索（CMD=114）
_cat0, query._catalog = query._catalog, lambda cn: {"buslines": [], "status": "1", "msg": ""}
_ln0, query.mygolbs.lines = query.mygolbs.lines, (
    lambda cn, kw="": ([{"lineName": "325路", "to": "博学路潮海村", "upperOrDown": "1"}]
                       if kw == "325" else []))
_lv0, query._live_my = query._live_my, (
    lambda cn, ln, d, *a, **k: {"status": "1", "msg": "获取实时数据成功!", "hasReal": "1",
                                "list": [{"busNumber": "8DD2BB52", "stationName": "四新南路",
                                          "statusType": "2", "busToStationNiheDistance": 120}]})
_r = query._mygolbs("武汉市", "325")
assert not _r.get("error") and _r["groups"][0][1], _r
query._catalog, query.mygolbs.lines, query._live_my = _cat0, _ln0, _lv0

# 「去天安门做几路」：做=坐 的手滑，尾巴砍不掉就整句当站名搜 → 「这个站没查到」
_tm = query.TAIL.sub("", query.LEAD.sub("", "去天安门做几路"))
assert _tm == "天安门", _tm
assert query.TAIL.sub("", query.LEAD.sub("", "去天安门坐几路")) == "天安门"
assert query.TAIL.sub("", query.LEAD.sub("", "天安门做几路车")) == "天安门"
assert query.TAIL.sub("", query.LEAD.sub("", "去天安门做哪路车")) == "天安门"

assert not query.NOT_TEXT.search("查看北流城南站方向还有没有")   # 微信 appmsg 要解析出 title，不能整段丢
assert query._veh_tokens("2026 那辆车") == [] and query._veh_tokens("2025年") == []
assert query._veh_tokens("48542557 到哪") == ["48542557"]        # 武汉车号就是长数字
assert query._veh_tokens("8A9E8AA8 到哪") == ["8A9E8AA8"]
# 「是，397号车」这种不带路/线的车号问法得能进快路，不然只剩模型一条路、它会反口问是哪一路的
assert query.BUS_RE.search("是，397号车") and query.BUS_RE.search("397号车")
assert query.BUS_RE.search("397号")
assert not query.BUS_RE.search("3号门怎么走")     # 门/楼/口 不是车
assert not query.BUS_RE.search("2号楼在哪")
assert query._veh_tokens("玉林G02 397号车到哪了") == ["397"]
# 位数就对不上时不做几百个方向的白扫：直接按形状说清（昆明车号都是 5 位）
assert query._id_hint({"chelaile": ["51434", "04387"]}, "4449").startswith("（这城车号是 5 位")
assert query._id_hint({"chelaile": ["51434"]}, "5143") != ""
assert query._id_hint({"chelaile": ["51434"]}, "51434") == ""
assert query._id_hint({"chelaile": ["桂A1"]}, "4449") == ""

# 「530呢」：本线没这辆车时去全城其它线路找，别只丢一句「没跑这辆」
_cs14, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "zd440"}}
_pi_keep2, query._plate_index = query._plate_index, lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({}, 46, 46)
query._cache.pop(("plate-idx", ("玉林市", query.CITY_SCAN_MAX)), None)
query._cache.pop(("id-kinds", "玉林"), None)
_ci14, query._selfno_index = query._selfno_index, lambda cid, cap=query.SELFNO_MAX, start=0, budget=0: (
    {"530": [("26", "人民大北路口", "辛田村路口")], "516": [("G02", "玉林汽车总站", "榕楼社区")]}, 46, 46)
query._cache.pop(("selfno-idx", "zd440"), None)
_vc = query.render_veh_city("玉林", "玉林市", "530", "G02")
assert "26路" in _vc and "辛田村路口" in _vc, _vc
_vc2 = query.render_veh_city("玉林", "玉林市", "516", "G02")   # 同一辆在本线上：这条已经答过，不重复
assert "没看到 516 在路上" in _vc2, _vc2
_vc3 = query.render_veh_city("玉林", "玉林市", "999", "G02")
assert "全城 46 个方向都翻了" in _vc3 and "没看到 999" in _vc3, _vc3
assert query.render_veh_city("玉林", "玉林市", "", "G02") == ""
# 车牌号也能在全城找：掌上那份索引里就有（各城报车牌还是自编号不一样，规则不绑源）
_pi14, query._plate_index = query._plate_index, lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({"K11878D": ("26", "1", "文化广场路口")}, 46, 46)
_cat14, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "26", "from": "人民大北路口", "to": "玉林汽车总站"}]}
query._cache.pop(("plate-idx", ("玉林市", query.CITY_SCAN_MAX)), None)
query._cache.pop(("id-kinds", "玉林"), None)
assert "26路" in query.render_veh_city("玉林", "玉林市", "桂K11878D", "G02路")
query._plate_index, query._catalog = _pi14, _cat14
# 挂在 _one 上：答案里说「没跑这辆」时补一句全城结果
_lq3, query.query = query.query, lambda ck, wants, src=None, veh="": {
    "source": "车来了", "line": "G02", "groups": [("甲", [("516", "A", 1)])], "pos": {}}
_lr3, query.render = query.render, lambda ck, want, res, veh="", d="", shown="": f"G02 这会儿没跑 {veh} 这辆"
_lvc3, query.render_veh_city = query.render_veh_city, lambda ck, cn, v, line="", mk=None: "CITYSCAN"
_o530 = query._one("玉林", "玉林市", "G02", "", "530呢", None, True, {})
_nl = chr(10)
assert _o530 == "G02 这会儿没跑 530 这辆" + _nl + "CITYSCAN", _o530
query.query, query.render, query.render_veh_city = _lq3, _lr3, _lvc3
query.cities, query._selfno_index = _cs14, _ci14
query._plate_index = _pi_keep2
# 「用掌上公交查G02路」：数据源那层壳不能当城市名，否则前缀取成「上公交」
assert query.parse(query.SRCPRE.sub("", "用掌上公交查G02路")) == ("G02", "")
assert query.parse(query.SRCPRE.sub("", "车来了查石家庄41路")) == ("41", "石家庄")
# 「车来了，看一下 G02…」：数据源后面跟逗号再跟动作词，一样得剥干净
assert query.parse(query.SRCPRE.sub("", "车来了，看一下 G02 现在上线的有几台车")) == ("G02", "")
assert query.parse(query.SRCPRE.sub("", "车来了查询玉林职业技术学院有没有公交车")) is None
# 带线路号的闲聊（「11路那车真挤」）不该被快路抢答，纯报号照答
assert not query.looks_like_ask("今天坐9路回家累死了")
assert not query.looks_like_ask("11路那车真挤")
# 单字母+数字被 ALPHA_PAT 当线号（「用s5注册（」），整句没路/线/站/车也没城市名 → 闲聊；
# 裸号问法靠城市名放行
assert not query.looks_like_ask("用s5注册（")
assert not query.looks_like_ask("这台s5充电")
assert not query.looks_like_ask("2F有车吗")    # 短代号 + 问法词，没写路/线/号也没城市名
assert query.looks_like_ask("s5路")          # 带「路」不算闲聊
# 城市名认得出时裸号照放行（这里前面桩过城市表，detect_city 得自己给）
_dc, query.detect_city = query.detect_city, lambda m, p, d: ("深圳", True)
assert query.looks_like_ask("深圳M470")
query.detect_city = _dc
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
# 站名在线路号前面：「我在玉林火车站等9路」「玉林火车站9路还有几站」得认出站，否则落到整条线清单上
for _m in ("我在玉林火车站等9路", "玉林火车站9路还有几站", "在玉林火车站等9路呢"):
    assert "玉林火车站" in list(query._cands("玉林市", _m)), _m
# 「g02离玉林北站最近的公车要多久」：站名在号后面但隔个「离」，别退化成整条线清单
assert "玉林北站" in list(query._cands("玉林市", "g02离玉林北站最近点的公车要多久"))
# 「在市政广场要等多久等到17路」：站名和号中间夹着问法，站名不能被「等到」切没
assert query.parse("在市政广场要等多久等到17路") == ("17", "市政广场")
assert "市政广场" in list(query._cands("玉林市", "在市政广场要等多久等到17路"))
# 群里贴的脚本不是公交问题：`x0 = iroot(n, d)` 里的 x0 被当线路号，回过「没查到「X0」这条线」
_CODE = "import math\ndef iroot(n, k):\n    x0 = k\n    return x0\n"
assert query.CODE_RE.search(_CODE) and not query.looks_like_ask(_CODE)
assert query.answer(_CODE, "南宁市", key="code") == ""
assert query.looks_like_ask("玉林9路站点") and not query.CODE_RE.search("玉林9路站点")
# 上游刚挂过也不能让闲聊收到「上游没响应」——那句是「彻底变成问公交工具」的由来
_uf = query._up_fail[0]
query._up_fail[0] = query.time.time()
assert query.answer("这大肥鱼彻底变成问公交工具了", "玉林市") == ""
assert query.answer("晚上吃点啥呢", "玉林市") == ""
query._up_fail[0] = _uf
# 「车来了查X校区的线路」：数据源和动词不能留在站名里；点名车来了就扫车来了的站表
assert list(query._cands("玉林市", "车来了查广西医科大玉林校区的线路")) == ["广西医科大玉林校区"]
# 「那你去查南宁朝阳广场的公交线路」：LEAD 只剥开头，「那」「你」剥完才露出「去查」；
# 裸「查」也得剥（「查桥站」这种真站名不剥：后面不到 4 个字）
assert "南宁朝阳广场" in list(query._cands("南宁市", "那你去查南宁朝阳广场的公交线路"))
assert list(query._cands("无锡市", "查桥站有哪些公交")) == ["查桥站"]
# 「玉林北站有哪几路车」：城市名剥掉只剩「北站」搜不到，得再拿原样那句试一次
_st2 = query.stops
query.stops = lambda cn, kw: [{"stationName": "玉林北站", "relatedRoutes": ["G01路"]}] if kw == "玉林北站" else []
assert query.pick_stop("玉林市", "玉林北站有哪几路车") == "玉林北站"
query.stops = _st2
# 「玉林汽车总站去税局咋去」：疑问词在最后，HOP2 只认「A咋去B」这一种语序
assert query.hop("玉林汽车总站去税局咋去") == ("玉林汽车总站", "税局")
assert query.hop("汽车总站去玉林北站咋坐") == ("汽车总站", "玉林北站")
# 「税局」：上游站名搜索要 3 个字以上，两字简称退到单字再按顺序匹（「税局」⊂「市税务局」），
# 返回的得是站牌真名，上层拿它去对线路站序；全城匹到不止一个站就不认
_st3 = query.stops
query.stops = lambda cn, kw: (
    [{"stationName": "市税务局", "relatedRoutes": ["9路"]},
     {"stationName": "市人社局", "relatedRoutes": ["3路"]}] if kw == "税" else [])
assert query.find_stop("玉林市", "税局") == (query._merge_same(
    [{"stationName": "市税务局", "relatedRoutes": ["9路"]}],
    {"stationName": "市税务局", "relatedRoutes": ["9路"]}), "市税务局")
assert query.find_stop("玉林市", "车站") == (None, "车站")     # 整个词都是通用字，不认
query.stops = _st3
# 「北京密101路」：剥掉线号只剩个「密」，上游子串搜会匹上「北京密码小区」，答一句「不经过」
_st4 = query.stops
query.stops = lambda cn, kw: [{"stationName": "北京密码小区", "relatedRoutes": ["密101路"]}]
assert query.pick_stop("北京市", "北京密101路") is None
query.stops = _st4
# 「AirPods5和pro3选哪个比较好」：QQ 的 @ 前缀里 CA0308D 让前导零线号命中进快路，
# 抹掉 @ 之后「pro3」里的 3 又被当成 3 路——数字打头的线名不许粘在英文字母后面
assert not query.BUS_RE.search(query.AT_RE.sub(" ", "<@CA0308D02E977A8606EF36B77DB31115> AirPods5和pro3选哪个比较好"))
assert query._msg_line("玉林市", "AirPods5和pro3选哪个比较好", "玉林") is None
assert query._in_line("AirPods5和pro3", "3") is False
assert query._in_line("玉林9路", "9") is True
# 「上海16号线」：目录里叫「地铁16号线」，城市名占了「地铁」的位子，两边都不成立就落到没查到
_cat4, _cit4 = query._catalog, query.cities
query.cities = lambda: {}
query._catalog = lambda c: {"buslines": [{"lineName": "地铁16号线"}, {"lineName": "161路"}]}
assert query._exact_line("上海市", "上海16号线", "上海16号") == "地铁16号线"
assert query._exact_line("上海市", "上海161路", "161") == "161路"
query._catalog, query.cities = _cat4, _cit4
# 「微信ZFB收眯 一天8OO0」：广告拿 O 冒 0，8OO0 被 ALPHA_PAT 当线号，整句没问法没闲聊词就兜底答站
assert not query.looks_like_ask("微信ZFB收眯 一天8OO0")
assert not query.looks_like_ask("收8OO0")
assert query.looks_like_ask("G02现在有车吗")            # 字母开头的线号不受影响
assert query.looks_like_ask("佛山614A有车吗")           # 有城市名+问法的照旧
# 「哦，最早是玉林那个 h5 起步的啊」：h5 是网页版本，不是线号。目录里小写线名都是两位数字
# （南宁 k10路、昆明 Cj10路），一位数字的一个都没有
assert query._chatty_code("哦，最早是玉林那个 h5 起步的啊。")
assert query._chatty_code("最初开始研究这个是拿的玉林的实时公交那个 h5")
assert not query._chatty_code("s5路")            # 写了「路」就不是口语
assert not query._chatty_code("南宁k10")          # 两位数字的小写线名照常认
assert not query.looks_like_ask("哦，最早是玉林那个 h5 起步的啊。")
assert not query.looks_like_ask("@大肥鱼 最初开始研究这个是拿的玉林的实时公交那个 h5")
assert query.answer("哦，最早是玉林那个 h5 起步的啊。", "玉林市") == ""   # 别回「没查到「玉林那个H5」」
assert query.looks_like_ask("南宁k10")
# 「c2c出呗」：C2C/B2B 这类一两位字母夹一两位数字的缩写不是线号（真线号 C621A 是三位数、
# B1K 要带「路/线/号」或问法），整句只剩「出呗」这种话时别答成「没查到「C2C」这条线，玉林这边…」
assert not query.looks_like_ask("c2c出呗")
assert not query.looks_like_ask("C2C")
assert query.looks_like_ask("c2c线")                  # 带「线」才是线号写法
assert query.looks_like_ask("c2c出呗，2路有车吗")      # 同句里有真问法照旧答
assert query.looks_like_ask("K901A呢")                # 三位数字的真线号不受影响
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
_cc = mygolbs.post
mygolbs.post = lambda p, timeout=8: {"buslines": [
    {"routeBean": {"routeName": "1路", "company": "甲公交", "companyMobile": ""}},
    {"routeBean": {"routeName": "2路", "company": "乙巴士", "companyMobile": "乙巴士:020-12345678"}}]}
_x = query.render_city_company("某市")
assert "2 家公交公司" in _x and "乙巴士 020-12345678" in _x and "甲公交" in _x, _x
_y = query.render_city_company("某市", "某市乙巴士电话")     # 点了名只报这家
assert "1 家" in _y and _y.count("020-12345678") == 1, _y
assert query.render_city_company(None) is None
assert not query.CITY_CO_RE.search("我们公司今年业绩咋样")      # 闲聊不能被公司查询抢走
assert query.CITY_CO_RE.search("玉林公交公司电话")
mygolbs.post = _cc

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
_rh, query.render_hop = query.render_hop, lambda ck, nm, got, **kw: "北流105路 直达（18 站）"
_cit3, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}, "北流": {"mygolbs": "北流市"}}
assert query.answer("从北流汽车站到玉林北站坐啥公交车", "玉林市") == "北流105路 直达（18 站）"
query.render_hop = lambda ck, nm, got, **kw: ""      # 目录里查不到（真跨城）才走跨城那句
assert "跨城" in query.answer("从北流汽车站到玉林北站坐啥公交车", "玉林市")
query.render_hop, query.cities = _rh, _cit3

# 「从机场一号航站楼到元氏火车站」没写城市：默认城查不到就沿用上一条问过的城
_rh2, _st7, _cit4 = query.render_hop, query.stops, query.cities
query.render_hop = lambda ck, nm, got, **kw: "" if ck == "玉林" else "石家庄 换乘"
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
# 「玉高附中等多久才能等到7路」：口语叫法「玉高附中」得换成站牌上的「玉林高中初中部」，
# 换不出来时也不许说「这座城暂时没有公交数据」——那是站名，不是城市
assert query.STOP_ALIAS["玉高附中"] == "玉林高中初中部"
_stops = query.stops
query.stops = lambda c, w: ([{"stationName": "玉林高中初中部", "relatedRoutes": ["7路"]}]
                            if "初中部" in w else [])
assert query.find_stop("玉林市", "玉高附中")[0]["stationName"] == "玉林高中初中部"
query.stops = _stops
# 「玉林九中的3路等多久」：「的」粘在站名尾巴上，站表里搜「玉林九中的」搜不到
assert "玉林九中" in list(query._cands("玉林市", "玉林九中的3路等多久"))
assert "玉林火车站" in list(query._cands("玉林市", "玉林火车站的9路等多久"))
# 站在站台问等多久、站名又没认出来：反问哪一站，别退化成整车清单
# 全国线名形态（579 城 74696 条目录扫出来的少数派）：照抄站牌上的线名必须认得出
assert query.parse("DK401路") == ("DK401", "")          # 双字母前缀
assert query.parse("601B2路") == ("601B2", "")          # 号中间夹字母
assert query.parse("27-1路") == ("27-1", "")            # 数字-数字
assert query.parse("1005-1号线") == ("1005-1", "")      # 数字-数字 + 号线
assert query.parse("BRT1号线") == ("BRT1号", "")        # 乌鲁木齐/厦门 BRT
assert query.parse("地铁快线R101路") == ("R101", "地铁快线")   # 东莞：不是「地铁快线」这条线
assert query.parse("hy4") is None and query.parse("9KB") is None and query.parse("MP3") is None
assert query._pick([("2路A", {})], "2") == [("2路A", {})]                 # 路+字母
assert query._pick([("东山2路", {}), ("东山12路", {})], "3") == []
assert query._pick([("东山2路", {}), ("东山12路", {})], "2") == [("东山2路", {})]  # 县名写在线名里
assert query._pick([("云海专线B（T105B/定制巴士1893号）", {})], "T105B")   # 括号里的别名号
_cat0 = query._catalog
query._catalog = lambda c: {"buslines": [{"lineName": "张堰一路"}, {"lineName": "②号线"}]}
assert query._exact_line("上海市", "张堰一路", "张堰1") == "张堰一路"      # 中文数字 vs 阿拉伯
assert query._exact_line("上海市", "②号线", "2号") in (None, "②号线")     # 「②」不许崩
query._catalog = _cat0
assert query.TIMEQ_RE.search("3路等多久") and not query.INTERVAL_RE.search("3路等多久")
assert not query.TIMEQ_RE.search("9路多久一班") or query.INTERVAL_RE.search("9路多久一班")
assert "这座城暂时没有公交数据" not in str(query.answer("玉高附中等多久才能等到7路", "玉林市"))
assert query.find_stop("玉林市", "玉林火车站")[0]["stationName"].startswith("玉林火车站")
# 「G02路还差多久到玉林汽车站」：站牌上叫「汽车总站」，掌上搜「汽车站」一个字都搜不到
assert "玉林汽车总站" in list(query._cands("玉林市", "G02路还差多久到玉林汽车站"))# 「玉林北站要等几分钟才能等到 g02」：问法里的「要等」「往」粘在站名上，站名就认不出
assert "玉林北站" in list(query._cands("玉林市", "玉林北站要等几分钟才能等到 g02"))
assert "玉林北站" in list(query._cands("玉林市", "往玉林北站还要等多少分钟才能等到 g02"))
# 「G02在玉林汽车总站要等多久才能等到」：号在前、站夹中间，尾字「到」把最后一片切成空
assert "玉林汽车总站" in list(query._cands("玉林市", "G02在玉林汽车总站要等多久才能等到"))
assert "汽车总站" in list(query._cands("玉林市", "G02在汽车总站要等多久"))
# 「G02的512到玉林汽车总站要等多久」：问的是这条线上自编号 512 那一辆，不是线号 512
assert query.VEH2_RE.search("G02的512到玉林汽车总站要等多久").group(1) == "512"
assert query.VEH2_RE.search("玉林到南宁的506路") is None
# 「威远3路副线」「云和1路上城门」：比裸号长的真线名整段在句子里，不能被裸号那条顶掉
assert query._in_line("威远3副现在有车吗", "威远3副")
assert not query._in_line("玉林99路", "9")      # 「99」里的 9 不是 9 路
# 「桂 K12682D 是哪个字编号呢」：车牌中间打空格、少打个「自」都得认出来，不然整句落到模型手里编
assert query.PLATE_RE.search("桂 K12682D 是哪个字编号呢").group(1).replace(" ", "") == "桂K12682D"
assert query.BUS_RE.search("桂 K12682D 是哪个字编号呢")
assert query.SELFNO_RE.search("桂 K12682D 是哪个字编号呢")
# 「用巴士派迪查D276」：第四种数据源（别名「派迪」），点了名就别走实时，前缀也别当城市
assert query.SRC_RE.search("用巴士派迪查南宁D276").group("bp")
assert query.SRC_RE.search("派迪查G02").group("bp")
assert query.SRCPRE.sub("", "用巴士派迪查南宁D276").strip() == "南宁D276"
# 「lck6116evga2 在玉林配属哪条线路」：车型代号不是线号（尾数 2 会匹成 2 路），进型号那条分支
assert query.MODEL_RE.search("lck6116evga2 在玉林配属哪条公交线路").group(1).upper() == "LCK6116EVGA2"
assert query.MODEL_Q.search("lck6116evga2 在玉林配属哪条公交线路")
assert query.MODEL_RE.search("查南宁D276") is None          # D276 是线号，不是型号
# 「不是，他查的是公交公司门口的公交站」：跟群里别人解释，不是问机器人
assert not query.looks_like_ask("不是，他查的是公交公司门口的公交站")
assert query.answer("不是，他查的是公交公司门口的公交站", "玉林市") == ""
assert query.looks_like_ask("不是，G02现在有车吗")          # 带线号照旧答
assert query.looks_like_ask("其他城市9路有吗")               # 「其他」里的他不是第三人称
# 「地铁快巴4」是南宁的微循环公交，不是地铁：别把收班说成「地铁不上报车机」
assert query.STOPS_RE.search("南宁地铁快巴4站表")
assert not re.match(r"^(?:地铁|轨道交通)\d+号", query.norm_line("地铁快巴4路"))
assert re.match(r"^(?:地铁|轨道交通)\d+号", query.norm_line("轨道交通1号线"))
# 「东莞707配车」：配车是建档里的车型，得问巴士派迪，不能被当成实时问（原来 BUS_RE 不认，整句交给模型）
assert query.BUS_RE.search("东莞707配车")
assert query.BUS_RE.search("707路配了几辆车")
assert query.FLEET_RE.search("东莞707配车")
# 「西安616」：城市名跟裸号，不带「路」——BUS_RE 不认，快路不触发，整句交给模型就没回
assert query.CITYNUM_RE.search("西安616")
assert query.CITYNUM_RE.search("@御坂09714号 西安616")

# 「新郑站有什么线路」：裸县名（不带「市」）也得翻到父城，不然落回默认城答「玉林站表里没查到」
_cv = query.cities
query.cities = lambda: {**_cv(), "郑州": {"mygolbs": "郑州市", "chelaile": "010"}}
assert query.detect_city("新郑站有什么线路", "", "玉林市") == ("郑州", True)
query.cities = _cv

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
# 「等到9路」的「9路」是线路号，别当目的地：dest 一有值，方向会被 _goes 全过滤掉
assert query._dest_after("玉林市", "玉林汽车总站等多久才能等到9路", "汽车总站") is None
# 「玉林汽车总站等多久才能等到9路」：站经停这条线，可两个方向连 tips 都没有（got 空）。
# 返 None 会落到整条线的清单（「9路 现在 2 辆在路上」），站在站台的人读这个没用
_lm5, _sq5, _ct5, _ps5 = query._live_my, query._station_seq, query._catalog, query.pick_stop
query._live_my = lambda cn, n, d, order=1: {"routeOnStationRTimeInfoList": []}
query._station_seq = lambda c, l, d: [{"stationName": "汽车总站"}, {"stationName": "乙站"}]
query._catalog = lambda c: {"buslines": [{"lineName": "9路", "upperOrDown": d, "to": t}
                                        for d, t in (("1", "甲"), ("2", "乙"))]}
query.pick_stop = lambda c, m: "汽车总站"
_da5, _go5 = query._dest_after, query._goes
query._dest_after = lambda c, m, s: "别处"        # 「G02 到高新产业园，去玉林北站」那种 dest
query._goes = lambda c, l, s, d: None             # 两个方向都开不到 dest
_g5 = query.render_at("玉林", "玉林市", "9", "玉林汽车总站等多久才能等到9路")
assert _g5 and "没有开往别处的预测" in _g5, _g5
query._live_my, query._station_seq, query._catalog, query.pick_stop = _lm5, _sq5, _ct5, _ps5
query._dest_after, query._goes = _da5, _go5

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
# 「这个站还在跑的线路有几条」：条数得写在头一句，不然人得自己去数哪行有车
assert _s4.startswith("玉林市这 2 条线这会儿有 1 条在跑："), _s4
assert query._msg_line("玉林市", "那玉林北站定制夜班车呢？") == "玉林北站定制夜班车"
assert query._msg_line("玉林市", "玉林北站有哪几条公交线？") is None
# 「现在有玉林北站夜班车吗」：少说了「定制」，且「现在」在句首——以前被 TAIL 吃空，回成反问
assert query._msg_line("玉林市", "现在有玉林北站夜班车吗？") == "玉林北站定制夜班车"
query._catalog, query._live_my = _cat4, _lm4

# BRT 的叫法：「北京BRT4」不能落成通州的「T4」（那是另一条线），目录里叫「快速公交4线」。
# 前缀粘着字母的线名（T4）也不该在「BRT4」里被认出来
# 「武汉鄂101路」：目录里 BRT1路 的「BRT」被当成「地铁/公交」那类前缀剥掉，剥完剩「1」，
# 而「1」又出现在 101 里面 → 把 BRT1 当成用户问的线，答成「BRT1路 有 6 辆」。
# 剥完得是完整的一个号（两侧不粘数字）才算
assert not query._in_line("武汉鄂101路", "1")
assert query._in_line("上海16号", "16号")
assert query._exact_line("上海市", "上海16号线", "16号", "上海") == "地铁16号线"
_cat9, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("BRT1路", "101路-鄂州华洪公交")]}
assert query._exact_line("武汉市", "武汉鄂101路", "101", "武汉") is None
assert [n for n, _ in query._pick([(r["lineName"], r) for r in query._catalog("x")["buslines"]],
                                  "101")] == ["101路-鄂州华洪公交"]
# 「武汉913」：同一处裸包含，目录里 BRT1 剥成「1」正好在 913 里 → 答成「BRT1路 有 6 辆」，
# 车是雄楚大道 BRT 那批，站名全对不上。913 里没有独立的「1」，不能匹
query._catalog = lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"} for n in ("913路", "1路", "BRT1路")]}
assert query._exact_line("武汉市", "武汉913", "913") == "913路"
assert query._exact_line("武汉市", "武汉1路", "1") == "1路"          # 别被更长的 BRT1 顶掉
assert query._exact_line("武汉市", "武汉BRT1", "BRT1") == "BRT1路"   # 用户自己写了 BRT 就认
query._catalog = _cat9
# 「惠州博罗233路」：博罗是惠州下辖县，不是站名。以前拿「博罗」搜站命中「博罗天虹」，
# 回一句「233路 不经过博罗天虹」——惠州 233 路跑的是惠城—惠阳，跟博罗没关系
_cit8, query.cities = query.cities, lambda: {"惠州": {"mygolbs": "惠州市"}}
assert query._place_head("惠州博罗233路") and query._place_head("博罗233路")
assert query._place_head("安宁区8路")
assert not query._place_head("玉林火车站9路")     # 真站名照样当站问
assert not query._place_head("玉林9路")
_p11, query.pick_stop = query.pick_stop, lambda cn, m: "博罗天虹"   # 上游只有名字相近的站
assert query.render_at("惠州", "惠州市", "233", "惠州博罗233路") is None
assert query.render_at("惠州", "惠州市", "233", "我在博罗天虹等233路")
query.pick_stop = _p11
query.cities = _cit8
_cat7, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("233路", "博罗1路", "博罗128路", "博罗222路")]}
# 县名自成一套时别拿同城裸号顶：「博罗233」不是「233路」
assert [n for n, _ in query._pick([(r["lineName"], r) for r in query._catalog("x")["buslines"]],
                                  "博罗233")] == []
assert [n for n, _ in query._pick([(r["lineName"], r) for r in query._catalog("x")["buslines"]],
                                  "博罗128")] == ["博罗128路"]
query._catalog = _cat7
# 目录把一个名字写两条线（连云港「B1路/B1路夜」、郑州「888路/郑州-中牟」）：问 B1 要命中它，
# 不能被同城的 B11/B12K/B1K 那堆前缀候选顶成「没这条线」
assert query._slash_parts("B1路/B1路夜") == ["B1", "B1夜"]
assert query._slash_parts("888路/郑州-中牟") == ["888", "郑州-中牟"]
_cat6, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("B1路/B1路夜", "B11路", "B12K线", "B13路", "B1K线")]}
_pk = query._pick([(r["lineName"], r) for r in query._catalog("x")["buslines"]], "B1")
assert [n for n, _ in _pk] == ["B1路/B1路夜"], _pk
assert [n for n, _ in query._pick([(r["lineName"], r) for r in query._catalog("x")["buslines"]], "B11")] == ["B11路"]
_r6 = query.render("连云港", "B1", {"source": "掌上公交", "line": "B1路/B1路夜",
    "groups": [("在海一方公园", [("苏GA9356", "光明小区", 61, "")])], "note": ""})
render_pick = _r6.startswith("有，B1路 现在 1 辆在路上"), _r6
query._catalog = _cat6
assert render_pick[0], render_pick[1]
# 「河池8路」：车来了把县市写进线名（宜州8 / 金城江8），掌上是裸号 8路。
# 不能在裸号匹配时挑 shortest 撞上邻县的宜州8——那是另一条线，车也不能并进来
assert query._tail_line("金城江8", "8") and query._tail_line("宜州8", "8")
assert not query._tail_line("金城江18", "8")      # 1 和 8 挨着，读作 18
assert query._tail_line("永嘉51", "51") and not query._tail_line("永嘉151", "51")
class _FakeCL3:
    def lines(self, cid):
        return [("宜州8", "y1"), ("宜州8", "y2"), ("金城江8", "j1"), ("金城江18", "k1")]
    def live(self, cid, lid):
        return {"state": 0, "desc": "", "to": "X", "buses": [("宜1", "S", 10)], "pos": {}}
_cl11, _cit11, _cat11 = query.chelaile, query.cities, query._catalog
query.chelaile = _FakeCL3()
query.cities = lambda: {"河池": {"mygolbs": "河池市", "chelaile": "307"}}
query._catalog = lambda cn: {"buslines": [
    {"lineName": "8路", "upperOrDown": "1", "from": "A", "to": "B"},
    {"lineName": "15路", "upperOrDown": "1", "from": "A", "to": "B"}]}
assert query._chelaile("307", "8") is None       # 本城有裸号 8路，宜州8 是另一条
assert query._chelaile("307", "金城江8") is not None
_cat12 = query._catalog
query._catalog = lambda cn: {"buslines": [
    {"lineName": "永嘉51", "upperOrDown": "1", "from": "A", "to": "B"}]}
assert query._chelaile("307", "51") is None      # 目录里没有裸号 51
query._catalog = _cat12
# 实时取不到时只用目录回一份：抬头按用户问的号，说明是「没取到」而不是「没车」
_st = {"source": "掌上公交", "line": "8路", "groups": [("御景龙庭", [])],
       "note": "06:05 起、末班 22:30、32 站", "stale": True}
_t11 = query.render("河池", "8", _st)
assert _t11.startswith("8路 的实时车这会儿没取到") and "这会儿没车" not in _t11, _t11
query.chelaile, query.cities, query._catalog = _cl11, _cit11, _cat11
# 「河池606路」：号是对的，目录里叫「宜州606路（快班）」/「（普班）」。回「没查到 606 这条线」
# 是假话——明明有两条。按号挑出来让用户选；只有一条就直接答（河池603 → 宜州603路）
_cat14, _cit14 = query._catalog, query.cities
query.cities = lambda: {"河池": {"mygolbs": "河池市"}}
query._catalog = lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("宜州606路（快班）", "宜州606路（普班）", "宜州603路", "金城江18", "金城江8")]}
assert [n for n in query._number_tails("河池", "606")] == ["宜州606路（快班）", "宜州606路（普班）"]
assert query._number_tails("河池", "603") == ["宜州603路"]
assert query._number_tails("河池", "8") == ["金城江8"]        # 金城江18 不算 8
assert query._number_tails("河池", "18") == ["金城江18"]
assert query._number_tails("河池", "607") == []
_t14 = query.render("河池", "606", {"error": "没这条线路"})
assert "606路 有 2 条" in _t14 and "快班" in _t14 and "普班" in _t14, _t14
query._catalog, query.cities = _cat14, _cit14
# 线名自己带括号说明时别再套一层：「宜州606路（快班）」不要写成「606快（宜州606路（快班））」
assert query._ask_name("606快", "宜州606路（快班）") == "宜州606路（快班）"
# 快慢班：掌上把定性词写进括号「宜州606路（快班）」，车来了写尾巴「宜州606路快线」，
# 用户写「宜州606快线」。以前括号连同「快班」一起剥掉，快慢两条线都变成「宜州606」，
# 谁都不等，转去车来了又只有 -5，于是答成「查不到实时车」——可掌上明明有 3 辆车
assert query.norm_line("宜州606路（快班）") == "宜州606快"
assert query.norm_line("宜州606路快线") == query.norm_line("宜州606快线") == "宜州606快"
assert query.norm_line("宜州606路（普班）") == query.norm_line("宜州606路普线") == "宜州606普"
assert query.norm_line("宜州606路（快班）") != query.norm_line("宜州606路（普班）")
assert query.norm_line("地铁快线R101路") == query.norm_line("地铁快R101路")
assert query.norm_line("6路夜班") == "6夜班"          # 夜班/夜线不受影响
assert query.parse("宜州606快线") == ("606快", "宜州")
# 「河池K2路」：车来了叫「金城江K2路」（金城江=河池市区），是同一趟车，
# 抬头要写用户问的号，别直接甩「金城江K2路」——群里看着像答了另一条线
assert query._ask_name("K2", "金城江K2路") == "K2路（金城江K2路）"
assert query._ask_name("15", "金城江15") == "15路（金城江15）"
# 名字对不上时靠终点站认亲：金城江8 就是 8 路（终点 御景龙庭/公共交通公司 一致），
# 宜州8 不是（终点 东门商场/新火车站）
assert query._same_terminals(["御景龙庭（六圩中学）"], {"御景龙庭（六圩中学）", "公共交通公司"})
assert query._same_terminals(["碧桂园（金城江）"], {"高铁站", "碧桂园"})
assert not query._same_terminals(["东门商场", "新火车站（宜州）"], {"御景龙庭（六圩中学）", "公共交通公司"})
class _FakeCL4:
    LINES = [("宜州8", "y1"), ("宜州8", "y2"), ("金城江8", "j1"), ("金城江8", "j2")]
    TO = {"y1": "东门商场", "y2": "新火车站（宜州）",
          "j1": "御景龙庭（六圩中学）", "j2": "公共交通公司"}
    def lines(self, cid):
        return list(self.LINES)
    def live(self, cid, lid):
        return {"state": 0, "desc": "", "to": self.TO[lid], "buses": [("x", "S", 1)], "pos": {}}
_cl12, _cit12, _cat13 = query.chelaile, query.cities, query._catalog
query.chelaile = _FakeCL4()
query.cities = lambda: {"河池": {"mygolbs": "河池市", "chelaile": "309"}}
query._catalog = lambda cn: {"buslines": [
    {"lineName": "8路", "upperOrDown": "1", "from": "公共交通公司", "to": "御景龙庭（六圩中学）"},
    {"lineName": "8路", "upperOrDown": "2", "from": "御景龙庭（六圩中学）", "to": "公共交通公司"}]}
_r12 = query._chelaile("309", "8")
assert _r12 and _r12["line"] == "金城江8", _r12      # 认到同一趟车，不是宜州8
query.cities = lambda: {"河池": {"mygolbs": "河池市", "chelaile": "311"}}
query._catalog = lambda cn: {"buslines": [
    {"lineName": "8路", "upperOrDown": "1", "from": "A", "to": "完全不同的终点"}]}
assert query._chelaile("311", "8") is None          # 终点全不对：不认
query.chelaile, query.cities, query._catalog = _cl12, _cit12, _cat13
# 「河池15路」：目录里叫「金城江15」（金城江是河池市区）。抬头不能直接甩目录名，
# 也不能给公司前缀加括号（双誉公交C61 是同一条线的运营方，括起来只是噪音）
assert query._ask_name("15", "金城江15") == "15路（金城江15）"
assert query._ask_name("C61", "双誉公交C61") == "双誉公交C61"
assert query._ask_name("9", "9路") == "9路"
assert query._ask_name("BRT4", "快速公交4") == "快速公交4"
# 两家都没车时两家的话都得留着；但「临时停运」和「线路还在跑」放一起自相矛盾，留停运那句
_mb = query._merge_blank(
    {"source": "掌上公交", "line": "15路", "groups": [("A", [])], "note": "线路还在跑，只是车机没报位置"},
    [{"source": "车来了", "line": "金城江15", "groups": [("B", [])],
      "note": "车来了那边这条线标的是「暂无车辆时间」（停运状态），这条线 30 站"}])
assert "停运" in _mb["note"] and "线路还在跑" not in _mb["note"], _mb["note"]
_mb2 = query._merge_blank(
    {"source": "掌上公交", "line": "9路", "groups": [("A", [])], "note": "线路还在跑，只是车机没报位置"},
    [{"source": "车来了", "line": "9", "groups": [("B", [])], "desc": "本线路末班时间已过"}])
assert "末班时间已过" in _mb2["note"] and "还在跑" in _mb2["note"], _mb2["note"]
# 车来了 state=-2（临时停运/暂无车辆时间）：desc 原文甩给群友等于没说
class _FakeCL2:
    def lines(self, cid):
        return [("金城江15", "p1"), ("金城江15", "p2")]
    def live(self, cid, lid):
        return {"state": -2, "desc": "暂无车辆时间", "to": "凌霄村委", "buses": []}
    def stations(self, cid, lid):
        return ["s"] * 30
_cl10, _cit10 = query.chelaile, query.cities
_bl10, query._bare_line_exists = query._bare_line_exists, lambda cid, want: False
query.chelaile = _FakeCL2()
_r11 = query._chelaile("308", "15")
query.chelaile, query.cities, query._bare_line_exists = _cl10, _cit10, _bl10
assert _r11.get("paused") and "停运状态" in (_r11.get("note") or ""), _r11.get("note")
assert "暂无车辆时间" in _r11["note"] and "30 站" in _r11["note"], _r11["note"]
# 「快速公交1线」的「公交」是线名的一部分（厦门叫「快1路」）：剥掉就剩「快速」，
# 拼成「快速1」查不到，最后落到同城的 1 路——那是另一条线
assert query._is_place("博罗") and query._is_place("博罗县") and query._is_place("安宁")
assert not query._is_place("快速") and not query._is_place("快速公交")
assert query.brt_alias("快速公交1") == ["快1", "BRT1号", "BRT1"]
# 车来了 state=-5：北京全城都不给实时（别的线也是 -5），昆明只有个别线没接（别的一问就有车）。
# 以前一律说「上游不给昆明的实时车辆位置」，可同城 108 路明明有车
class _FakeCL:
    def __init__(self, others_have_cars):
        self.others = others_have_cars
    def lines(self, cid):
        return [("双誉公交C61", "a9"), ("108", "b9"), ("52", "c9")]
    def live(self, cid, lid):
        if lid == "a9":      # 目标线：-5，只给首末班
            # 首末班覆盖全天：运营窗口内才轮到「没给实时」那句话（窗口外先说收班）
            return {"state": -5, "desc": "首班 00:00 末班 23:59", "to": "团结镇", "buses": []}
        if self.others:
            return {"state": 0, "desc": "", "to": "X", "buses": [("8100", "S", 10)], "pos": {}}
        return {"state": -5, "desc": "首班 05:00 末班 22:40", "to": "Y", "buses": []}
    def stations(self, cid, lid):
        return ["s"] * 22
_cl9, _cit9 = query.chelaile, query.cities
query.chelaile = _FakeCL(True)
query.cities = lambda: {"昆明": {"chelaile": "CITY-LINE"}}
_r9 = query._chelaile("CITY-LINE", "C61")
assert "别的线有" in (_r9.get("note") or ""), _r9.get("note")
assert "上游不给昆明" not in (_r9.get("note") or ""), _r9.get("note")
query.chelaile = _FakeCL(False)
query.cities = lambda: {"北京": {"chelaile": "CITY-WIDE"}}
_r10 = query._chelaile("CITY-WIDE", "C61")
assert "上游不给北京的实时车辆位置" in (_r10.get("note") or ""), _r10.get("note")
query.chelaile, query.cities = _cl9, _cit9
# 「北京BRT4」得进快路：BUS_RE 不认它的时候整个由模型决定要不要调工具，群里就是「查不出来」
assert query.BUS_RE.search("北京BRT4") and query.BUS_RE.search("厦门BRT1")
assert query.BUS_RE.search("北京快速公交4线") and query.BUS_RE.search("乌鲁木齐BRT2")
assert not query.BUS_RE.search("今天吃什么")
assert query.parse("北京BRT4") == ("BRT4", "北京")
assert query.parse("北京BRT4号线") == ("BRT4号", "北京")
assert query.brt_alias("BRT4") == ["快速公交4", "快4", "BRT4号"]
assert query.brt_alias("快速公交4") == ["快4", "BRT4号", "BRT4"]
assert query.brt_alias("9路") == [] and query.brt_alias("G02") == []
_cat5, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": n, "upperOrDown": "1", "from": "A", "to": "B"}
    for n in ("T4路", "快速公交4线", "1路")]}
assert query._msg_line("北京市", "北京BRT4") is None      # 不能挑中 T4路
assert query._msg_line("北京市", "北京快速公交4线") == "快速公交4线"
assert query._msg_line("北京市", "北京T4路") == "T4路"    # 真点名 T4 还是认
query._catalog = _cat5
# 车来了 state=-5 = 这城不给实时车辆（北京全城），别报成「这会儿没车」
_cl5, query._chelaile = query._chelaile, lambda cid, want: {
    "source": "车来了", "line": "快速公交4线", "unknown": True,
    "desc": "首班 05:00 末班 22:30",
    "note": "上游不给北京的实时车辆位置，只有时刻表：首班 05:00 末班 22:30、19 站",
    "groups": [("阜成门", []), ("龙泉西公交场站", [])]}
_t5 = query.render("北京", "快速公交4", query._chelaile("027", "快速公交4"))
query._chelaile = _cl5
assert _t5.startswith("快速公交4线 查不到实时车，上游不给北京的实时车辆位置"), _t5
assert "这会儿没车" not in _t5, _t5

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
# 「金龙湾来8路没有。」：金龙湾是站名，里面的「龙湾」是温州市龙湾区。区县名被夹在更长的词里
# 不算地名——之前整句被当成温州的事，答「金龙湾来8 在温州没有这条线（温州目录里是另一条同号的线）」
assert query._district_city("金龙湾") is None
assert query._district_city("金龙湾来") is None
assert query._district_city("金龙湾来8路没有。") is None
_cdz, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市"}, "温州": {"mygolbs": "温州市"},
                                           "石家庄": {"mygolbs": "石家庄市"}}
assert query._district_city("我在龙湾8路") == "温州"      # 真在说地名时才认
assert query.detect_city("金龙湾来8路没有。", "金龙湾", "玉林市") == (None, False)   # 别当成某座城明写
assert query.detect_city("在龙湾站等8路", "龙湾", "玉林市") == ("温州", True)
query.cities = _cdz
_rh8, query.render_hop = query.render_hop, lambda ck, nm, got, **kw: f"HOP:{ck}" if ck == "石家庄" else None
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
_p111, mygolbs.post = mygolbs.post, lambda p, timeout=8: (
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
mygolbs.post = _p111
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
_pi, query._plate_index = query._plate_index, lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({"K12345": ("9路", "1", "火车站")}, 50, 80)
query._cache.clear()
assert query.render_plate("玉林市", "桂K12345") == "桂K12345 在 9路 上，往乙站方向，刚过 火车站"
assert "只扫了前 50 条" in query.render_plate("玉林市", "桂K99999")
query._plate_index = lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({}, 80, 80)
query._cache.clear()
assert "80 条方向全扫了" in query.render_plate("玉林市", "桂K99999")
query._plate_index, query._catalog = _pi, _cat
query._cache.clear()

# 引用消息：里面的线路号不能把正文的车牌问题顶掉（引用「G02 现在 2 辆」问「桂K12345 自编号」）
qm = query.QUOTE_RE.search("[引用消息(: 有，G02路 现在 2 辆在路上)] 桂K12345自编号是啥")
assert qm and "G02路 现在 2 辆" in qm.group(1)
assert query.SELFNO_RE.search("桂K12345自编号是啥") and not query.SELFNO_RE.search("桂K12345")
assert query.parse("有，G02路 现在 2 辆在路上")          # 引用原文本身会被 parse 认成线，才需要先剥掉

# 「玉林八中有没有什么公交线路」不是要整城 25 条：句里还剩地名就是在问那个地方
assert query._city_wide("玉林有哪些公交线路？", "玉林市")
assert query._city_wide("玉林这边有哪些公交线路", "玉林市")
assert not query._city_wide("玉林八中有没有什么公交线路", "玉林市")
assert not query._city_wide("玉林北站有哪几条公交线？", "玉林市")

# 微信的引用是整段 XML：wxid_m9ngvm9jcku022 / <svrid>858091132112776141 里的数字会被当成线号，
# 一条「查看X站方向还有没有」被回成「没查到「022」这条线」加四遍「没查到「V1」」
# 正题在 <title>，被引用的那句在 <refermsg><content>
_wx = ('<msg><appmsg><title>查看北流城南站方向还有没有</title><refermsg>'
       '<svrid>858091132112776141</svrid><chatusr>wxid_m9ngvm9jcku022</chatusr>'
       '<content>北流105路 这会儿没车</content></refermsg></appmsg></msg>')
_cwx, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "北流105路", "upperOrDown": "1", "from": "玉林北站", "to": "北流城南站"},
    {"lineName": "北流105路", "upperOrDown": "2", "from": "北流城南站", "to": "玉林北站"}]}
_fwx, query._first_last = query._first_last, lambda cn, ln, d: ("07:30", "19:50")
_wxo = query.answer(_wx, "玉林市", key="wx1")
assert "往北流城南站" in _wxo and "「022」" not in _wxo, _wxo  # 这行会真的读 104（在途/到站两种措辞都过）；车牌 桂K00220D 本身就含 022，只许查「被当成线号」
assert query._dir("查看北流城南站方向还有没有，不是那个方向还在跑吗") == "北流城南站"
assert query._dir("往呈贡广场方向有几辆车") == "呈贡广场"
assert query._dir("哪个方向没车") == ""
query._catalog, query._first_last = _cwx, _fwx

# 自编号：掌上只报车牌、车来了只报自编号，靠同一刻的 GPS 一对一配（一辆只能配一个号）
_cl2, query.chelaile = query.chelaile, types.SimpleNamespace(
    lines=lambda cid: [("9", "L1")],
    live=lambda cid, lid: {"buses": [("514", "甲站", 100), ("397", "乙站", 900)],
                           "pos": {"514": {"lat": 22.6500, "lng": 110.1700},
                                   "397": {"lat": 22.7100, "lng": 110.2300}}})
_ci, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
_lm, query._live_my = query._live_my, lambda cn, ln, d: {"list": [
    {"busNumber": "桂K12345", "bus_lat": 22.6545, "bus_lng": 110.1736,
     "stationName": "甲站", "busToStationNiheDistance": 480, "statusType": "2"},
    {"busNumber": "桂K54321", "bus_lat": 22.7066, "bus_lng": 110.2260,
     "stationName": "乙站", "statusType": "0"}]}
query._plate_index = lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({"K12345": ("9路", "1", "火车站"),
                                  "K54321": ("9路", "1", "甲站")}, 2, 80)
query._cache.clear()
_s514 = query.render_selfno("玉林", "玉林市", "桂K12345")
assert "514（自编号）" in _s514 and "车牌" in _s514, _s514
assert "397（自编号）" in query.render_selfno("玉林", "玉林市", "桂K54321")
# 「这几辆车自编号分别是多少」整条线一次列完，一辆一行
_bl = query.render_line_selfno("玉林", "玉林市", "9")
assert "桂K12345 甲站 480m → 514" in _bl and "桂K54321 已到乙站 → 397" in _bl
# 「几辆 / 到哪了 / 自编号」一句话问三件，三件都得在
assert _bl.split("\n")[0].endswith("自编号：") and "甲站" in _bl
query._plate_index = lambda cn, cap=query.PLATE_MAX, budget=0, start=0: ({}, 80, 80)
query._cache.clear()
assert "没看到 桂K99999" in query.render_selfno("玉林", "玉林市", "桂K99999")
query._live_my, query._plate_index, query.cities, query.chelaile = _lm, _pi, _ci, _cl2
query._cache.clear()

# 「A 咋去 B」「A 怎么去 B」：没「到」字也要认成换乘问句；「我在X咋去Y」把「我在」剥掉
assert query.hop("玉林火车站咋去北流万达") == ("玉林火车站", "北流万达")
assert query.hop("玉林火车站怎么去北流万达") == ("玉林火车站", "北流万达")
assert query.hop("我在十字街咋去火车站") == ("十字街", "火车站")
assert query.hop("怎么去北流万达") is None              # 没出发站，不猜
# 「9路用什么车」：车型/配车只有建档资料有；4 种以上要收，别刷屏
_bp, query.buspedia = query.buspedia, types.SimpleNamespace(
    region_id=lambda cn: 34,
    find_line=lambda cid, nm: [{"uid": "L9", "name": nm, "active": True}],
    models=lambda uid: [("甲 X1", "m1"), ("乙 X2", "m2"), ("丙 X3", "m3"),
                        ("丁 X4", "m4"), ("戊 X5", "m5")],
    comp_name=lambda uid: "玉林公交",
    fleet=lambda mid, c="", r="": [("2020", 10, "1~10")])
_f = query.render_fleet("玉林", "玉林市", "9")
# 只报车型不报数量：/model 的保有量是整家公司的（南宁704 数出 986 辆），当成这条线配车数就是错的
assert "9路 建档了 5 种车型：甲 X1、乙 X2、丙 X3、丁 X4、戊 X5" in _f, _f
assert "辆" not in _f.split("\n")[0], _f
assert "不记每条线配几辆" in _f, _f
query.buspedia = types.SimpleNamespace(
    region_id=lambda cn: 34,
    find_line=lambda cid, nm: [{"uid": "L5", "name": nm, "active": False}],
    models=lambda uid: [], comp_name=lambda uid: "", fleet=lambda *a, **k: [])
assert "车型这站没填" in query.render_fleet("玉林", "玉林市", "5")
query.buspedia = types.SimpleNamespace(region_id=lambda cn: None, find_line=lambda *a: [])
assert "建档里没有 9路" in query.render_fleet("玉林", "玉林市", "9")   # 这站没建这条线：说清，别默默落到实时
# 「北流105配车」：县级市没有自己的 region，得落回父城、用目录里的真名（北流105路）
# 也要认：口语问配车不带「路」字
assert query.parse("北流105配车") == ("105", "北流"), query.parse("北流105配车")
# 一句话问「配车 + 线上几辆」要都答；「配了几辆车」问的是配车本身，别当实时再查一遍
# 「这是抓的档案站吗」只有个「站」字，不是公交问句——反问成「「这是抓的档案站」这个站没查到」就抢了闲聊
assert query.BUS_RE.search("这是抓的档案站吗") is None
assert query.answer("这是抓的档案站吗", "玉林市") == ""
# 「公交？」：只有称呼的一句是对话里的反问。玉林站表里有个站叫「公交二车队」，
# 以前拿「公交」去模糊匹站，答「公交二车队 车来了这边有 1 条线路：3 红十字会医院 ↔ 公交二车队」
assert query._generic_word("公交？") and query._generic_word("巴士") and query._generic_word("公交车")
assert query._generic_word("公交线路") and query._generic_word("公交。")
assert not query._generic_word("公交二车队")      # 真站名照旧能查
assert not query._generic_word("玉林公交公司")
assert query.answer("公交？", "玉林市") == ""
assert query.answer("公交", "玉林市") == ""
# 「你们那还有多少车是纯发动机的」问的是车辆本身（动力/车型），不是哪条线在跑：
# 别扫全城回「玉林市这会儿有 20 条线在跑」，两家实时接口根本没这个字段
_sw12, query.render_sweep = query.render_sweep, lambda city, plates=False, only=None: "SWEEP"
_rs12, query.render_stop = query.render_stop, lambda *a, **k: None
assert query.answer("你们那还有多少车是纯发动机的", "玉林市") == ""
assert query.answer("玉林还有哪条线路在跑", "玉林市") == "SWEEP"      # 真问在跑就照旧扫
query.render_sweep, query.render_stop = _sw12, _rs12
# 站名里带「公交」的照旧要答（这条走车来了模糊匹站）
_cs9, query.cities = query.cities, lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "128"}}
_cl9, query.chelaile = query.chelaile, types.SimpleNamespace(
    lines=lambda cid: [("3", "l1"), ("9", "l2")],
    stations=lambda cid, lid: ["红十字会医院", "公交二车队"] if lid == "l1" else ["火车站"])
assert "公交二车队" in (query.render_stop("玉林", "玉林市", "公交二车队 有哪几路") or "")
query.cities, query.chelaile = _cs9, _cl9
assert query.LIVE_Q.search("西安930路有什么配车，现在线上几辆车")
assert not query.LIVE_Q.search("西安930路配了几辆车"), "配了几辆车 不是问实时"
_seen_r = []
query.buspedia = types.SimpleNamespace(
    region_id=lambda cn: _seen_r.append(cn) or 34,
    find_line=lambda cid, nm: [{"uid": "L9", "name": nm, "active": True}] if nm == "北流105路" else [],
    models=lambda uid: [("甲 X1", "m1")], comp_name=lambda uid: "益业公汽",
    fleet=lambda mid, c="", r="": [("2017", 29, "")])
_oc, query._catalog = query._catalog, lambda cn: {"buslines": [{"lineName": "北流105路", "upperOrDown": "1"}]}
_nb = query.render_fleet("北流", "北流市", "105")
query._catalog = _oc
assert "北流105路 建档了 1 种车型：甲 X1" in _nb, _nb
assert _seen_r == ["玉林市"], _seen_r      # 落父城、且用带「市」的名字（fleet 按这个字符串筛）
# 「用车来了查」只点了源名：上一条的线得接着用（源名剥空 = 追问上一条），没有就反问
# （返空会让模型自己编一份「车来了这边只有 3 条」）
assert query.answer("用车来了查", "玉林市", "", "zz-src") == "查哪条线？报个线名，我换个源再查"
assert "查哪条线" not in (query.answer("车来了", "玉林市", "", "zz-src") or "")
# 「安宁12」两家实时都没收，可这条线真实存在：不能回「没这条」
_bp2 = types.SimpleNamespace(
    region_id=lambda cn: 34,
    find_line=lambda cid, nm: ([{"name": nm, "term": ["甲<>乙"], "active": True}]
                               if nm == "安宁12路" else []),
    find_line_fuzzy=lambda cid, nm: [],
    models=lambda uid: [], comp_name=lambda uid: "", fleet=lambda *a, **k: [])
query.buspedia = _bp2
assert "甲 ↔ 乙" in (query.render_ghost("安宁", "昆明市", "安宁12") or "")
# 单字城市简称要能剥（佛256 → 256路）；全名城名不能剥（安宁99 → 99 会是昆明市区的线）
query.buspedia = types.SimpleNamespace(
    region_id=lambda cn: 34,
    find_line=lambda cid, nm: [{"name": nm, "term": ["丙<>丁"], "active": True}] if nm == "256路" else [],
    find_line_fuzzy=lambda cid, nm: [],
    models=lambda uid: [], comp_name=lambda uid: "", fleet=lambda *a, **k: [])
assert "丙 ↔ 丁" in (query.render_ghost("佛山", "佛山市", "佛256") or "")
assert query.render_ghost("安宁", "昆明市", "安宁99") is None
# 「昆山165路」：苏州目录 1558 条里没有 165（车来了 1916 条也没有），可「16」正好是「165」的前半截，
# 裸包含一匹就把 16 路认成用户问的线，还说「这城目录里有这条线」——纯属胡认
_cc4, _cat4, _bp4 = query.cities, query._catalog, query.buspedia
query.cities = lambda: {"苏州": {"mygolbs": "苏州市", "chelaile": "011"},
                        "惠州": {"mygolbs": "惠州市"}, "武汉": {"mygolbs": "武汉市"},
                        "鄂尔多斯": {"mygolbs": "鄂尔多斯市"}}
query._catalog = lambda cn: {"buslines": [{"lineName": n} for n in ("16路", "65路", "136路", "865路")]}
assert query._exact_line("苏州市", "昆山165", "昆山165") is None
assert query._exact_line("苏州市", "16路", "16路") == "16路"
# 县市自己那套线路没进这城目录：说清「哪块没覆盖」，别只回「两家都没收这条」
assert query._place_gap("苏州", "昆山165") == "昆山"
# 但区名只是行政区、目录里就是这个裸号时不能这么说：「东胜区8路」在鄂尔多斯目录里就是 8路
query._catalog = lambda cn: {"buslines": [{"lineName": n} for n in ("8路", "K1路")]}
assert query._place_gap("鄂尔多斯", "东胜区8") == ""
assert query._place_gap("鄂尔多斯", "东胜区99") == "东胜区"     # 这个号确实没有
query._catalog = lambda cn: {"buslines": [{"lineName": n} for n in ("博罗233路", "233路")]}
assert query._place_gap("惠州", "博罗233") == ""       # 博罗的线就挂在惠州目录下
assert query._place_gap("武汉", "鄂101") == ""         # 「武汉鄂」不是地名
query._catalog = lambda cn: {"buslines": [{"lineName": n} for n in ("16路", "65路")]}
query.buspedia = types.SimpleNamespace(
    region_id=lambda cn: 148,
    find_line=lambda cid, nm: ([{"name": "昆山165路", "term": ["*通医线*"], "active": True}]
                               if nm.startswith("昆山165") else []),
    find_line_fuzzy=lambda cid, nm: [],
    models=lambda uid: [], comp_name=lambda uid: "", fleet=lambda *a, **k: [])
_gk = query.render_ghost("苏州", "苏州市", "昆山165") or ""
assert "昆山这块两家都没覆盖" in _gk and "*" not in _gk, _gk
assert "通医线" in _gk, _gk
# 建档那站这会儿连不上时，也得分得清「没查到」和「这块没覆盖」——别列一堆苏州本地的 16路/65路
query.buspedia = types.SimpleNamespace(region_id=lambda cn: None, find_line=lambda *a: [],
                                      find_line_fuzzy=lambda *a: [])
_grs = query.render("苏州", "昆山165", {"error": "没这条线路"})
assert "昆山的线路掌上公交和车来了都没覆盖" in _grs and "16路" not in _grs, _grs
query.cities, query._catalog, query.buspedia = _cc4, _cat4, _bp4
# 建档记成「已停运」的老号，同城目录里起讫对得上的是它现在的号（苏州 55 → 555）
_oc2 = query._catalog
query._catalog = lambda c: {"buslines": [{"lineName": "555路", "from": "南环桥（华东装饰城）",
                                         "to": "越溪首末站南"}]}
assert query._renamed("苏州市", "南环桥（华东装饰城） ↔ 越溪首末站南") == "555路"
assert query._renamed("苏州市", "甲 ↔ 乙") == ""
query._catalog = _oc2
# 「614」在佛山是 614A/614B（只有车来了收了，目录里全是带字母的）：别回「没查到这条线」
_cc, _cat3, _cl3 = query.cities, query._catalog, query.chelaile
query.cities = lambda: {"佛山": {"mygolbs": "佛山市", "chelaile": "019"}}
query._catalog = lambda c: {"buslines": []}
query.chelaile = types.SimpleNamespace(lines=lambda cid: [("614A", "1"), ("614A", "2"),
                                                          ("614B", "3"), ("600", "4")])
assert query._variants("佛山", "614") == ["614A", "614B"], query._variants("佛山", "614")
assert query._variants("佛山", "600") == []
query.cities, query._catalog, query.chelaile = _cc, _cat3, _cl3
query.buspedia = _bp

# 「2路到汽车总站叫我」：到站提醒（照 Trackr 的口径：站序差 / 节奏 / 同一辆车只喊一次）
assert query.WATCH_RE.search("2路到汽车总站叫我")
assert query.WATCH_RE.search("G02到玉林北站提醒我")
assert query.WATCH_OFF_RE.search("取消提醒")
assert query.WATCH_Q_RE.search("我的提醒")
assert watch._left({"1": ["甲站", "乙站", "丙站"]}, "甲站", "丙站") == 2
assert watch._left({"1": ["甲站", "乙站", "丙站"]}, "丙站", "甲站") == -2
assert watch._left({"1": ["甲站", "乙站"]}, "甲站", "戊站") is None     # 这个方向不经过
assert watch._interval(0) == 10 and watch._interval(3) == 20 and watch._interval(9) == 45
assert watch._interval(1, 3) == 80                                    # 连续失败 ×2 退避
# 盯车时说的时间也得是北京时间（实例是 UTC，用 localtime 会差 8 小时）
assert _tm.strftime("%H:%M", query._now(0)) == "08:00"

# 「我在汽车总站等1路和G02」：第一人称+所在站，「我」曾被 CHAT 判成闲聊、整句交给模型
assert query.looks_like_ask("我在汽车总站等1路和G02")
assert query.looks_like_ask("我在玉林火车站等9路")
assert not query.looks_like_ask("我在家躺着")

# 「一起等」：同一个站几条线的车排成一张表，按到站先后，没车的/不经过的分开说
_pk2, _ra2 = query.pick_stop, query.render_at


def _fake_at(city_key, city_name, want, msg, veh="", tally=None, at=""):
    tbl = {"1": ("1路", {"甲": (9, "K1", "还有 6 站")}),
           "G02": ("G02路", {"乙": (2, "K2", "还有 1 站")}),
           "9": ("9路", {}),
           "7": ("7路", None)}
    line_name, live = tbl[want]
    if tally is not None:
        tally[line_name] = None if live is None else (live, [], "汽车总站")
    return "x"


query.pick_stop = lambda cn, msg: "汽车总站"
query.render_at = _fake_at
_tog = query.render_together("玉林", "玉林市", ["1", "G02", "9", "7"], "汽车总站等1路G02 9路 7路")
_tog = _tog.split("\n")
assert _tog[0] == "汽车总站 这会儿，按到站先后：", _tog
assert "G02路（往乙） 大约 2 分钟" in _tog[1], _tog       # 2 分钟的排最前，不是按问的顺序
assert "1路（往甲） 大约 9 分钟" in _tog[2], _tog
assert _tog[3] == "9路 这会儿没车报过来", _tog
assert _tog[4] == "7路 不经过汽车总站", _tog
assert query.render_together("玉林", "玉林市", ["7"], "汽车总站等7路") is None   # 全都不到这站，交回原路径
query.pick_stop, query.render_at = _pk2, _ra2

# 「从1路东线」：号上粘着汉字的名字（在从化，属广州）。本城目录里没有就别回「没查到「1」」
assert query._written_name("从1路东线", "1") == "从1路东线"
assert query._written_name("1路东线", "1") == "1路东线"
assert query._written_name("1路末班车几点", "1") == ""      # 「末班车」不是线名后缀
assert query._written_name("坐1路了", "1") == ""            # 「坐」是动词不是前缀地名
assert query._written_name("105路", "105") == ""            # 纯号

# 「查询玉林职院附近有没有公交车」：动词粘在站名前头，把「职院」和站名隔开了
_c1 = list(query._cands("玉林市", "查询玉林职院附近有没有公交车"))
assert "职院" in _c1, _c1                    # 「查询」剥掉了，简称才补得成全称
assert "那洪立交" in list(query._cands("玉林市", "那洪立交"))   # 剥动词不能把「那」开头的站名吃掉
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

# 「这站哪辆车来得最快」：逐线逐方向比到站预测，报最快那辆 + 没预测的线点名
_cat2, query._catalog = query._catalog, lambda cn: {"buslines": [
    {"lineName": "1路", "upperOrDown": "1", "to": "甲站"},
    {"lineName": "2路", "upperOrDown": "1", "to": "乙站"}]}
_sq, query._station_seq = query._station_seq, lambda cn, ln, d: (
    [{"stationName": "丙站"}] * 3 if (ln, d) == ("1路", "1")
    else ([{"stationName": "丙站"}] if (ln, d) == ("2路", "1") else []))
_lm2, query._live_my = query._live_my, lambda cn, ln, d, order=1: (
    {"routeOnStationRTimeInfoList": [{"busNumber": "桂K1", "busToStationCount": 2,
                                      "busToStationTime": 4}]} if ln == "1路" else {})
query._cache.clear()
_b = query.render_stop_best("玉林市", "丙站", ["1路", "2路"])
assert _b.splitlines()[0] == "丙站 这会儿：", _b
assert "1路 往甲站方向：桂K1 约 4 分钟（还有 2 站）" in _b, _b       # 最快的排第一行
assert "2路 这会儿没有到站预测" in _b, _b                            # 没预测的线单独点名
# 始发站上游不给到站预测，只给「等待发车」：车就在站里，得报出来，不能算成没车
_lm3 = query._live_my
query._live_my = lambda cn, ln, d, order=1: (
    {"routeOnStationRTimeInfoList": [{"busNumber": "", "busToStationCount": -1,
                                      "busToStationTips": "等待发车"}],
     "list": [{"busNumber": "桂K9", "stationName": "丙站站前广场"}]}
    if ln == "1路" else {})
_b2 = query.render_stop_best("玉林市", "丙站", ["1路"])
assert "1路 往甲站：车已经在丙站站前广场等着发车（桂K9）" in _b2, _b2
query._live_my = _lm3
query._catalog, query._station_seq, query._live_my = _cat2, _sq, _lm2
query._cache.clear()

# 目录把号写在括号里（「大容山旅游专线（28路）」）：归一化会连号一起剥掉，
# 拿裸号查必须能匹上，否则「玉林28路」永远「没这条线」
_pk = query._pick
assert [c[0] for c in _pk([("大容山旅游专线（28路）", 1)], "28")] == ["大容山旅游专线（28路）"]
assert [c[0] for c in _pk([("大容山旅游专线（28路）", 1), ("28路", 2)], "28")] == ["28路"]   # 全等优先
assert [c[0] for c in _pk([("大容山旅游专线（28路）", 1)], "大容山旅游专")] == ["大容山旅游专线（28路）"]

assert query.STOPS_RE.search("玉林9路站点") and not query.STOPS_RE.search("9路现在到哪个站点了")
# 有些城两家的号只在数字那截一样（掌上 BS09863D / 车来了 粤B09863D），core 是第二把钥匙
assert query._plate_core("粤B09863D") == query._plate_core("BS09863D") == "09863D"
assert query._plate_core("桂K11860D") == "11860D"
# 「北流有哪些公交线路」是线路清单，「有哪些公交公司」才是公司清单
assert query.CITYLINES_RE.search("北流有哪些公交线路") and not query.CITY_CO_RE.search("北流有哪些公交线路")
assert query.CITY_CO_RE.search("北流有哪些公交公司") and query.CITY_CO_RE.search("玉林公交公司电话是多少")
# 「济宁有哪几个公交公司」——「哪几个/几个/多少个」原来不认，掉进扫车分支答成「抽查了 60 条主线都没车」
for m in ("济宁有哪几个公交公司", "济宁有几个公交公司", "济宁有多少个公交公司"):
    assert query.CITY_CO_RE.search(m), m
assert not query.CITY_CO_RE.search("济宁9路有几个站")
# 「玉林市含有几家公交公司」原来落到站名兜底，答成「市含」这个站——问的是公司数，不是站点
for m in ("玉林市含有几家公交公司", "玉林有几家公交公司", "玉林市公交公司有几家"):
    assert query.CITY_CO_RE.search(m), m
assert not query.CITY_CO_RE.search("司机和公交公司救援")
# 「济宁76夜线」是掌上的真线「76路夜线」，不带「路」字也得认；削成 76路会答成白班首末班
assert query.parse("济宁76夜线") == ("76夜", "济宁")
assert query.parse("济宁76路夜线") == ("76夜", "济宁")
assert query.parse("济宁76路") == ("76", "济宁")          # 白班别被夜线吃掉
assert query.BUS_RE.search("济宁76夜线")
_oc = query._catalog
query._catalog = lambda cn: {"buslines": [{"lineName": "北流101路", "to": "西山"},
                                         {"lineName": "北流101路", "to": "北流客运中心"}]}
assert query.render_city_lines("北流市") == "北流这边 1 条线路：\n北流101路：西山 ↔ 北流客运中心"
query._catalog = _oc

# 「N路离X站大约几分钟」：一句话给结论；两个方向都有车时才带方向名
_p, _c2, _s, _l, _e = query.pick_stop, query._catalog, query._station_seq, query._live_my, query._etas
query.pick_stop = lambda cn, m: "十字街"
_one_dir = {"buslines": [{"lineName": "9路", "upperOrDown": "1", "to": "甲"}]}
_both = {"buslines": [{"lineName": "9路", "upperOrDown": "1", "to": "甲"},
                      {"lineName": "9路", "upperOrDown": "2", "to": "乙"}]}
query._catalog = lambda cn: _one_dir
query._station_seq = lambda cn, ln, d: [{"stationName": "十字街"}]
query._live_my = lambda cn, ln, d, i=1: {"n": d}
query._etas = lambda p: (7.0, 2, "桂K1")
assert query.render_at("玉林", "玉林市", "9", "十字街") == "9路 离十字街 大约 7 分钟（桂K1，还有 2 站）"
# 「用车来了查玉林G02」：点名了数据源就不许把另一家的车合进来，两份编号体系混一起一眼就串
_mg, _cl, _ct = query._mygolbs, query._chelaile, query.cities
query.cities = lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
query._mygolbs = lambda c, w: {"source": "掌上公交", "line": "G02路",
                               "groups": [("往A", [("桂K1", "甲", 100, "2")])],
                               "pos": {"桂K1": {"lat": 22.0, "lng": 110.0}}}
query._chelaile = lambda c, w: {"source": "车来了", "line": "G02",
                                "groups": [("往A", [("512", "甲", 100, "2")])],
                                "pos": {"512": {"lat": 23.0, "lng": 111.0}}}
_r = query.query("玉林", ["G02"], "chelaile")
assert _r["source"] == "车来了" and [b[0] for _, bs in _r["groups"] for b in bs] == ["512"]
assert len([b for _, bs in query.query("玉林", ["G02"])["groups"] for b in bs]) == 2
query._mygolbs, query._chelaile, query.cities = _mg, _cl, _ct
query._catalog = lambda cn: _both
query._etas = lambda p: ((20.0 if p["n"] == "2" else 7.0), 2, "桂K" + p["n"])
assert query.render_at("玉林", "玉林市", "9", "十字街") == \
    "9路 离十字街：往甲 大约 7 分钟（桂K1，还有 2 站）；往乙 大约 20 分钟（桂K2，还有 2 站）"
# 哪个方向没有到站时间也要如实报出来，不能只念有车的那一边
query._etas = lambda p: (7.0, 2, "桂K1") if p["n"] == "1" else None
assert query.render_at("玉林", "玉林市", "9", "十字街") == \
    "9路 离十字街：往甲 大约 7 分钟（桂K1，还有 2 站）；往乙 这会儿没车报过来"
# 上游点明没车时要对一下首末班：真收班了说收班，运营窗口里只说这一站没预测（别抄上游的「非运营时间」）
query._etas = lambda p: None
query._live_my = lambda cn, ln, d, i=1: {
    "routeOnStationRTimeInfoList": [{"busNumber": "", "busToStationCount": -1, "busToStationTips": "非运营时间"}]}
_fl, _w = query._first_last, query._window
query._first_last = lambda cn, ln, d: ("20:35", "22:55")
query._window = lambda f, l: "after"
assert query.render_at("玉林", "玉林市", "9", "十字街") == \
    "9路 这会儿没车报过来，末班已经开走：20:35–22:55"
query._window = lambda f, l: "open"      # 窗口里还开着：上游那句「非运营时间」不能照抄
assert query.render_at("玉林", "玉林市", "9", "十字街") == \
    "9路 这会儿没有到十字街的预测（往甲、乙方向都没报过来，刚过站或还没发车）"
query._first_last, query._window = _fl, _w
query.pick_stop, query._catalog, query._station_seq, query._live_my, query._etas = _p, _c2, _s, _l, _e

# 「安宁12」：安宁市属昆明，可掌上只有兰州的安宁区，「安宁」这个键被区占了。裸「安宁」得
# 归昆明、线名里的「安宁」不能当城市名剥掉，否则会答成昆明市区的 12 路
_c0, _cat0, _cl0 = query.cities, query._catalog, query.chelaile.lines
query.cities = lambda: {"安宁": {"mygolbs": "昆明市", "chelaile": "081"},
                        "安宁区": {"mygolbs": "兰州市"},
                        "昆明": {"mygolbs": "昆明市", "chelaile": "081"}}
assert query._line_pre("安宁") == "安宁", query._line_pre("安宁")
assert query._line_pre("安宁区") == "", query._line_pre("安宁区")
assert query.parse("安宁12")[0] == "安宁12", query.parse("安宁12")
query._catalog = lambda n: {"buslines": [{"lineName": "安宁K1"}, {"lineName": "12路"}]}
query.chelaile.lines = lambda c: [("安宁K7", "1")]
assert query._near("昆明", "安宁12") == "安宁K1、安宁K7", query._near("昆明", "安宁12")
query._catalog, query.chelaile.lines, query.cities = _cat0, _cl0, _c0

# 「南京新街口有哪些公交」被当成了「哪条线在跑」，整城 818 条倒进群里
assert query.TAIL.sub("", "新街口有哪些公交") == "新街口", query.TAIL.sub("", "新街口有哪些公交")
assert not query.SWEEP_RE.search("南京新街口有哪些公交"), "哪些不该进 SWEEP_RE"
assert query.SWEEP_RE.search("南宁今晚还有没有公交"), "时间词那句还得走 sweep"
assert query.STOPASK_RE.search("新乡胖东来三胖有哪些公交")
assert not query.STOPASK_RE.search("玉林汽车总站有什么公交车不是站")

# 「新乡饮马口站有几条线路」不是「几路」，快路没接住就整句进了模型（它去报全城 93 条）
assert query.BUS_RE.search("新乡饮马口站有几条线路")
assert query.BUS_RE.search("饮马口站有哪些线路")
assert not query.BUS_RE.search("这条线路真长"), "聊线路长短不该抢"

# 「饮马口站有几条线路」「东简客运站有几条线路」：问法得刮干净，别整句当站名
assert query.TAIL.sub("", "饮马口站有几条线路") == "饮马口站", query.TAIL.sub("", "饮马口站有几条线路")
# 「X 的公交线路」：TAIL 只剥「线路」会剩「的公交」，站名就搜不到了
assert query.TAIL.sub("", "关山月美术馆②的公交线路") == "关山月美术馆②", \
    query.TAIL.sub("", "关山月美术馆②的公交线路")
# 文字线名 parse 认不出（靠 TEXT_LINE 兜）：这句话不能去看 ctx 里的线号，
# 否则先问过 M308 再问这句会答成「湛江没查到 M308」
assert query.parse("用车来了查湛江宝钢专线") is None
assert query.TEXT_LINE.search("用车来了查湛江宝钢专线")
assert query.STOPASK_RE.search("饮马口站有几条线路")
assert not query._subseq("东简", "吴川东升客运站"), "东简客运站不该匹到吴川东升客运站"

# 整城清单要收口：郑州 650 条只列前 40，末尾说还剩多少
_cap = query._catalog
query._catalog = lambda cn: {"buslines": [{"lineName": f"{i}路", "to": f"站{i}"} for i in range(1, 651)]}
_txt = query.render_city_lines("郑州市").split("\n")
assert _txt[0] == "郑州这边 650 条线路，先列前 40 条：", _txt[0]
assert len(_txt) == 42 and _txt[-1] == "还有 610 条没列，想看哪条直接报线路号", _txt[-1]
query._catalog = _cap

# 「北流汽车新城」不能砍掉「新城」匹上「北流汽车总站」：砍尾巴只许砍通用字
assert query.STOP_TRIM_OK.fullmatch("站"), "「饮马口站」这种尾巴要放行"
assert not query.STOP_TRIM_OK.fullmatch("新城"), "「汽车新城」的「新城」不是通用字"

# 点名的线名不是数字时（宝钢专线/大容山专线）不能沿用上一条问过的 12 路
_lk0, _lq0, _ra0, _cat1 = query._last, query.query, query.render_at, query._catalog
query._last = lambda k: {"line": "12"}
query._catalog = lambda cn: {"buslines": [{"lineName": "大容山旅游专线（28路）", "to": "大容山景区"}]}
query.render_at = lambda *a, **k: None
query.query = lambda ck, wants, src=None, veh="": {
    "line": "大容山旅游专线（28路）", "groups": [("大容山景区", [("桂K1", "站", "100米")])]}
_txt = query.answer("用车来了查玉林大容山专线", "玉林市")
assert "大容山" in _txt and "12 现在" not in _txt, _txt
query._last, query.query, query.render_at, query._catalog = _lk0, _lq0, _ra0, _cat1

# 只在车来了目录里的线名也要认（湛江妇幼专线），否则会沿用上一条问过的线
_c1, _cl1 = query.cities, query.chelaile.lines
query.cities = lambda: {"湛江": {"chelaile": "157", "mygolbs": "湛江市"}}
query._catalog = lambda cn: {"buslines": []}
query.chelaile.lines = lambda cid: [("妇幼专线", "1"), ("宝钢专线", "2")]
assert query._msg_line("湛江市", "用车来了查湛江妇幼专线", "湛江") == "妇幼专线"
assert query._msg_line("湛江市", "湛江宝钢专线", "湛江") == "宝钢专线"
query.cities, query.chelaile.lines = _c1, _cl1
# 「夜班车」进快路，但「昨晚夜班车真慢」这种叙述不算
assert query.BUS_RE.search("玉林北站夜班车有没有车")
assert not query.BUS_RE.search("昨晚夜班车真慢")
# 纯文字线名（「妇幼专线」）不带数字也不带公交词，也要进快路；叙述句仍交给模型
assert query.BUS_RE.search("妇幼专线")
assert query.TEXT_LINE.fullmatch("妇幼专线")
assert not query.TEXT_LINE.fullmatch("这条专线真长")
assert not query.looks_like_ask("这条专线真长")

# 县级市（吴川）掌上认、车来了只给地级市 id：线只在车来了目录里也要认出来
_c2, _cl2, _cat2 = query.cities, query.chelaile.lines, query._catalog
query.cities = lambda: {"吴川": {"mygolbs": "吴川市", "chelaile": "157"}}
query._catalog = lambda cn: {"buslines": [{"lineName": "1路（梅菉-霞山新线）"}]}
query.chelaile.lines = lambda cid: [("吴川2路（梅菉-霞山旧线）", "1"), ("2路", "2")]
assert query._exact_line("吴川市", "湛江吴川二路（梅菉-霞山旧线）", "2", "吴川") == "吴川2路（梅菉-霞山旧线）"
# 车来了目录里只有别的县的同名线时宁可返空（「吴川9路」不许变湛江市区 9 路）
assert query._exact_line("吴川市", "湛江2路", "2", "吴川") is None
# 借来的车来了 id 只查带地名的线名：「吴川9路」不许落成湛江市区的 9 路
_c3 = query.cities
query.cities = lambda: {"吴川": {"mygolbs": "吴川市", "chelaile": "157", "_borrow": True}}
assert "error" in query.query("吴川", ["9"], "chelaile", ""), "裸号不该去借来的目录里查"
query.cities = _c3
query.cities, query.chelaile.lines, query._catalog = _c2, _cl2, _cat2

# 「妇幼专线」这种纯文字线名只在车来了那边有：默认城没有就沿用上一条问过的城
_c4, _ml4, _o4 = query.cities, query._msg_line, query._one
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "湛江": {"mygolbs": "湛江市", "chelaile": "157"}}
query._msg_line = lambda cn, msg, ck="": ("妇幼专线" if cn == "湛江市" else None)
_seen = {}
query._one = lambda ck, nm, num, pre, msg, src, ex, last, key="": _seen.update({"ck": ck}) or "ok"
query.LAST["t_fu"] = {"city": "湛江"}
assert query.answer("妇幼专线", "玉林市", key="t_fu") == "ok"
assert _seen["ck"] == "湛江", "纯文字线名该沿用上一条问过的城"
query.LAST.pop("t_fu", None)
query.cities, query._msg_line, query._one = _c4, _ml4, _o4

# 车来了站名只在那边有（湛江文化中心，掌上 551 个站名里没有）：目录 120 个方向，
# 写死扫前 60 条会漏——8 路在第 88 位
_c5c, _c5l, _c5s = query.cities, query.chelaile.lines, query.chelaile.stations
query.cities = lambda: {"湛江": {"mygolbs": "湛江市", "chelaile": "157"}}
query.chelaile.lines = lambda cid: [(str(i), f"L{i}") for i in range(1, 90)] + [("8", "A"), ("8", "B")]
query.chelaile.stations = lambda cid, lid: (["寸金公交站", "湛江文化中心", "四航局"] if lid in ("A", "B")
                                            else ["甲站", "乙站"])
_c5o = query._stop_chelaile("湛江市", "湛江文化中心")
assert "8 寸金公交站 ↔ 四航局" in _c5o, _c5o
assert "1 条线路" in _c5o and "只扫了前" not in _c5o
query.cities, query.chelaile.lines, query.chelaile.stations = _c5c, _c5l, _c5s

# 「玉林公交公司」平台站名是「公共汽车公司」：别名换写法再搜，别砍成「公司」
_a6, _s6 = query.cities, query.stops
query.cities = lambda: {"玉林": {"mygolbs": "玉林市", "chelaile": "440"}}
query.stops = lambda cn, kw: [{"stationName": "公共汽车公司"}] * 2 if "公共汽车" in kw else []
assert "玉林公共汽车公司" in list(query._cands("玉林市", "玉林公交公司有没有公交"))
assert query.pick_stop("玉林市", "公交公司有哪几路") == "公共汽车公司"
query.cities, query.stops = _a6, _s6

# 「成都东16」= 成都 + 东16：方位字是线名的一部分（成都目录里叫东16路），不是「成都东」站
_cd7, query.cities = query.cities, lambda: {"成都": {"mygolbs": "成都市"},
                                            "玉林": {"mygolbs": "玉林市"},
                                            "牡丹江": {"mygolbs": "牡丹江市"},
                                            "柳州": {"mygolbs": "柳州市"}}
assert query.parse("成都东16") == ("东16", "成都")
assert query.parse("成都东16现在有车吗") == ("东16", "成都")
assert query.parse("玉林2023年GDP") is None
assert query.BUS_RE.search("成都东16")
query.cities = _cd7

# 同一所学校被平台拆成几条站记录，各挂各的线：「市一职校（玉林技师学院）」只挂城际，
# 7路/G01 挂在「玉林技师学院（市一职中）」名下 —— 别名要能认到一起去
assert query._alias("玉林技师学院（市一职中）") & query._alias("市一职校（玉林技师学院）") == {"玉林技师学院"}
_s8, query.stops = query.stops, lambda c, k: {
    "市一职校（玉林技师学院）": [{"stationName": "市一职校（玉林技师学院）", "relatedRoutes": ["城际线"],
                                  "lat": "22.6500", "lng": "110.2000"}],
    "玉林技师学院": [{"stationName": "市一职校（玉林技师学院）", "relatedRoutes": ["城际线"],
                      "lat": "22.6500", "lng": "110.2000"},
                     {"stationName": "玉林技师学院（市一职中）", "relatedRoutes": ["G01路"],
                      "lat": "22.6519", "lng": "110.2013"}],
}.get(k, [])
assert query._full_stop("玉林市", {"stationName": "市一职校（玉林技师学院）",
                                 "relatedRoutes": ["城际线"]})["relatedRoutes"] == ["G01路", "城际线"]
query.stops = _s8

# 「麻章车站」问多少条线路：隔壁站牌「麻章汽车站」不算（「麻章」二字也够 0.8 相似度），
# 短候选「麻章」更不能把「麻章一中」这类站带进来
_c9c, _c9l, _c9s = query.cities, query.chelaile.lines, query.chelaile.stations
query.cities = lambda: {"湛江": {"mygolbs": "湛江市", "chelaile": "157"}}
query.chelaile.lines = lambda cid: [("6", "S"), ("52", "T"), ("太平-麻章A线", "U")]
query.chelaile.stations = lambda cid, lid: {"S": ["海洋大学", "麻章车站"], "T": ["湛江北站", "麻章车站"],
                                            "U": ["太平客运站", "麻章汽车站"]}[lid]
query._cache.clear()          # 上面文化中心那块的 catalog-cl 157 还挂在缓存里
_c9o = query._stop_chelaile("湛江市", "用车来了查湛江麻章车站有多少条公交线路")
assert "2 条线路" in _c9o and "6 " in _c9o and "52 " in _c9o and "太平" not in _c9o, _c9o
query.cities, query.chelaile.lines, query.chelaile.stations = _c9c, _c9l, _c9s

# 「从市体育中心坐到玉林北站」：动词粘在站名后面，站牌上还少个「市」；
# 上游会把起点换成附近的「文体人民路口」，这种一条腿的方案不能交回 render_hop
assert query.hop("从市体育中心坐到玉林北站坐哪条公交线路呢") == ("市体育中心", "玉林北站")
_p9s, query.stops = query.stops, lambda cn, kw: [{"stationName": "体育中心", "relatedRoutes": ["7路"],
                                                 "lat": "22.6", "lng": "110.1"}] if kw == "体育中心" else []
assert (query.find_stop("玉林市", "市体育中心")[0] or {}).get("stationName") == "体育中心"
query.stops = _p9s
_p9a, query._ask111 = query._ask111, lambda cn, a, b: [{
    "stationNum": "15站", "routeUpDownSimple1": [
        {"routeName": "G01路", "upStationName": "文体人民路口"}]}]
assert "文体人民路口" in query.render_plan("玉林市", "市体育中心", "玉林北站")
# 换乘段要标只坐几站：「玉林总站-福绵新滔工业园」这种区间线名念出来像「要坐去福绵」，
# 群里就有人问「你怎么让我坐福绵那条」，其实只反向坐 2 站到换乘站
_p9b, query._ask111 = query._ask111, lambda cn, a, b: [{
    "stationNum": "32站", "routeUpDownSimple1": [
        {"routeName": "玉林总站-福绵新滔工业园", "upStationName": "电脑城（新民路）",
         "upStationIndex": 26, "downStationIndex": 28},
        {"routeName": "玉林至北流城际公交线路", "upStationName": "市二医院西院（一环北路）",
         "upStationIndex": 2, "downStationIndex": 31}]}]
# 111 只按总时长排，永远让你在城际半路（第 2 站）上；用户实际想先坐 26/6 路到首发站坐着去
_p9d, query.mygolbs.stations = query.mygolbs.stations, lambda cn, ln, d: [{"stationName": "汽车总站"}]
_p9e, query.render_hop = query.render_hop, lambda ck, cn, got, only_direct=False: (
    "从电脑城到汽车总站能直达：\n26路（2 站），首班 07:10 还没到\n6路（3 站），首班 07:00 还没到")
_p9c = query.render_plan("玉林市", "玉林电脑城", "北流汽车总站")
assert "玉林总站-福绵新滔工业园（2站）" in _p9c, _p9c
assert "玉林至北流城际公交线路（29站）" in _p9c, _p9c
assert "汽车总站是 玉林至北流城际公交线路 的首发站" in _p9c, _p9c
assert "26路 2站、6路 3站" in _p9c, _p9c
# 111 只数站数，会排「反向坐 2 站福绵线再换城际」这种凑数方案——玉林本地没人这么干，
# 比最快方案慢的、腿只坐 1~2 站的直接不显示
_p9f, query._ask111 = query._ask111, lambda cn, a, b: [
    {"stationNum": "34站", "totalTime": "1小时3分钟", "routeUpDownSimple1": [
        {"routeName": "7路", "upStationName": "电脑城", "upStationIndex": 1, "downStationIndex": 12}],
     "routeUpDownSimple2": [
        {"routeName": "玉林至北流城际公交线路", "upStationName": "旅游汽车客运站",
         "upStationIndex": 9, "downStationIndex": 32}]},
    {"stationNum": "32站", "totalTime": "1小时10分钟", "totalWalkDistance": "231米",
     "routeUpDownSimple1": [
        {"routeName": "玉林总站-福绵新滔工业园", "upStationName": "电脑城（新民路）",
         "upStationIndex": 26, "downStationIndex": 28}],
     "routeUpDownSimple2": [
        {"routeName": "玉林至北流城际公交线路", "upStationName": "市二医院西院（一环北路）",
         "upStationIndex": 2, "downStationIndex": 32}]}]
_p9g = query.render_plan("玉林市", "玉林电脑城", "北流汽车总站")
assert "福绵" not in _p9g, _p9g
assert "坐 7路（11站）" in _p9g and "或者" not in _p9g, _p9g
query._ask111 = _p9f
query.mygolbs.stations, query.render_hop = _p9d, _p9e
query._ask111 = _p9a

# 玉林导乘资料（玉林微途整理）：换乘站表、枢纽站上车点、联营线无实时
assert yulin.ways("G01路")[1]["中药港"] == "8"
assert yulin.ways("G01") == yulin.ways("G01路")          # 带不带「路」都是同一条
assert yulin.note("北流105路").startswith("部分车次")
assert yulin.no_real("玉樟线").startswith("玉林-樟木")
assert yulin.plan(["9路"], ["23路"]) == ("9路", "工业品电器城", "23路")
assert yulin.split("1/3/G02夜班车") == ["1", "3", "G02夜班车"]
_w9 = query.render_ways(*yulin.ways("G01路"))
assert "中药港：8" in _w9 and "技师学院" not in _w9, _w9   # 技师学院是另一个站，注释里不许并进这句
# 「主要换乘站」用导乘表，问「每站」仍逐站扫上游
assert "主要换乘站" in query.render_transfer("玉林市", "G01路", "G01主要换乘站")
assert "主要换乘站" not in (query.render_transfer("玉林市", "G01路", "G01每站能换乘啥") or "")

# 2026-11-05 群的 412 条真实提问里翻出来的四类崩坏，见各条注释
# ①「G02 有多少辆车，以及到哪里了」：HOP_RE 按「到」切，「以及」被当出发站
assert query.hop("看一下现在玉林 G02 路有多少辆车，以及到哪里了") is None
assert query.hop("从十字街到火车站") == ("十字街", "火车站")
# ②「湛江线路」「香港城巴全部线路」都是要整城清单，不认就落到最后返回空串
assert query._city_wide("用车来了查湛江线路", "湛江市")
assert query._city_wide("用车来了查香港城巴全部线路", "香港特别行政区")
assert not query._city_wide("用车来了查湛江开发区公交全部线路", "湛江市")
# ③「安宁区8路」：线号前那个词是行政区名，不是站名，别匹成「安宁区医院」
_p10, query.pick_stop = query.pick_stop, lambda cn, m: "安宁区医院"   # 上游只有这个名字相近的站
assert query.render_at("兰州", "兰州市", "8", "安宁区8路") is None
assert query.render_at("鄂尔多斯", "鄂尔多斯市", "8", "东胜区 8路 夜班车") is None
query.pick_stop = _p10

print("selftest ok")
if "--live" in sys.argv:
    import importlib

    importlib.reload(query)  # 拿掉上面被替换掉的假城市表
    for msg in ["玉林 G02 路有没有车", "香港 1 路还有几站", "大连 2 路到哪了",
                "玉林 从十字街到火车站坐什么车", "南宁 我在金湖广场，16路还有几站",
                "南宁 金湖广场有几路车", "容县 11 路有车吗", "玉林 6 路夜班还有几站"]:
        print("\n问:", msg)
        print(query.answer(msg, "玉林市"))
