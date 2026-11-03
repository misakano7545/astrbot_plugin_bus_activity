"""全国公交实时查询：掌上公交优先，车来了兜底。

只问「有没有车、车到哪」，不做换乘、不做历史。
接口细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md 与 docs/车来了API.md。
"""

import json
import re
import time
import urllib.error
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
# Origin 是唯一命门：末尾多一个斜杠、换成 http、或干脆不带，上游都回 HTTP 200 +
# 恒定 58 字节串 0vx0czK73ykwCHaLBTiKh0iCXQINzRvSZw4+b/H8FgE52U=，症状是「永远查不到」。
HEADERS = {
    "Origin": "https://h5.mygolbs.com",
    "Referer": "https://h5.mygolbs.com/",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 12) Mobile Safari/537.36",
}
CITY_TTL, CATALOG_TTL, LIVE_TTL = 6 * 3600, 3600, 20
CACHE_MAX = 2048
LABEL = {"mygolbs": "掌上公交", "chelaile": "车来了"}
_cache = {}

# 「玉林 G02 路」「B1 有车吗」「105路」都认；裸数字必须带「路」，防「8点」误判
# 「路」后面紧跟的定性词（夜班/专线）得一起吃掉，否则「6路夜班」会被截成「6」
LINE_RE = re.compile(
    r"(?P<alpha>[A-Za-z]{1,2}\d{1,3})(?:\s*路)?"
    r"|(?P<num>\d{1,3})\s*路(?P<qual>夜班)?"
)
PREFIX_RE = re.compile(r"([\u4e00-\u9fa5]{1,4})\s*$")
INTENT = ("车", "到哪", "几站", "还有", "来了", "多久", "快吗", "有没有",
          "坐什么", "怎么坐", "怎么去", "换乘", "几路",
          "在哪", "实时", "几辆", "几点", "末班", "首班", "怎么样", "到站")
SUFFIX = re.compile(r"(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|自治县|地区|盟|市|城区|区|县)$")
# 剥完不足两字就原样保留：容县/威县/沙县/沛县/丰县 削成单字会在索引里变成
# 威/沙/沛/容/丰 这些键，既不精确又被 detect_city 的 len(k) >= 2 滤掉（见 issue #1）；
# 「长宁县」照样能收成「长宁」，用户写简称也查得到。
MIN_CITY_KEY = 2


def norm_line(name):
    s = re.sub(r"[（(].*?[)）]", "", name or "")
    s = re.sub(r"\s+", "", s)
    return re.sub(r"(线路|路|线)", "", s).upper()


def norm_city(name):
    s = (name or "").strip()
    short = SUFFIX.sub("", s)
    # 剥完不足两个字说明这个缀本来就是名字的一部分，原样保留
    return short if len(short) >= MIN_CITY_KEY else s


def parse(msg):
    """→ (线路号, 紧贴着的汉字段)。县内线路「陆川1路」靠这个前缀区分。"""
    m = LINE_RE.search(msg)
    if not m:
        return None
    pre = PREFIX_RE.search(msg[: m.start()])
    num = (m.group("alpha") or m.group("num")) + (m.group("qual") or "")
    return norm_line(num), (pre.group(1) if pre else "")


def _cached(kind, key, ttl, fn):
    hit = _cache.get((kind, key))
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    # 过期键长期占位会让 _cache 一直涨，顺手清掉同一个 kind 下的过期项，超上限再整体淘汰
    if len(_cache) >= CACHE_MAX:
        now = time.time()
        for k in [k for k, (t, _) in _cache.items() if now - t >= CITY_TTL]:
            _cache.pop(k, None)
    _cache[(kind, key)] = (time.time(), val)
    return val


def _post(params, timeout=8):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 缺 CITYNAME 或线路名不存在时上游回的是 HTML 错误页，不是 JSON
        return {"status": e.code, "msg": f"HTTP {e.code}"}
    text = raw.lstrip()
    if not text.startswith("{"):
        # 58 字节令牌 = Origin 头不对，前置分流层直出的占位串
        return {"status": -9, "msg": "非 JSON 响应，检查 Origin 头"}
    try:
        data = json.loads(text)
    except ValueError as e:
        return {"status": -9, "msg": f"JSON 解析失败 {e}"}
    return data if isinstance(data, dict) else {"status": -9, "msg": "响应不是对象"}


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


