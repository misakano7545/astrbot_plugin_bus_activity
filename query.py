"""全国公交实时查询：掌上公交优先，车来了兜底。

只问「有没有车、车到哪」，不做换乘、不做历史。
接口细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md 与 docs/车来了API.md。
"""

import difflib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
    # 真地铁也在这套目录里：「地铁10号线」「轨道交通1号线」「地铁广佛线」
    r"(?P<metro>(?:地铁|轨道交通)\s*\d+\s*号线|地铁\s*[\u4e00-\u9fa5]{1,3}线)"
    r"|(?P<alpha>[A-Za-z]{1,2}\d{1,3})(?:\s*路)?"
    r"|(?P<num>\d{1,3})\s*路(?P<qual>夜班)?"
    r"|(?P<cnnum>[一二三四五六七八九十]{1,3})\s*路"
)
PREFIX_RE = re.compile(r"([\u4e00-\u9fa5]{1,4})\s*$")
# 「八路」「十路」「二十三路」：口语报线路号不带阿拉伯数字
CN_DIGIT = {c: i for i, c in enumerate("零一二三四五六七八九", 0)}


def _cn_num(s):
    """中文数字 → 阿拉伯数字；不是中文数字返回 None。"""
    if not s or any(c not in CN_DIGIT and c not in "十百两" for c in s):
        return None
    s = s.replace("两", "二")
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_DIGIT.get(a, 1) or 1) * 10 + (CN_DIGIT.get(b, 0) if b else 0)
    n = 0
    for c in s:
        n = n * 10 + CN_DIGIT[c]
    return n
# 「用车来了看看有G02」里的「了看看有」不是县名，是动词和语气词
JUNK_HEAD = re.compile(r"^(?:车来了|掌上公交|掌上|帮我|麻烦|你|我|再|还|也|就|那|这)+")
JUNK_TAIL = re.compile(r"(?:看看|查查|看看有|看|查|有|的|了|来|台|部|辆|只|几)+$")
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
    s = re.sub(r"\s+", "", s).replace("轨道交通", "地铁")
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
    num = (m.group("alpha") or m.group("num") or m.group("metro")
           or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
    pre = JUNK_TAIL.sub("", JUNK_HEAD.sub("", pre.group(1) if pre else ""))
    return norm_line(num), pre


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
    if not isinstance(data, dict):
        return {"status": -9, "msg": "响应不是对象"}
    # 业务失败也是 HTTP 200（status 是字符串 "-1"/"-2"），别当成「没这条线路」或「没车」
    if str(data.get("status")) != "1":
        return {"status": data.get("status"), "msg": data.get("msg") or "", "error": True}
    return data


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


def _covered(raw):
    """掌上有没有这座城的数据。表外的县级市（义乌、慈溪、晋江）有，巩义、昆山没有。"""
    try:
        r = _cached("probe-city", raw, CITY_TTL, lambda: _post({"CMD": "119", "CITYNAME": raw}))
    except Exception:
        return False
    return r.get("status") == "1" and bool(r.get("buslines"))


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


# 实例跑在 UTC（/etc/timezone = Etc/UTC），报时间、判末班都得自己挪到北京时间
TZ_OFF = 8 * 3600


def _now():
    # 从 UTC 起算，不吃宿主机时区——本机是 CST、实例是 UTC，用 localtime 会差 8 小时
    return time.gmtime(time.time() + TZ_OFF)


def _past_last(last):
    """末班过了没。字符串比较跨不了零点（'00:20' < '22:40'），按分钟算。"""
    try:
        h, m = last.split(":")[:2]
        last_m = int(h) * 60 + int(m)
    except ValueError:
        return False
    t = _now()
    now_m = t.tm_hour * 60 + t.tm_min
    # 按「末班离现在多久」算，跨零点自然成立：00:02 问 22:40 → 1358 分钟前，已开走；
    # 23:00 问 00:10 → 70 分钟后，还没到。
    # shortcut: 以 12 小时为界判过去/将来，早班车式的末班（06:00）在下午会判错
    return (last_m - now_m) % (24 * 60) > 12 * 60


def _first_last(city_name, line, direction):
    """(首班, 末班)，取不到给空串。只在整车数为 0 时调用，回答「是不是收班了」。"""
    r = _cached("firstlast", (city_name, line, direction), CATALOG_TTL,
                lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                               "DIRECTION": direction}))
    fl = r.get("firstLast") or []
    return (min((x.get("first") or "" for x in fl), default=""),
            max((x.get("last") or "" for x in fl), default=""))


