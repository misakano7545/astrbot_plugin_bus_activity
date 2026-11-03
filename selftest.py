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

# 从消息里取线路号 + 紧贴着的汉字段（县内线路用）
assert query.parse("玉林 G02 路有没有车") == ("G02", "玉林")   # 前缀也可能是城市，由 detect_city 分辨
assert query.parse("B1 有车吗") == ("B1", "")
assert query.parse("105路还有几站") == ("105", "")
assert query.parse("陆川 1 路有车吗") == ("1", "陆川")
assert query.parse("早上8点有车吗") is None  # 裸数字不带「路」不算线路

# 目录匹配：全等优先，前缀只在候选少时接受
cat = [("6路", "a"), ("6路夜班", "b")]
assert query._pick(cat, "6") == [("6路", "a")]
assert query._pick(cat, "9") == []
assert query._pick([("B11", 1), ("B112", 2), ("B113", 3)], "B11")[0][0] == "B11"

# 排版
res = {"source": "掌上公交", "line": "1路",
       "groups": [("A站", [("桂A1", "某路口", 326)]), ("B站", [])]}
out = query.render("南宁", "1", res)
assert out.splitlines() == ["南宁 1路 现在 1 辆车在路上（掌上公交）",
                            "往A站：桂A1 某路口 326m",
                            "往B站：暂无车"], out
assert "现在没车" in query.render("南宁", "1", {"source": "车来了", "line": "1", "groups": [("", [])]})

# 城市识别：给假城市表，不联网
query.cities = lambda: {"玉林": {"mygolbs": "玉林市"}, "南宁": {"mygolbs": "南宁市"}}
assert query.detect_city("玉林 G02 路有没有车", "", "南宁市") == ("玉林", True)
assert query.detect_city("2 路有车吗", "", "南宁市") == ("南宁", False)   # 没写城市用默认
assert query.detect_city("火星 1 路有车吗", "火星", "南宁市") == (None, False)  # 认不出就别瞎答

print("selftest ok")

if "--live" in sys.argv:
    import importlib

    importlib.reload(query)  # 拿掉上面被替换掉的假城市表
    for msg in ["玉林 G02 路有没有车", "香港 1 路还有几站", "大连 2 路到哪了"]:
        print("\n问:", msg)
        print(query.answer(msg, "玉林市"))