def detect_city(msg, prefix, default_city):
    """→ (城市键, 是否明写)。明写了才算数，防止「火星 1 路」被当成本地线路瞎答。"""
    idx = cities()
    hits = [k for k in idx if k in msg]          # 含 1 字城名，够长才用
    hits = [k for k in hits if len(k) >= 2 or prefix.startswith(k)]
    if hits:
        return max(hits, key=len), True
    if prefix and prefix in idx:
        return prefix, True
    key = norm_city(default_city)
    if key not in idx:
        return None, False
    # 线路号前面还有串认不出的汉语（「火星」「陆川」），别默默当成本地线路
    if prefix and prefix != key and not key.startswith(prefix):
        return None, False
    return key, False


def _pick(catalog, want):
    """目录里挑线路：先全等，再唯一前缀，最后按短优先消歧。

    前缀候选 ≥ 3 个时不再直接放弃：11/12/13 路同时在的城市问「1」确实没法唯一定，
    但 B12/B13/B14 之外问「B1」应该命中「B1」本身，短的那个优先（见 issue #3）。
    catalog 元素是 (显示名, 原样对象)。
    """
    exact = [c for c in catalog if norm_line(c[0]) == want]
    if exact:
        return exact
    near = [c for c in catalog if norm_line(c[0]).startswith(want)]
    if not near:
        return []
    if len(near) < 3:
        return near
    # 候选太多：先看有没有名字长度刚好比 want 长一位的（1 → 11路），否则给用户看候选
    near.sort(key=lambda c: len(norm_line(c[0])))
    shortest = len(norm_line(near[0][0]))
    tied = [c for c in near if len(norm_line(c[0])) == shortest]
    return tied if len(tied) < 3 else []


def _pick_city_line(catalog, want, city_name):
    """县内线路在目录里带县名前缀（容县11路），裸号「11」也要能命中。"""
    hit = _pick(catalog, want)
    if hit:
        return hit
    pre = norm_city(city_name).upper()
    if pre and pre != want:
        return _pick([(n[len(pre):] if norm_line(n).startswith(pre) else n, o)
                      for n, o in catalog if norm_line(n).startswith(pre)], want)
    return []


def _last_bus(city_name, line, direction):
    """末班时刻 HH:MM，取不到返回空。只在整车数为 0 时调用，回答「是不是收班了」。"""
    r = _cached("firstlast", (city_name, line, direction), CATALOG_TTL,
                lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                               "DIRECTION": direction}))
    return max((x.get("last") or "" for x in r.get("firstLast") or []), default="")


def _live_my(city_name, name, d):
    """CMD=104 整包：list 是车，dislist/speedlist 是每段里程与均速（m/min），算分钟要用。"""
    return _cached("live-my", (city_name, name, d), LIVE_TTL,
                   lambda: _post({"CMD": "104", "CITYNAME": city_name, "LINENAME": name,
                                  "DIRECTION": d, "STATIONORDER": "1"}))


def _eta(payload, j, bus):
    """车还有几分钟到第 j 站（0 基）。车速表单位是 m/min（实测与 GPS 位移吻合）。"""
    i = int(bus.get("index") or 0)
    if i > j:
        return None                      # 已经过站了
    dis = [x.get("d") or 0 for x in payload.get("dislist") or []]
    sp = [x.get("speed") or 0 for x in payload.get("speedlist") or []]
    leg = lambda k: dis[k] / sp[k] if k < len(dis) and k < len(sp) and sp[k] else 0
    v0 = sp[i - 1] if 0 < i <= len(sp) else (sp[0] if sp else 0)
    t = float(bus.get("busToStationNiheDistance") or 0) / v0 if v0 else 0
    return t + sum(leg(k) for k in range(i, j))