def _last_bus(city_name, line, direction):
    return _first_last(city_name, line, direction)[1]


def _live_my(city_name, name, d, order=1):
    """CMD=104 整包。order > 0 时上游给「到这个站」自己的预测（routeOnStationRTimeInfoList），
    每站最多 2 辆 —— 比自己拿 dislist/speedlist 估准，也更少代码。"""
    return _cached("live-my", (city_name, name, d, order), LIVE_TTL,
                   lambda: _post({"CMD": "104", "CITYNAME": city_name, "LINENAME": name,
                                  "DIRECTION": d, "STATIONORDER": str(order)}))


def _etas(payload):
    """目标站的预测里最快一辆 → (分钟, 还有几站, 车牌)。没预测返回 None。

    站已过时上游回 busToStationCount=-1、busNumber 为空，不能用（实测「已过末班时刻」那行）。
    """
    rows = [r for r in payload.get("routeOnStationRTimeInfoList") or []
            if r.get("busNumber") and int(r.get("busToStationCount") or -1) >= 0]
    if not rows:
        return None
    best = min(rows, key=lambda r: float(r.get("busToStationTime") or 0) or 1e9)
    return (float(best.get("busToStationTime") or 0),
            int(best.get("busToStationCount") or 0), best["busNumber"])


def _run(payload, ia, ib):
    """a 站到 b 站按每段均速估的分钟数（0 基站序）。上游只给到站预测，区间时长得自己累。"""
    dis = [x.get("d") or 0 for x in payload.get("dislist") or []]
    sp = [x.get("speed") or 0 for x in payload.get("speedlist") or []]
    return sum(dis[k] / sp[k] for k in range(ia, min(ib, len(sp))) if sp[k])



def _catalog(city_name):
    """CMD=119 的原始响应。同一个缓存 key 只能有一种形状——之前这里存 dict、
    别处存 buslines 列表，谁先跑谁把对方写坏（render_at 就是这样崩的）。"""
    return _cached("catalog-my", city_name, CATALOG_TTL,
                   lambda: _post({"CMD": "119", "CITYNAME": city_name}))


def _mygolbs(city_name, want):
    cat = _catalog(city_name)
    if cat.get("error"):
        return {"error": cat.get("msg") or "上游这会儿没响应"}     # 例如「当前城市暂无法提供公交查询」
    rows = cat.get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    if not hit:
        # 没有就报候选，别让「沛县 1 路」这种（只有 11/17/18 路）答成「没这条线」
        near = sorted({r["lineName"] for r in rows
                       if norm_line(r["lineName"]).startswith(want)})[:4]
        return {"error": "是 " + " / ".join(near) + " 吗"} if near else None
    groups, live_known, first, last, live_flags = [], False, "", "", []
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
        live_flags.append(str(payload.get("hasReal")) == "0")
        if not cars:
            f0, l0 = _first_last(city_name, name, d)
            first, last = first or f0, l0 or last
        groups.append((row.get("to") or "", [(c.get("busNumber"), c.get("stationName"),
                                              c.get("busToStationNiheDistance"), c.get("statusType"))
                                             for c in cars]))
    note, unknown = "", False
    if not any(g for _, g in groups):
        if live_flags and all(live_flags):
            # hasReal=0 = 上游这座城根本不给车辆位置（北京、上海、成都等），
            # 不能当成「没车」报，那是假消息（实测玉林 0 车时 hasReal 仍是 1）
            unknown, note = True, "上游对这个城市不提供实时车辆位置"
        elif last and _past_last(last):
            # 连带说首班：凌晨问日间线时，用户要知道的是「几点再来」
            note = f"末班 {last} 已经开走了" + (f"，首班 {first}" if first else "")
        elif live_known:
            note = "线路还在跑，这会儿没车开车机"
    return {"source": "掌上公交", "line": hit[0][1]["lineName"], "groups": groups,
            "note": note, "unknown": unknown}


