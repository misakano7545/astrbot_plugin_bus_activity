"""离线自检：python3 selftest.py

不联网，只验解析/匹配/排版这些纯逻辑；加 --live 再打一次真接口。
"""
import re

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
    "南宁", "1", {"source": "掌上公交", "line": "1", "groups": [("", [])], "note": "线路还在跑，只是车机没报位置"})
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
_hq, query.query = query.query, lambda ck, wants, src=None: (
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

# issue #8：前缀是线路名的一部分，明写城市也不丢；前缀自己就是城市时以它为准
_cap, _q = [], query.query
query.query = lambda ck, wants, src=None: (_cap.append((ck, wants)), {"error": "没这条线路"})[1]
_cit = query.cities
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "容县": {"mygolbs": "容县市"}}
_rc, query.render_company = query.render_company, lambda *a: None
_rs, query.render_stops = query.render_stops, lambda *a: None
_ra, query.render_at = query.render_at, lambda *a: None
query.answer("玉林 容县 11 路有车吗", "玉林市")
assert _cap[-1] == ("容县", ["11"]), _cap[-1]          # 别拿玉林的目录查裸号 11
query.answer("玉林 陆川 1 路有车吗", "玉林市")
assert _cap[-1] == ("玉林", ["陆川1"]), _cap[-1]       # 目录里有陆川1路
query.query, query.cities = _q, _cit
query.render_company, query.render_stops, query.render_at = _rc, _rs, _ra

# 没写城市、默认城市又查不到：要说明「按谁查的」，别让用户以为这条线不存在
_q, query.query = query.query, lambda ck, wants, src=None: {"error": "没这条线路"}
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
_q2, query.query = query.query, lambda ck, w, src=None: (
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
# 站表不许截断：23 条就报 23 条，否则用户再问「剩下的呢」认不出站名
_cat, _stops = query._catalog, query.stops
query._catalog = lambda cn: {"buslines": [{"lineName": f"{i}路", "from": "甲", "to": "乙"} for i in range(23)]}
query.stops = lambda cn, kw: [{"stationName": "谈固站", "relatedRoutes": [f"{i}路" for i in range(23)]}]
_out = query.render_stop("石家庄市", "石家庄市", "谈固站有哪几路公交车")
assert _out.count("\n") == 23 and "还有" not in _out, _out
query._catalog, query.stops = _cat, _stops
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
_t = query.render_times("玉林市", "G02")
assert _t.count("首班") == 2 and "07:45" in _t and "乙站 → 甲站" in _t, _t
assert query.render_times("玉林市", "999") is None
query._catalog, query._first_last = _cat, _fl

# 实例是 UTC：时钟要按北京时间报（23:37 那种是 UTC，实际 07:37）
import time as _tm
_utc = _tm.time
query.time.time = lambda: 1745000000.0          # 某个 UTC 时刻
assert len(query._clock(0)) == 5 and query._clock(0) == \
    _tm.strftime("%H:%M", _tm.gmtime(1745000000.0 + 8 * 3600)), query._clock(0)
assert query._clock(1500) == _tm.strftime("%H:%M", _tm.gmtime(1745000000.0 + 8 * 3600 + 1500 * 60))
query.time.time = _utc

# 「G02到高新产业园，去玉林北站」：上车站是产业园，玉林北站只是方向，不是上车站
_fs = query.find_stop
query.find_stop = lambda c, raw: ({"stationName": raw} if raw in ("高新产业园", "玉林北站") else None, raw)
assert query.pick_stop("玉林市", "你看一下G02到高新产业园，去玉林北站要等多久？") == "高新产业园"
assert query._dest_after("玉林市", "你看一下G02到高新产业园，去玉林北站要等多久？",
                         "高新产业园") == "玉林北站"
assert query._dest_after("玉林市", "我在玉林北站，G02现在到那里我要多少分钟", "玉林北站") is None
query.find_stop = _fs

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
                                   ((13, 0), "17:47", "23:10", True),
                                   ((0, 30), "22:00", "01:00", False)]:
    _at(*_now)
    assert query._past_last(_last, _first) is _want, (_now, _first, _last, query._past_last(_last, _first))
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

print("selftest ok")

if "--live" in sys.argv:
    import importlib

    importlib.reload(query)  # 拿掉上面被替换掉的假城市表
    for msg in ["玉林 G02 路有没有车", "香港 1 路还有几站", "大连 2 路到哪了",
                "玉林 从十字街到火车站坐什么车", "南宁 我在金湖广场，16路还有几站",
                "南宁 金湖广场有几路车", "容县 11 路有车吗", "玉林 6 路夜班还有几站"]:
        print("\n问:", msg)
        print(query.answer(msg, "玉林市"))