def _mygolbs(city_name, want):
    rows = _cached("catalog-my", city_name, CATALOG_TTL,
                   lambda: _post({"CMD": "119", "CITYNAME": city_name}).get("buslines") or [])
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    if not hit:
        # 没有就报候选，别让「沛县 1 路」这种（只有 11/17/18 路）答成「没这条线」
        near = sorted({r["lineName"] for r in rows
                       if norm_line(r["lineName"]).startswith(want)})[:4]
        return {"error": "是 " + " / ".join(near) + " 吗"} if near else None
    groups, live_known, last = [], False, ""
    for name, row in hit:
        d = str(row.get("upperOrDown") or "")
        # 传 0/3/空串时服务端会静默退化成方向 1，回一列错方向的车，宁可不报
        if d not in ("1", "2"):
            continue
        payload = _live_my(city_name, name, d)
        cars = payload.get("list") or []
        # hasReal=1 且回的就是「获取实时数据成功」说明上游认为线路在运营，只是这一趟没有车报 GPS
        if payload.get("hasReal") in ("1", 1) or "成功" in str(payload.get("msg") or ""):
            live_known = True
        if not cars:
            last = _last_bus(city_name, name, d) or last
        groups.append((row.get("to") or "", [(c.get("busNumber"), c.get("stationName"),
                                              c.get("busToStationNiheDistance")) for c in cars]))
    note = ""
    if not any(g for _, g in groups):
        if last and time.strftime("%H:%M") > last:
            note = f"末班 {last} 已经开走了"
        elif live_known:
            note = "线路还在跑，这会儿没车开车机"
    return {"source": "掌上公交", "line": hit[0][1]["lineName"], "groups": groups, "note": note}


def _chelaile(city_id, want):
    rows = _cached("catalog-cl", city_id, CATALOG_TTL, lambda: chelaile.lines(city_id))
    hit = _pick(rows, want)
    if not hit:
        return None
    name = hit[0][0]
    # 目录是方向级的，同一条线上下行是两个 lineId，全都查一遍再合并（见 issue #6）。
    # 但香港这种一个名字对应多家公司（九巴/城巴/屿巴 都叫「1」），别把别人的车算进来，
    # 所以只取排在前面的那一条线路的方向对。
    ids = [lid for n, lid in hit if n == name][:2]
    groups, desc = [], ""
    for line_id in ids:
        d = _cached("live-cl", (city_id, line_id), LIVE_TTL, lambda i=line_id: chelaile.live(city_id, i))
        # state=-1 等待发车、-2 临时停运、-3 末班已过，desc 是现成中文文案
        desc = desc or d.get("desc") or ""
        groups.append((d.get("to") or "", d["buses"]))
    return {"source": "车来了", "line": name, "groups": groups, "desc": desc}


def query(city_key, wants):
    """两家依次试。有车就立刻用，两家都空车才算没车——否则会漏掉另一家有车的城。

    wants 是候选线路名，先试带前缀的「陆川1」，再试裸号「1」。
    """
    ent = cities().get(city_key) or {}
    tried, blank, near = [], None, None
    for want in wants:
        for source, fetch in (("mygolbs", _mygolbs), ("chelaile", _chelaile)):
            target = ent.get(source)
            if not target:
                continue
            try:
                got = fetch(target, want)
            except Exception as e:
                tried.append(f"{LABEL[source]}异常 {e}")
                continue
            if got and "error" in got:
                if got["error"].startswith("是 "):
                    near = near or got      # 目录里有近似线名，另一家也许真有这条，先记着
                else:
                    tried.append(got["error"])
                continue
            if got and any(group for _, group in got["groups"]):
                return got
            if got:
                blank = blank or got
        if blank:
            return blank           # 有这条线，只是当下两端都没车
    if not ent:
        # 城市表里没这个键：要么名字写错，要么两家都不覆盖，别把话说成「两家都没有」
        return {"error": "这个城市名没认出来，写全称或把城市补在路线前面试试"}
    if near:
        return near
    if tried and all("异常" in t for t in tried):
        return {"error": "上游这会儿没响应，过会儿再问"}
    return {"error": "没这条线路"}


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
        e = res["error"]
        if e.startswith("是 "):                       # 目录里有近似线名，报候选
            return f"没「{want}」这条，是 {e[2:]}"
        if e.startswith("没这条"):
            return f"没查到「{want}」这条线"
        return e                                      # 其余文案本身已经是人话
    total = sum(len(b) for _, b in res["groups"])
    line = res["line"]
    if not total:
        # 「没车」和「上游没给车辆 GPS」是两件事，后者会被乘客读成停运（见 issue #2）
        tail = res.get("note") or res.get("desc") or ""
        return f"{line} 这会儿没车" + ("，" + tail if tail else "")
    multi = sum(1 for _, b in res["groups"] if b) > 1      # 只有一个方向有车时不念空方向
    rows = [f"有，{line} 现在 {total} 辆在路上"]
    for label, buses in res["groups"]:
        if not buses:
            if multi and label:
                rows.append(f"往{label}还没车")
            continue
        rows.append((f"往{label}：" if label else "") + "，".join(_car(*b) for b in buses))
    return "\n".join(rows)