def _chelaile(city_id, want):
    rows = _cached("catalog-cl", city_id, CATALOG_TTL, lambda: chelaile.lines(city_id))
    hit = _pick(rows, want)
    if not hit:
        return None
    name = hit[0][0]
    # 目录是方向级的：同一条线上下行是两个 lineId，全查再合并（见 issue #6）。
    # 香港一个「1」对应五家公司（九巴/城巴/屿巴/新界小巴/港岛小巴），名字里的括号
    # 就是公司，按名字分组查，别取前两条（前两条可能分属两家，混着报）。
    groups, desc = [], ""
    for nm in dict.fromkeys(n for n, _ in hit):
        co = re.search(r"[（(](.+?)[)）]", nm)
        for line_id in [lid for n, lid in hit if n == nm]:
            d = _cached("live-cl", (city_id, line_id), LIVE_TTL, lambda i=line_id: chelaile.live(city_id, i))
            # state=-1 等待发车、-2 临时停运、-3 末班已过，desc 是现成中文文案
            desc = desc or d.get("desc") or ""
            to = d.get("to") or ""
            groups.append((f"{to}（{co.group(1)}）" if co else to, d["buses"]))
    # 多家公司同名时抬头别写成「1（九巴） 有 7 辆」，那不是一家的车
    return {"source": "车来了", "line": re.sub(r"[（(].*?[)）]", "", name)
            if len({n for n, _ in hit}) > 1 else name, "groups": groups, "desc": desc}


# 掌上这座城不给车辆位置（hasReal=0：北京、成都）时，下次把车来了排前面，省一次空等
PREFER_CL = {}


def query(city_key, wants, src=None):
    """两家依次试。有车就立刻用，两家都空车才算没车——否则会漏掉另一家有车的城。

    wants 是候选线路名，先试带前缀的「陆川1」，再试裸号「1」。
    src 是用户点名要的数据源（「从车来了看一下」），排它前面而已，不是只用它——
    点名的那家没有这条线时仍要能答上来。
    """
    ent = cities().get(city_key) or {}
    tried, blank, near = [], None, None
    order = ("chelaile", "mygolbs") if PREFER_CL.get(city_key) else ("mygolbs", "chelaile")
    if src:
        order = (src,) + tuple(s for s in order if s != src)
    for want in wants:
        for source in order:
            fetch = {"mygolbs": _mygolbs, "chelaile": _chelaile}[source]
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
                # 掌上这座城没实时（unknown），记下来，下一问直接先问车来了
                if got.get("unknown") and ent.get("chelaile"):
                    PREFER_CL[city_key] = True
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
    if tried:
        return {"error": tried[0]}          # 上游自己说的原因，比我编的准
    return {"error": "没这条线路"}


def _car(plate, stn, dist, status=""):
    # statusType "0" = 已到站（官方 H5：到站车辆直接归到最近站）；其余取值语义没确认，
    # 只信 "2"（在途）带出来的距离，别拿别的状态的数字当「离下一站多远」。
    if str(status) == "0":
        return f"{plate} 已到{stn}"
    try:
        dist = float(dist or 0)
    except (TypeError, ValueError):
        dist = 0
    if dist <= 0 or str(status) not in ("", "2"):
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
        # 「没车」和「上游没给车辆位置」是两件事，后者会被乘客读成停运（见 issue #2）
        if "地铁" in norm_line(line):           # 地铁进得了目录，但全程 hasReal=0
            return f"{line} 没有实时车辆位置，地铁不上报车机，站表和首末班能查"
        tail = res.get("note") or res.get("desc") or ""
        head = f"{line} 查不到实时车" if res.get("unknown") else f"{line} 这会儿没车"
        return head + ("，" + tail if tail else "")
    multi = sum(1 for _, b in res["groups"] if b) > 1      # 只有一个方向有车时不念空方向
    rows = [f"有，{line} 现在 {total} 辆在路上"]
    for label, buses in res["groups"]:
        if not buses:
            if multi and label:
                rows.append(f"往{label}还没车")
            continue
        rows.append((f"往{label}：" if label else "") + "，".join(_car(*b) for b in buses))
    return "\n".join(rows)


