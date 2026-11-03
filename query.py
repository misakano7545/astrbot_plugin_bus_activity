"""全国公交实时查询：掌上公交优先，车来了兜底。

只问「有没有车、车到哪」，不做换乘、不做历史。
接口细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md 与 docs/车来了API.md。
"""

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:  # AstrBot 按包加载时走相对导入，单文件调试时退回顶层模块
    from . import chelaile
except ImportError:
    import chelaile

HERE = Path(__file__).parent
CHELAILE_CITIES = json.loads((HERE / "cities.json").read_text("utf-8"))

API = "https://h5.mygolbs.com/ApiData.do"
HEADERS = {
    "Origin": "https://h5.mygolbs.com",
    "Referer": "https://h5.mygolbs.com/",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 12) Mobile Safari/537.36",
}
CITY_TTL, CATALOG_TTL, LIVE_TTL = 6 * 3600, 3600, 20
LABEL = {"mygolbs": "掌上公交", "chelaile": "车来了"}
_cache = {}

# 「玉林 G02 路」「B1 有车吗」「105路」都认；裸数字必须带「路」，防「8点」误判
LINE_RE = re.compile(r"(?P<alpha>[A-Za-z]{1,2}\d{1,3})(?:\s*路)?|(?P<num>\d{1,3})\s*路")
INTENT = ("车", "到哪", "几站", "还有", "来了", "多久", "快吗", "有没有")
SUFFIX = re.compile(r"(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|自治县|地区|盟|市|城区|区|县)$")


def norm_line(name):
    s = re.sub(r"[（(].*?[)）]", "", name or "")
    s = re.sub(r"\s+", "", s)
    return re.sub(r"(线路|路|线)", "", s).upper()


def norm_city(name):
    return SUFFIX.sub("", (name or "").strip())


def parse(msg):
    """→ (线路名归一化 or None)"""
    m = LINE_RE.search(msg)
    if not m:
        return None
    return norm_line(m.group("alpha") or m.group("num"))


def _cached(kind, key, ttl, fn):
    hit = _cache.get((kind, key))
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[(kind, key)] = (time.time(), val)
    return val


def _post(params, timeout=8):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def cities():
    """归一化城市名 → {"mygolbs": 平台原名, "chelaile": cityId}"""
    def build():
        idx = {}
        for name, cid in CHELAILE_CITIES.items():
            idx.setdefault(norm_city(name), {})["chelaile"] = cid
        try:
            for row in _post({"CMD": "101"}).get("data") or []:
                name = row.get("cityname") or ""
                if name:
                    idx.setdefault(norm_city(name), {})["mygolbs"] = name
        except Exception:
            pass  # 掌上城市表拉不到就只用车来了那份，不阻断查询
        return idx
    return _cached("cities", "", CITY_TTL, build)


def detect_city(msg, default_city):
    """消息里出现的最长城市名；没写就用默认城市。"""
    hits = [k for k in cities() if len(k) >= 2 and k in msg]
    if hits:
        return max(hits, key=len)
    key = norm_city(default_city)
    return key if key in cities() else None


def _pick(catalog, want):
    """目录里挑线路：先全等，再唯一前缀。catalog 元素是 (显示名, 原样对象)。"""
    exact = [c for c in catalog if norm_line(c[0]) == want]
    if exact:
        return exact
    near = [c for c in catalog if norm_line(c[0]).startswith(want)]
    return near if len(near) < 3 else []


def _mygolbs(city_name, want):
    rows = _cached("catalog-my", city_name, CATALOG_TTL,
                   lambda: _post({"CMD": "119", "CITYNAME": city_name}).get("buslines") or [])
    hit = _pick([(r["lineName"], r) for r in rows], want)
    if not hit:
        return None
    groups = []
    for name, row in hit:
        d = row["upperOrDown"]
        cars = _cached("live-my", (city_name, name, d), LIVE_TTL,
                       lambda name=name, d=d: _post({"CMD": "104", "CITYNAME": city_name,
                                                     "LINENAME": name, "DIRECTION": d,
                                                     "STATIONORDER": "1"}).get("list") or [])
        groups.append((row.get("to") or "", [(c.get("busNumber"), c.get("stationName"),
                                              c.get("busToStationNiheDistance")) for c in cars]))
    return {"source": "掌上公交", "line": hit[0][1]["lineName"], "groups": groups}


def _chelaile(city_id, want):
    rows = _cached("catalog-cl", city_id, CATALOG_TTL, lambda: chelaile.lines(city_id))
    hit = _pick(rows, want)
    if not hit:
        return None
    name, line_id = hit[0]
    d = _cached("live-cl", (city_id, line_id), LIVE_TTL, lambda: chelaile.live(city_id, line_id))
    # state=-1「等待发车」、-3「已收班」都可能是该线当时没车，照实报
    return {"source": "车来了", "line": name, "groups": [("", d["buses"])]}


def query(city_key, want):
    """两家依次试。有车就立刻用，两家都空车才算没车——否则会漏掉另一家有车的城。"""
    ent = cities().get(city_key) or {}
    tried, blank = [], None
    for source, fetch in (("mygolbs", _mygolbs), ("chelaile", _chelaile)):
        target = ent.get(source)
        if not target:
            continue
        try:
            got = fetch(target, want)
        except Exception as e:
            tried.append(f"{LABEL[source]}异常 {e}")
            continue
        if got and any(group for _, group in got["groups"]):
            return got
        if got:
            blank = blank or got
        else:
            tried.append(f"{LABEL[source]}没这条线")
    if blank:
        return blank
    return {"error": "；".join(tried) or "两家都没有这个城市"}


def _car(plate, stn, dist):
    try:
        dist = float(dist or 0)
    except (TypeError, ValueError):
        dist = 0
    if dist <= 0:
        return f"{plate} {stn}"
    return f"{plate} {stn} {dist / 1000:.1f}km" if dist >= 1000 else f"{plate} {stn} {round(dist)}m"


def render(city_key, want, res):
    if "error" in res:
        return f"查不到「{want}」：{res['error']}"
    total = sum(len(b) for _, b in res["groups"])
    if not total:
        return f"{city_key} {res['line']} 现在没车在路上（{res['source']}）"
    rows = [f"{city_key} {res['line']} 现在 {total} 辆车在路上（{res['source']}）"]
    for label, buses in res["groups"]:
        head = f"往{label}：" if label else ""
        rows.append(head + ("｜".join(_car(*b) for b in buses) if buses else "暂无车"))
    return "\n".join(rows)


def answer(msg, default_city):
    want = parse(msg)
    if not want:
        return None
    city_key = detect_city(msg, default_city)
    if not city_key:
        return f"没认出城市，说清楚一点，比如「{default_city} {want} 路有没有车」"
    return render(city_key, want, query(city_key, want))