HOP_RE = re.compile(r"从\s*([^，,。？?！!\s]{2,12}?)\s*到\s*([^，,。？?！!\s]{2,12})")
# 「火车站坐什么车」要把尾巴砍掉再当站名查；「我在十字街」要把脑袋砍掉
TAIL = re.compile(r"(怎么坐|坐什么|怎么去|有什么|有哪几|几路车|坐几路|怎么走|咋坐|咋去|换乘|有直达|直达|还有|吗|呢|啊).*$")
LEAD = re.compile(r"^(我?在|我?从|去|到|等|坐)")


def stops(city_name, keyword):
    """CMD=102 站点搜索：带 lat/lng 和经停线路，是唯一能按站名反查线路的命令。"""
    return _cached("stop", (city_name, keyword), CATALOG_TTL,
                   lambda: _post({"CMD": "102", "CITYNAME": city_name,
                                  "KEYWORD": keyword}).get("busstations") or [])


def find_stop(city_name, raw):
    """站名从长到短试，命中即止——用户嘴里不会带括号后缀，得让平台的站名去对。"""
    for n in (8, 6, 5, 4, 3, 2):
        if len(raw) >= n:
            rows = stops(city_name, raw[:n])
            if rows:
                return rows[0], raw[:n]
    return None, raw


def hop(msg):
    """→ (出发站, 到达站)。只认「从X到Y」，别的句式不猜。"""
    m = HOP_RE.search(msg)
    return (m.group(1), TAIL.sub("", m.group(2))) if m else None


def pick_stop(city_name, msg):
    """从整句里抠站名：按标点切片，逐片砍掉「我在/坐什么」再拿去搜。"""
    city = norm_city(city_name)
    for part in re.split(r"[，,。.？?！!、\s]+", msg):
        cand = LEAD.sub("", TAIL.sub("", part))
        if len(cand) < 2 or norm_city(cand) == city or LINE_RE.search(cand):
            continue                        # 城市名和线路号不是站名
        got, _ = find_stop(city_name, cand)
        if got:
            return got["stationName"]
    return None


def _station_seq(city_name, line, direction):
    return _cached("stations", (city_name, line, direction), CATALOG_TTL,
                   lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                                  "DIRECTION": direction}).get("data") or [])


def _goes(city_name, line, a, b):
    """A 站是否在 B 站之前。返回 (方向, A 站序, B 站序)，都是 0 基；不顺路就 None。"""
    for d in ("1", "2"):
        names = [r.get("stationName") or "" for r in _station_seq(city_name, line, d)]
        ia = next((i for i, n in enumerate(names) if a in n), None)
        ib = next((i for i, n in enumerate(names) if b in n), None)
        if ia is not None and ib is not None and ia < ib:
            return d, ia, ib
    return None


def _hop_line(city_name, line, d, ia, ib):
    """这一路下一辆车几分钟到 A、从 A 坐到 B 要几分钟。"""
    payload = _live_my(city_name, line, d)
    best = None
    for car in payload.get("list") or []:
        t = _eta(payload, ia, car)
        if t is None:
            continue
        if best is None or t < best[0]:
            best = (t, _eta(payload, ib, car))
    if not best:
        last = _last_bus(city_name, line, d)
        if last and time.strftime("%H:%M") > last:
            return f"，末班 {last} 已经开走了"
        return "，这会儿没车报位置"
    wait = max(1, round(best[0]))
    ride = max(1, round(best[1] - best[0]))
    return f"，下一辆约 {wait} 分钟后到，坐过去 {ride} 分钟"