HOP_RE = re.compile(r"从?\s*([^，,。？?！!\s]{2,12}?)\s*到\s*([^，,。？?！!\s]{2,12})")
# 「火车站坐什么车」要把尾巴砍掉再当站名查；「我在十字街」要把脑袋砍掉
TAIL = re.compile(r"(怎么坐|坐什么|怎么去|有什么|有哪几|几路车|坐几路|怎么走|咋坐|咋去|换乘|有直达|直达|路线|的话|还有|吗|呢|啊).*$")
# @ 标记里的 QQ 号（[At:E2795…] / <@E8CBB…>）长得像「E8 路」，解析前先抹掉
AT_RE = re.compile(r"\[At:[^\]]*\]|<@[^>\s]*>")
# 图片/文件消息的占位标签：「description"eyJ0ZXh0…」里的 base64 长得像「yJ0 路」
MARKUP_RE = re.compile(r"<attachment[^>]*>|\[图片\]|\[Image[^\]]*\]", re.I)
LEAD = re.compile(r"^(我?在|我?从|去|到|等|坐)")


def stops(city_name, keyword):
    """CMD=102 站点搜索：带 lat/lng 和经停线路，是唯一能按站名反查线路的命令。"""
    return _cached("stop", (city_name, keyword), CATALOG_TTL,
                   lambda: _post({"CMD": "102", "CITYNAME": city_name,
                                  "KEYWORD": keyword}).get("busstations") or [])


def find_stop(city_name, raw):
    """站名从长到短试，命中即止——用户嘴里不会带括号后缀，得让平台的站名去对。

    底线是 3 字：允许退到 2 字时，「南宁机场」会退成「南宁」，搜出「南宁孔庙」
    这种风马牛不相及的站，比不答更糟。
    """
    for n in (8, 6, 5, 4, 3):
        if len(raw) >= n:
            rows = stops(city_name, raw[:n])
            if rows:
                return rows[0], raw[:n]
    # 用户写的和站牌上的常常差一两个字（「玉林旅游客运站」vs「旅游汽车客运站」），
    # 拿尾巴当关键词搜一遍，再挑最像的那个，相似度不够宁可不认
    for n in (3, 2):
        if len(raw) > n:
            rows = stops(city_name, raw[-n:])
            best = max(rows or [], key=lambda r: difflib.SequenceMatcher(
                None, raw, r.get("stationName") or "").ratio(), default=None)
            if best and difflib.SequenceMatcher(None, raw, best["stationName"]).ratio() >= 0.6:
                return best, best["stationName"]
    return None, raw


def hop(msg):
    """→ (出发站, 到达站)。只认「从X到Y」，别的句式不猜。"""
    m = HOP_RE.search(msg)
    if not m or LINE_RE.search(m.group(1)):     # 「坐1路到火车站」左边是线路号，不是站
        return None
    return m.group(1).lstrip("从"), TAIL.sub("", m.group(2)).rstrip("的了")


def pick_stop(city_name, msg):
    """从整句里抠站名：按标点切片，逐片砍掉「我在/坐什么」再拿去搜。"""
    city = norm_city(city_name)
    for part in re.split(r"[，,。.？?！!、\s]+", msg):
        cand = LEAD.sub("", TAIL.sub("", part))
        if LINE_RE.search(cand):
            # 「G02到高新产业园」：线路号后面那个才是上车站，别整片跳过
            cand = re.split(r"到|去|往|在|经停", cand)[-1]
        if len(cand) < 2 or norm_city(cand) == city or LINE_RE.search(cand):
            continue                        # 城市名和线路号不是站名
        got, _ = find_stop(city_name, cand)
        if got:
            return got["stationName"]
    return None


def _dest_after(city_name, msg, stop):
    """「G02到高新产业园，去玉林北站」：站名后面那个「去/往X」是方向，不是上车站。"""
    m = re.search(r"(?:去|往|到)\s*([^，,。.？?！!\s]{2,12})", msg.split(stop)[-1])
    if not m:
        return None
    # 「去玉林北站要等多久」：站名后面跟的是「要/等多久」，砍掉再搜，别指望模糊匹配
    got, _ = find_stop(city_name, TAIL.sub("", re.split(r"要|等", m.group(1))[0]))
    return (got or {}).get("stationName")


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
    """这一路下一辆车几分钟到 A（上游预测），从 A 坐到 B 要几分钟（按每段均速累）。"""
    payload = _live_my(city_name, line, d, ia + 1)      # STATIONORDER 1 基
    eta = _etas(payload)
    if not eta:
        last = _last_bus(city_name, line, d)
        if last and _past_last(last):
            return f"，末班 {last} 已经开走了"
        return "，这会儿没车报位置"
    return f"，下一辆约 {max(1, round(eta[0]))} 分钟后到，坐过去 {max(1, round(_run(payload, ia, ib)))} 分钟"


