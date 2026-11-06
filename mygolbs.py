"""掌上公交（mygolbs）H5 接口：城市表、目录、站表、经停线路、实时车、换乘、公司电话。

单独成文件是为了别的组件 import 就能用：不依赖 AstrBot，也不依赖本插件任何模块。
协议细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md。

三个坑：
  Origin 必须是 https://h5.mygolbs.com（无尾斜杠），错了上游回 HTTP 200 + 58 字节占位串。
  业务失败也是 HTTP 200，status 是字符串 "-1"/"-2"，不能当成「没这条线」。
  LINENAME 必须带「路」。
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://h5.mygolbs.com/ApiData.do"
HEADERS = {
    "Origin": "https://h5.mygolbs.com",
    "Referer": "https://h5.mygolbs.com/",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 12) Mobile Safari/537.36",
}
CITY_TTL, CATALOG_TTL, LIVE_TTL = 6 * 3600, 3600, 20
CACHE_MAX = 2048

# 两个源共用的 TTL 缓存，key 是 (kind, key)，别一个 kind 存两种形状
cache = {}
# 最近一次连不上上游的时刻（秒）。调用方靠它决定要不要回一句「过会儿再问」，别静默挂住
last_fail = [0.0]

# 带方向、不带方向的命令都在这里；加新命令照这个形状写，别在调用方拼参数
def _cmd(code, **kw):
    p = {"CMD": code}
    p.update({k: v for k, v in kw.items() if v is not None})
    return p


def cached(kind, key, ttl, fn):
    hit = cache.get((kind, key))
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    # 网络抖动别进缓存：catalog 一挂就是一小时查不到车，全是这条缓存留的
    if isinstance(val, dict) and val.get("error") and "detail" in val:
        return val
    # 过期键长期占位会让 cache 一直涨，顺手清掉同一个 kind 下的过期项，超上限再整体淘汰
    if len(cache) >= CACHE_MAX:
        now = time.time()
        # list() 拿快照：换乘/单向判定是八线程并发，边迭代边插会炸「dictionary changed size」
        for k in [k for k, (t, _) in list(cache.items()) if now - t >= CITY_TTL]:
            cache.pop(k, None)
    cache[(kind, key)] = (time.time(), val)
    return val


def post(params, timeout=8):
    """POST 一条命令 → dict。失败也返回 dict（status 非 "1"），调用方按 status/error 判。"""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 缺 CITYNAME 或线路名不存在时上游回的是 HTML 错误页，不是 JSON
        return {"status": e.code, "msg": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        last_fail[0] = time.time()
        # 连不上/握手超时别把异常抛给调用方；detail 只留给人看，别发给用户
        return {"status": -9, "msg": "上游这会儿连不上，过一会儿再问一句",
                "error": True, "detail": str(e)}
    text = raw.lstrip()
    if not text.startswith("{"):
        # 58 字节令牌 = Origin 头不对，前置分流层直出的占位串
        return {"status": -9, "msg": "非 JSON 响应，检查 Origin 头"}
    try:
        data = json.loads(text)
    except ValueError as e:
        return {"status": -9, "msg": f"JSON 解析失败 {e}"}
    if not isinstance(data, dict):
        return {"status": -9, "msg": "响应不是对象"}
    if str(data.get("status")) != "1":
        return {"status": data.get("status"), "msg": data.get("msg") or "", "error": True}
    return data


def city_names():
    """CMD=101 全国城市表 → 平台原名列表（579 城）。"""
    def build():
        try:
            return [r.get("cityname") for r in post(_cmd("101")).get("data") or []
                    if r.get("cityname")]
        except Exception:
            return []      # 城市表拉不到就只用车来了那份，不阻断查询
    return cached("cities-101", "", CITY_TTL, build)


def catalog(city_name):
    """CMD=119 整城线路目录。一个方向一条记录（buslines[].lineName/to/upperOrDown）。"""
    return cached("catalog-my", city_name, CATALOG_TTL,
                  lambda: post(_cmd("119", CITYNAME=city_name)))


def stops(city_name, keyword):
    """CMD=102 站点搜索：带 lat/lng 和经停线路 relatedRoutes，唯一能按站名反查线路的命令。"""
    return cached("stop", (city_name, keyword), CATALOG_TTL,
                  lambda: post(_cmd("102", CITYNAME=city_name, KEYWORD=keyword))
                  .get("busstations") or [])


def seq_payload(city_name, line, direction):
    """CMD=103 整包（站序 + 首末班 + 票价的 commonts），站表/首末班/票价共用这一份缓存。"""
    return cached("firstlast", (city_name, line, direction), CATALOG_TTL,
                  lambda: post(_cmd("103", CITYNAME=city_name, LINENAME=line,
                                    DIRECTION=direction)))


def stations(city_name, line, direction):
    """站序（0 基序号看 stationOrder - 1）。"""
    return seq_payload(city_name, line, direction).get("data") or []


def first_last(city_name, line, direction):
    """(首班, 末班)，取不到给空串。方向 1/2 各有一份，别混着用。"""
    fl = seq_payload(city_name, line, direction).get("firstLast") or []
    return (min((x.get("first") or "" for x in fl), default=""),
            max((x.get("last") or "" for x in fl), default=""))


def price(city_name, line):
    """票价只藏在 103 的 commonts 那句话里，各家写法不一样：
    「票价：2元/人次」「票价3元」「单程票价4元一票制。石家庄公交…」「票价2元。\\n热线…」
    shortcut: 只认带「元」的阿拉伯数字写法，中文数字票价（「票价一元」）会漏；只取方向 1，
    分段计价的线会不准。遇到了再补。
    """
    m = re.search(r"票价[:：]?\s*([^。\n；;]{1,30})",
                  seq_payload(city_name, line, "1").get("commonts") or "")
    v = re.split(r"[\s，,（(]", (m.group(1) if m else "").strip())[0]
    return v if "元" in v else ""


def live(city_name, line, direction, order=1):
    """CMD=104 实时整包。order > 0 时上游给「到这个站」自己的预测
    （routeOnStationRTimeInfoList），每站最多 2 辆——比自己拿 dislist/speedlist 估准。"""
    return cached("live-my", (city_name, line, direction, order), LIVE_TTL,
                  lambda: post(_cmd("104", CITYNAME=city_name, LINENAME=line,
                                    DIRECTION=direction, STATIONORDER=str(order))))


def lines(city_name, keyword=""):
    """CMD=114 线路搜索的 buslines[]（routeBean 带 company/companyMobile）。KEYWORD 空 = 全目录。"""
    return cached("lines-114", (city_name, keyword), CATALOG_TTL,
                  lambda: post(_cmd("114", CITYNAME=city_name,
                                    KEYWORD=keyword)).get("buslines") or [])


def transfer(city_name, a, b):
    """CMD=111 换乘方案：两次换乘也排、按总时长排序。坐标留空，按站名查就够。"""
    def ask():
        d = post(_cmd("111", CITYNAME=city_name, STARTPOINTNAME=a, STARTPOINTLNG="",
                      STARTPOINTLAT="", ENDPOINTNAME=b, ENDPOINTLNG="", ENDPOINTLAT=""))
        info = d.get("info") if isinstance(d, dict) else None
        return info if isinstance(info, list) else []
    return cached("plan", f"{city_name}|{a}|{b}", 60, ask)