def render_hop(city_key, city_name, raw):
    """A 站到 B 站：拿两站的经停线路求交，再按站序验方向，验不过的不算直达。"""
    a, a_name = find_stop(city_name, raw[0])
    b, b_name = find_stop(city_name, raw[1])
    if not a or not b:
        return None
    ra = set(a.get("relatedRoutes") or [])
    rb = set(b.get("relatedRoutes") or [])
    head = f"从{a['stationName']}到{b['stationName']}"
    if not ra & rb:
        short = lambda s: "、".join(sorted(s)[:6]) + ("…" if len(s) > 6 else "")
        return f"{head}没直达车，{a['stationName']} 有 {short(ra)}，{b['stationName']} 有 {short(rb)}"
    direct = []
    for line in sorted(ra & rb)[:4]:            # shortcut: 只验前 4 条，交叉线多的城可能漏
        got = _goes(city_name, line, a_name, b_name)
        if got:
            direct.append((line, got))
    if not direct:
        return f"{head}有同名线路但方向反了，得换乘"
    if len(direct) == 1:
        line, got = direct[0]
        return f"{head}坐 {line} 直达（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got)
    rows = [f"{head}能直达："]
    for line, got in direct:
        rows.append(f"{line}（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got))
    return "\n".join(rows)


def _left(seq_index, car_index):
    """站序是 1 基、车辆 index 是 0 基，差几站 = 站序 - (index+1)。"""
    return seq_index - (int(car_index or 0) + 1)


def _clock(mins):
    return time.strftime("%H:%M", time.localtime(time.time() + mins * 60))


def render_at(city_key, city_name, want, msg):
    """「我在 X 站，N 路还有几站」：按方向给最近一辆的到站时刻和分钟。"""
    station_name = pick_stop(city_name, msg)
    if not station_name:
        return None
    rows = _cached("catalog-my", city_name, CATALOG_TTL,
                   lambda: _post({"CMD": "119", "CITYNAME": city_name}).get("buslines") or [])
    hit = _pick([(r["lineName"], r) for r in rows], want)
    out = []
    for line_name, row in hit:
        d = row["upperOrDown"]
        names = [s.get("stationName") or "" for s in _station_seq(city_name, line_name, d)]
        idx = next((i + 1 for i, n in enumerate(names) if station_name in n), None)
        if not idx:
            continue
        payload = _live_my(city_name, line_name, d)
        cars = []
        for c in payload.get("list") or []:
            left = _left(idx, c.get("index"))
            if left < 0:
                continue
            mins = _eta(payload, idx - 1, c)
            cars.append((mins if mins is not None else 999, left, c.get("busNumber")))
        if not cars:
            continue
        mins, left, plate = min(cars)
        span = "马上到" if left == 0 else f"还有 {left} 站"
        if mins < 999:
            span = f"约 {_clock(mins)} 到（{max(1, round(mins))} 分钟），" + span
        out.append(f"往{row.get('to') or '?'}：{plate} {span}")
    if not out:
        return None
    return f"{hit[0][1]['lineName']} 到{station_name}：" + "；".join(out)


def render_stop(city_key, city_name, msg):
    """「X 站有哪几路」：站点经停线路，每条带首末站。"""
    name = pick_stop(city_name, msg)
    if not name:
        return None
    routes = (stops(city_name, name) or [{}])[0].get("relatedRoutes") or []
    if not routes:
        return None
    rows = _cached("catalog-my", city_name, CATALOG_TTL,
                   lambda: _post({"CMD": "119", "CITYNAME": city_name}).get("buslines") or [])
    ends = {}
    for r in rows:
        ends.setdefault(r["lineName"], []).append(f"{r.get('from')} ↔ {r.get('to')}")
    items = [f"{n} {ends[n][0]}" if ends.get(n) else n for n in routes if ends.get(n)] or list(routes)
    head = f"{name}有 {len(items)} 条线路："
    return head + "".join("\n" + i for i in items[:12]) + ("\n…还有 " + str(len(items) - 12) + " 条" if len(items) > 12 else "")


def answer(msg, default_city):
    line = parse(msg)
    prefix = line[1] if line else ""
    city_key, explicit = detect_city(msg, prefix, default_city)
    base = norm_city(default_city)
    if not city_key:
        # 认不出的前缀多半是县内线路名（陆川1路），在默认城市目录里再找一次
        if line and prefix and base in cities():
            res = query(base, [prefix + line[0]])
            if "error" not in res:
                return render(base, prefix + line[0], res)
        if hop(msg) or not prefix:      # 「从A到B」「X站有哪几路」不许城市名也能答
            city_key = base if base in cities() else None
    if not city_key:
        return f"「{prefix or '这个城市'}」是哪儿的公交？没认出来，写「{prefix} {line[0] if line else '1'} 路」这种试试"
    name = (cities().get(city_key) or {}).get("mygolbs")
    got = hop(msg)
    if got and name:
        out = render_hop(city_key, name, got)
        return out or f"没查到「{got[0]}」或「{got[1]}」这站，站名跟站牌上写的一样吗"
    if line:
        if name:
            out = render_at(city_key, name, prefix + line[0] if prefix and not explicit else line[0], msg)
            if out:
                return out
        wants = [prefix + line[0], line[0]] if prefix and not explicit else [line[0]]
        return render(city_key, wants[0], query(city_key, wants))
    if name:
        return render_stop(city_key, name, msg) or ""
    return ""