def render_hop(city_key, city_name, raw):
    """A 站到 B 站：拿两站的经停线路求交，再按站序验方向，验不过的不算直达。"""
    a, a_name = find_stop(city_name, raw[0])
    b, b_name = find_stop(city_name, raw[1])
    if not a and not b:
        return None                              # 两边都不是站名，多半本来就不是问公交
    if not a or not b:
        return ""       # 有一边不是公交站（机场、外地地名）就交回给模型，别硬给一句话了事
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


def _clock(mins):
    t = _now()
    m = t.tm_hour * 60 + t.tm_min + round(mins)
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def render_at(city_key, city_name, want, msg):
    """「我在 X 站，N 路还有几站」：按方向给最近一辆，分钟用上游对该站的预测。"""
    station_name = pick_stop(city_name, msg)
    if not station_name:
        return None
    # 「G02 到高新产业园，去玉林北站」问的是在产业园上车、往玉林北站那个方向，别把终点当上车站
    dest = _dest_after(city_name, msg, station_name)
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick([(r["lineName"], r) for r in rows], want)
    out = []
    for line_name, row in hit:
        d = row["upperOrDown"]
        if dest and dest != station_name:
            got = _goes(city_name, line_name, station_name, dest)   # 只留能开到 dest 的方向
            if not got or got[0] != d:
                continue
        names = [s.get("stationName") or "" for s in _station_seq(city_name, line_name, d)]
        idx = next((i + 1 for i, n in enumerate(names) if station_name in n), None)
        if not idx:
            continue
        eta = _etas(_live_my(city_name, line_name, d, idx))
        if not eta:
            continue
        mins, left, plate = eta
        span = "马上到" if left <= 0 else f"还有 {left} 站"
        out.append(f"往{row.get('to') or '?'}：{plate} 约 {_clock(mins)} 到（{max(1, round(mins))} 分钟），{span}")
    if not out:
        return None
    return f"{hit[0][1]['lineName']} 到{station_name}：" + "；".join(out)


STOPS_RE = re.compile(r"所有?站点|全部站点|站(点)?列(表|出来)|经过哪些站|有哪些站|停哪些站|站序|沿途")


def render_stops(city_name, want):
    """「B19 路所有站点」要的是站表，不是实时车。两个方向站一样，只念主方向。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick([(r["lineName"], r) for r in rows], want)
    out = []
    for line_name, row in hit:
        names = [s.get("stationName") or "" for s in _station_seq(city_name, line_name, row["upperOrDown"])]
        if names:
            body = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
            out.append(f"{line_name}（往{row.get('to') or '?'}，{len(names)} 站）：\n{body}")
    return "\n\n".join(out) or None


def render_times(city_name, want):
    """「G02 首班几点」问的是时刻表，不是实时车——有车没车都得回答时间。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    out = []
    for name, row in hit:
        d = str(row.get("upperOrDown") or "")
        if d not in ("1", "2"):
            continue
        f, l = _first_last(city_name, name, d)
        if f or l:                                   # 两个方向时刻不同就各念一行
            out.append(f"{name} 首班 {f or '?'}，末班 {l or '?'}"
                       f"（{row.get('from') or '?'} → {row.get('to') or '?'}）")
    return "\n".join(dict.fromkeys(out)) or None


TRANSFER_RE = re.compile(r"换乘|能换|可以换|转哪|转到")
# 「G02 首班几点发车」只问时刻，别拿实时车回（会答成末班已开走）
TIMES_RE = re.compile(r"首班|首车|头班|第一班|末班|末车|最后一班|收班|发车时间")
# 用户点名数据源：「从车来了的那个看一下数据」
SRC_RE = re.compile(r"(?P<cl>车来了)|(?P<my>掌上(?:公交)?)")
# 「9路是哪家公司的」「丢东西了找谁」——只有 114 的 routeBean 带运营公司
COMPANY_RE = re.compile(r"哪家公司|哪个公司|哪家公交|所属公司|运营公司|归谁|谁开的|投诉|客服|电话|遗失|丢了|落下|忘在")


