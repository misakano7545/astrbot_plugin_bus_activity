"""离线自检：python3 selftest.py

不联网，只验解析/匹配/排版这些纯逻辑；加 --live 再打一次真接口。
"""

import sys

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
    "南宁", "1", {"source": "掌上公交", "line": "1", "groups": [("", [])], "note": "线路还在跑，这会儿没车开车机"})
assert "末班" in query.render(
    "南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])], "desc": "本线路末班时间已过"})
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])]}) == \
    "1 这会儿没车"
assert query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])],
                                  "note": "末班 19:50 已经开走了"}) == \
    "1 这会儿没车，末班 19:50 已经开走了"
# 线路不存在 / 有近似线名 / 上游异常：别把机器串甩进群里
assert query.render("玉林", "999", {"error": "没这条线路"}) == "没查到「999」这条线"
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
_hq, query.query = query.query, lambda ck, wants: (
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
_sweep, query.render_sweep = query.render_sweep, lambda city: f"SWEEP:{city}"
_stop, query.render_stop = query.render_stop, lambda *a: None
assert query.answer("玉林 还有哪条线路有公交车", "玉林市") == "SWEEP:玉林市"
assert query.answer("玉林 今天天气怎么样", "玉林市") == ""      # 不像公交问题就交给 LLM
query.render_sweep, query.render_stop = _sweep, _stop

# hasReal=0 是「上游没给车辆位置」，不是「没车」，不能混成一句谎话
assert query.render("x", "1", {"line": "1路", "groups": [("A", []), ("B", [])],
                               "note": "上游对这个城市不提供实时车辆位置",
                               "unknown": True}).startswith("1路 查不到实时车")
assert query.render("x", "1", {"line": "1路", "groups": [("A", []), ("B", [])],
                               "note": "末班 20:25 已经开走了"}).startswith("1路 这会儿没车")

print("selftest ok")

if "--live" in sys.argv:
    import importlib

    importlib.reload(query)  # 拿掉上面被替换掉的假城市表
    for msg in ["玉林 G02 路有没有车", "香港 1 路还有几站", "大连 2 路到哪了",
                "玉林 从十字街到火车站坐什么车", "南宁 我在金湖广场，16路还有几站",
                "南宁 金湖广场有几路车", "容县 11 路有车吗", "玉林 6 路夜班还有几站"]:
        print("\n问:", msg)
        print(query.answer(msg, "玉林市"))