def render_company(city_name, want):
    """114.routeBean 有 company（实测几乎全城都有）和 companyMobile（只有部分城市有）。"""
    d = _cached("bean", (city_name, want), CATALOG_TTL,
                lambda: _post({"CMD": "114", "CITYNAME": city_name, "KEYWORD": want}))
    rows = [b.get("routeBean") or {} for b in (d.get("buslines") or [])]
    hit = _pick([(r["routeName"], r) for r in rows if r.get("routeName")], want)
    out, seen = [], set()
    for name, r in hit:
        co = (r.get("company") or "").strip()
        if not co or name in seen:
            continue
        seen.add(name)
        tel = (r.get("companyMobile") or "").strip().rsplit(":", 1)
        out.append(f"{name} 归{co}" + (f"，咨询电话 {tel[-1]}" if len(tel) == 2 else ""))
    if not out:
        return None
    if not any("电话" in o for o in out):
        out.append("平台没挂咨询电话，丢东西直接打 12345 或去线路终点站的调度室问")
    return "\n".join(out)


def render_transfer(city_name, want):
    """逐站能换哪几路：上下行站几乎一样，按站名去重只算一份。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick([(r["lineName"], r) for r in rows], want)
    out, done = [], set()
    for line_name, row in hit:
        if line_name in done:                    # 目录里同一路有上下行两条，别念两遍
            continue
        done.add(line_name)
        names, seen = [], set()
        for d in ("1", "2"):
            for s in _station_seq(city_name, line_name, d):
                n = s.get("stationName") or ""
                if n and n not in seen:
                    seen.add(n)
                    names.append(n)
        if not names:
            continue

        def others(n):
            hits = (stops(city_name, n) or [{}])[0].get("relatedRoutes") or []
            return "、".join(r for r in hits if r != line_name)

        with ThreadPoolExecutor(max_workers=8) as pool:
            body = list(pool.map(others, names))
        have = [(n, t) for n, t in zip(names, body) if t]
        rest = [n for n, t in zip(names, body) if not t]    # 没得换的站不逐条占行
        if not have:
            out.append(f"{line_name} 沿线 {len(names)} 站都没有别的线路经过")
            continue
        out.append(f"{line_name} 沿线 {len(names)} 站，{len(have)} 站能换乘：" + "\n" +
                   "\n".join(f"{i}. {n}（{t.count('、') + 1} 条）：{t}"
                              for i, (n, t) in enumerate(have, 1)) +
                   (f"\n其余 {len(rest)} 站只有这条线经过：" + "、".join(rest) if rest else ""))
    return "\n\n".join(out) or None


def render_stop(city_key, city_name, msg):
    """「X 站有哪几路」：站点经停线路，每条带首末站。"""
    name = pick_stop(city_name, msg)
    if not name:
        return None
    routes = (stops(city_name, name) or [{}])[0].get("relatedRoutes") or []
    if not routes:
        return None
    rows = _catalog(city_name).get("buslines") or []
    ends = {}
    for r in rows:
        ends.setdefault(r["lineName"], []).append(f"{r.get('from')} ↔ {r.get('to')}")
    items = [f"{n} {ends[n][0]}" if ends.get(n) else n for n in routes if ends.get(n)] or list(routes)
    head = f"{name}有 {len(items)} 条线路："
    return head + "".join("\n" + i for i in items[:12]) + ("\n…还有 " + str(len(items) - 12) + " 条" if len(items) > 12 else "")


# 「玉林还有哪条线路有车」这类全城问题没法靠线路号答，得把目录扫一遍。
# 放在站点判断之后：能认出站名就以站点为准，「十字街有几路车」不会被抢走。
SWEEP_RE = re.compile(r"哪条|哪些|哪几|哪路|还有|在跑|运营")
SWEEP_MAX = 60          # 扫 60 条线就是 120 个请求；再大的城市让用户点名，别硬扫


def _sweep_chelaile(city_name):
    """掌上没目录时的兜底扫：车来了逐条问 state。"""
    cid = next((c.get("chelaile") for c in cities().values()
                if (c or {}).get("mygolbs") == city_name), None)
    if not cid:
        return ""
    rows = _cached("catalog-cl", cid, CATALOG_TTL, lambda: chelaile.lines(cid))
    note = f"（抽查了 120 条）" if len(rows) > 120 else ""
    rows = rows[:120]

    def one(row):
        name, lid = row
        try:
            d = _cached("live-cl", (cid, lid), LIVE_TTL, lambda: chelaile.live(cid, lid))
        except Exception:
            return name, "", 0, None, ""
        return name, d.get("to") or "", len(d.get("buses") or []), d.get("state"), d.get("desc") or ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(one, rows))
    per = {}
    for name, to, n, _, _ in got:
        cnt, ends = per.setdefault(name, [0, set()])
        if n:
            per[name][0] = cnt + n
            ends.add(to)
    live = sorted(((k, v[0], "、".join(f"往{e}" for e in sorted(v[1])))
                   for k, v in per.items() if v[0]), key=lambda x: -x[1])
    if not live:
        # 别拿单条线的文案（「本线路末班时间已过」）解释全城：先看状态是否全一致
        states = {g[3] for g in got}
        descs = {g[4] for g in got if g[4]}
        tail = ("，首班还没到" if states == {-5} else "，末班都过了" if states == {-3}
                else f"（车来了：{descs.pop()}）" if len(states) == 1 and len(descs) == 1 else "")
        return f"{city_name}这会儿{f'抽查了 {len(rows)} 条' if note else '全线'}都没车报位置{tail}"
    return (f"{city_name}这会儿有 {len(live)} 条线在跑{note}："
            + "".join(f"\n{k} {c} 辆（{e}）" for k, c, e in live))


def render_sweep(city_name):
    """全城扫一遍：这会儿哪些线路真有车在路上。"""
    rows = _catalog(city_name).get("buslines") or []
    ends, dirs = {}, {}
    for r in rows:
        name = r["lineName"]
        ends.setdefault(name, f"{r.get('from')} ↔ {r.get('to')}")
        d = str(r.get("upperOrDown") or "")
        if d in ("1", "2"):
            dirs.setdefault(name, []).append(d)
    names = [n for n in ends if dirs.get(n)]
    if not names:      # 掌上这座城没有线路目录（北海就是），换车来了扫
        return _sweep_chelaile(city_name)
    # 抽 3 条线看有没有实时：只问第一条，一条线的 hasReal 就能让整城短路
    if all(str(_live_my(city_name, n, dirs[n][0]).get("hasReal")) == "0" for n in names[:3]):
        # 掌上不给（北京、成都），车来了也许给，先问车来了再说查不到
        return _sweep_chelaile(city_name) or \
            f"{city_name}上游不提供实时车辆位置，线路目录能查到，车在哪查不到"
    note = ""
    if len(names) > SWEEP_MAX:
        # 上千条线的城市扫不完：按号取前 60 条纯数字主线，够看出这会儿还有没有车在跑
        trunk = sorted((n for n in names if n.endswith("路") and n[:-1].isdigit()),
                       key=lambda n: int(n[:-1]))
        names = (trunk or names)[:SWEEP_MAX]
        note = f"（抽查了 {len(names)} 条主线）"
    jobs = [(n, d) for n in names for d in dirs[n]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = pool.map(lambda j: len(_live_my(city_name, j[0], j[1]).get("list") or []), jobs)
    per = {n: 0 for n in names}
    for (n, _), c in zip(jobs, counts):
        per[n] += c
    live = sorted(((n, c) for n, c in per.items() if c), key=lambda x: -x[1])
    if not live:
        if note:
            return f"{city_name}这会儿抽查了 {len(names)} 条主线，都没车报位置，可能都收班了"
        return f"{city_name}这会儿全线都没车报位置，可能都收班了"
    return (f"{city_name}这会儿有 {len(live)} 条线在跑{note}："
            + "".join(f"\n{n} {c} 辆（{ends[n]}）" for n, c in live))


LAST = {}          # 会话 → 上一条答过的城市/线路；追问「每站能换乘啥」不带车次，靠它


def _last(key):
    """按会话记：进程级的单槽会让 A 群的追问串到 B 群（问 G02 的换乘回了 9 路）。"""
    d = LAST.setdefault(key or "", {})
    while len(LAST) > 64:                        # shortcut: 超 64 个会话丢最早的，不做 LRU
        FIRST = next(iter(LAST))
        if FIRST == (key or ""):
            break
        LAST.pop(FIRST)
    return d


def answer(msg, default_city, ctx="", key=""):
    """ctx 是引用/上文里的文字，只用来认城市——追问「那 39 路往哪开」靠它。"""
    last = _last(key)
    msg = MARKUP_RE.sub(" ", AT_RE.sub(" ", msg))
    line = parse(msg)
    # 「从车来了的那个看一下数据」没写线路号：说的就是上一条问的那路车
    sm = SRC_RE.search(msg)
    src = ("chelaile" if sm.group("cl") else "mygolbs") if sm else None
    if src and not line and last.get("line") and re.search(r"看|查|数据|接口", msg):
        line = (last["line"], "")
    prefix = line[1] if line else ""
    if prefix in ("那", "这", "该", "此", "本"):      # 指示词不是县名，别拼进线路名
        prefix = ""
    city_key, explicit = detect_city(msg + " " + ctx, prefix, default_city)
    base = norm_city(default_city)
    # 前缀自己就是一座城（「玉林 容县 11 路」）时以它为准：玉林的目录里没有容县11路，
    # 拿玉林的目录查裸号会静默答成玉林 11 路
    if prefix and norm_city(prefix) in cities():
        city_key, explicit = norm_city(prefix), True
    if not city_key:
        # 认不出的前缀多半是县内线路名（陆川1路），在默认城市目录里再找一次；
        # 再不行试上一条问过的城市——群里追问「b19 所有站点」不会重报城市名
        # shortcut: 进程级共用一个城市，多群同时问不同城会串，够用就升级成按会话记
        if line:
            for ck in [base] + ([last["city"]] if last.get("city") in cities() else []):
                if ck in cities() and "error" not in query(ck, [prefix + line[0]]):
                    city_key = ck
                    break
        if not city_key and (hop(msg) or not prefix):      # 「从A到B」「X站有哪几路」不许城市名也能答
            city_key = base if base in cities() else None
        # 表里只有 320 城，县级市常缺（义乌、慈溪、晋江掌上都有）：拿原名直接问掌上
        if not city_key and len(prefix) >= 2:
            if not _covered(prefix):
                # 别写「查不了」：main.py 的 FALSE_CLAIM 会把这句当假话整句吞掉，用户啥也收不到
                return f"{prefix}这座城暂时没有公交数据，掌上和车来了都没覆盖"
            city_key = norm_city(prefix)
    if not city_key:
        return f"「{prefix or '这个城市'}」是哪儿的公交？没认出来，写「{prefix} {line[0] if line else '1'} 路」这种试试"
    # 表外城市直接用用户写的中文名当 CITYNAME（掌上就吃中文名）
    name = (cities().get(city_key) or {}).get("mygolbs") or (prefix if city_key not in cities() else None)
    last["city"] = city_key
    got = hop(msg)
    if got:
        ca = detect_city(got[0], "", "")[0]
        cb = detect_city(got[1], "", "")[0]
        if ca and cb and ca != cb:
            return f"{got[0]}在{ca}，{got[1]}在{cb}，跨城的公交串不起来，这段得坐高铁或长途"
    if got and name:
        return render_hop(city_key, name, got) or ""
    if name and TRANSFER_RE.search(msg):
        # 「这条线路每站能换什么」没有线路号，用上一条答过的线路
        want = (prefix + line[0] if prefix and not explicit else line[0]) if line else last.get("line") or ""
        out = render_transfer(name, want) if want else None
        if out:
            last["line"] = want
            return out
    if line:
        # 前缀是线路名的一部分（容县11路），明写城市也不该丢——explicit 只管去哪个目录找
        wants = ([prefix + line[0]] if prefix and norm_city(prefix) != city_key else []) + [line[0]]
        last["line"] = wants[0]
        if name:
            for w in wants:
                if TIMES_RE.search(msg):          # 问首末班时刻表
                    out = render_times(name, w)
                elif COMPANY_RE.search(msg):      # 问公司/找失物/投诉
                    out = render_company(name, w)
                elif STOPS_RE.search(msg):       # 要的是站表，别回实时车
                    out = render_stops(name, w)
                else:
                    out = render_at(city_key, name, w, msg)
                if out:
                    return out
        txt = render(city_key, wants[0], query(city_key, wants, src))
        # 没明写城市就按默认城市查，查不到时别让用户以为「这线路不存在」
        if not explicit and txt.startswith("没查到"):
            txt += f"\n（你没写城市，我按{norm_city(city_key)}查的，别的城市前面带上城市名）"
        return txt
    if name:
        # 「还运营吗」「哪条线在跑」没有站名，别拿去当站名搜（哈尔滨 → 哈尔滨站）
        if SWEEP_RE.search(msg) and not re.search(r"站|几路|哪路", msg):
            return render_sweep(name)
        out = render_stop(city_key, name, msg)
        if out:
            return out
        return ""
    return ""
