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
# 区县 → 地级市：「东胜区8路」这种写法得先知道东胜区属鄂尔多斯，掌上不认区县当城市名。
# 只收全国不重名的区县，重名的（朝阳区、城区）指不准是哪座城，宁可交回模型。
DISTRICTS = json.loads((HERE / "districts.json").read_text("utf-8"))
# 站名里带的是裸县名（「元氏火车站」不是「元氏县火车站」）：去掉行政后缀再收一遍，
# 同样只留唯一指向一座城的裸名
DIST_BARE = {}
for _k, _v in DISTRICTS.items():
    _b = re.sub(r"(特别行政区|自治区|自治县|自治州|地区|盟|市|区|县|旗)$", "", _k)
    if len(_b) >= 2:
        _old = DIST_BARE.get(_b)
        DIST_BARE[_b] = _v if _old in (None, _v) else ""      # 撞了就标空，不用

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
    # 「环山二号线」「高新二号线」：号码夹在中间，不带「地铁」也是目录里的真线名。
    # shortcut: 只吃 4 个汉字前缀，「玉林二号线」这种城市名+号线的写法会被整段吃掉
    # 前面带「地铁/轨道交通」的归上面那条（最左匹配会先把城市名一起吃进来，得挡掉）
    r"|(?<![A-Za-z0-9])(?![\u4e00-\u9fa5]{0,4}(?:地铁|轨道交通))"
    r"(?P<numline>[\u4e00-\u9fa5]{0,4}?[一二三四五六七八九十\d]{1,3}\s*号线)"
    r"|(?P<alpha>(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{1,3}(?![A-Za-z0-9]))(?:\s*路)?"
    r"|(?P<num>\d{1,3})\s*[路线](?P<qual>夜班|区间|支线|快线|专线)?"
    # 「131区间」不带「路」也算线路——石家庄目录里就叫「131路区间」
    r"|(?P<inum>\d{1,3})\s*(?P<iqual>区间|支线|快线|专线)"
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
JUNK_HEAD = re.compile(r"^(?:车来了|掌上公交|掌上|查询|查一下|查下|查一查|看一下|看下|看看|"
                       r"搜一下|搜搜|一下|帮我|麻烦|你|我|再|还|也|就|那|这)+")
JUNK_TAIL = re.compile(r"(?:看看|查查|看看有|看|查|有|的|了|来|台|部|辆|只|几)+$")
# 像在问公交才去查。线路/路线/站点这三个词在闲聊里到处都是（「人生路线」「这个站点好难」），
# 不单独算信号；实词、线号、数据源名才是。
BUS_RE = re.compile(
    r"公交|巴士|公共汽车|公车|地铁|轨道交通|几路|哪路|路车|末班|首班|到站|几站|\d\s*号线|号线|"
    r"怎么坐|直达|换乘|车来了|掌上公交|车牌|号牌|\d{1,3}\s*[路线]|(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{1,3}(?![A-Za-z0-9])")
# 快路抢答前的最后一道：带问法就答；像叙述（「11路那车真挤」「今天坐9路回家累死了」）
# 就交给模型。默认答，只挡明显的叙述句——挡多了会把「谈固站有什么线路」也挡掉。
ASK = re.compile(r"多久|几站|几分钟|几辆|几台|几点|有车|有没有|有吗|还有|在哪|到哪|到吗|怎么坐|怎么去|"
                 r"换乘|直达|首班|末班|时刻|查|看一下|看看|多少|什么车|哪几路|哪路|所有站|站点|站表|班车|"
                 r"能不能|可以|来吗")

CHAT = re.compile(r"我|你|他|她|它|觉得|感觉|真|太|超|累|吧|呀|哈|今天|昨天|明天|了")
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


def _district_city(text):
    """「元氏火车站」里带的是裸县名：翻成所在地级市，比落回默认城强。"""
    hits = [b for b, v in DIST_BARE.items() if v and b in (text or "")]
    if not hits:
        return None
    key = norm_city(DIST_BARE[max(hits, key=len)])
    return key if key in cities() else None


def _snap(pre):
    """前缀窗口截短过（「查东胜区」「下东胜区」）：右边才是地名，往右收到认得为止。"""
    if not pre:
        return pre
    idx = cities()
    for i in range(1, len(pre) - 1):
        t = pre[i:]
        if t in idx or norm_city(t) in idx or t in DISTRICTS:
            return t
    return pre


def parse(msg):
    """→ (线路号, 紧贴着的汉字段)。县内线路「陆川1路」靠这个前缀区分。"""
    m = LINE_RE.search(msg)
    if not m:
        return None
    # 「环山二号线」的线名自己就带汉字前缀，别再把它当县名重复拼一次
    # 先剥掉整段的动词再取尾巴：PREFIX_RE 只吃 4 个字，「帮我查一下东胜区」窗口里会剩
    # 「下东胜区」这种半截词，当成站名/线名前缀去查就成了一条不存在的线
    head = JUNK_HEAD.sub("", msg[: m.start()])
    pre = "" if m.group("numline") else PREFIX_RE.search(head)
    num = (m.group("inum") + m.group("iqual")) if m.group("inum") else \
        (m.group("alpha") or m.group("num") or m.group("metro") or m.group("numline")
         or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
    pre = JUNK_TAIL.sub("", JUNK_HEAD.sub("", pre.group(1) if pre else ""))
    return norm_line(num), _snap(pre)


def looks_like_ask(msg):
    """这句像在问公交吗。快路命中线路号后先用它过一道，别把闲聊抢走。"""
    return bool(ASK.search(msg)) or not CHAT.search(msg)


def _lines(msg):
    """句子里问了哪几条线：「樟13 / 12路」「11路和12路」都要答，别只答第一条。"""
    out, end = [], -1
    for m in LINE_RE.finditer(msg):
        if m.start() < end:            # 「131路区间」先匹到「131路」，别再算一条
            continue
        end = m.end()
        num = (m.group("inum") + m.group("iqual")) if m.group("inum") else \
            (m.group("alpha") or m.group("num") or m.group("metro") or m.group("numline")
             or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
        # 只认紧贴着的汉字前缀：多行里上一行的「樟」不算 12 路的前缀
        pre = re.search(r"([\u4e00-\u9fa5]{1,4})$", JUNK_HEAD.sub("", msg[: m.start()]))
        pre = "" if m.group("numline") else JUNK_TAIL.sub(
            "", JUNK_HEAD.sub("", pre.group(1) if pre else ""))
        # 「11路和12路」：前缀截到的是「路和」，不是县名
        pre = re.sub(r"公交$", "", pre)
        pre = re.sub(r"^(?:路|和|与|跟|及|、)+|(?:路|和|与|跟|及)+$", "", pre)
        if pre in ("那", "这", "该", "此", "本"):
            pre = ""
        out.append((norm_line(num), _snap(pre)))
    # 「樟13」这种县内线习惯不带「路」，整段就一个词时也认（福州目录里叫「樟13路」）
    for part in re.split(r"[\n，,。、;；]+", msg):
        part = AT_RE.sub("", part).strip()
        m = re.fullmatch(r"([\u4e00-\u9fa5]{1,3})(\d{1,3})", part)
        if m and m.group(1) not in cities() and (m.group(1) + m.group(2)) not in [n for n, _ in out]:
            out.append((norm_line(m.group(1) + m.group(2)), ""))
    return out


def _cached(kind, key, ttl, fn):
    hit = _cache.get((kind, key))
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    # 网络抖动别进缓存：catalog 一挂就是一小时查不到车，全是这条缓存留的
    if isinstance(val, dict) and val.get("error") and "detail" in val:
        return val
    # 过期键长期占位会让 _cache 一直涨，顺手清掉同一个 kind 下的过期项，超上限再整体淘汰
    if len(_cache) >= CACHE_MAX:
        now = time.time()
        for k in [k for k, (t, _) in _cache.items() if now - t >= CITY_TTL]:
            _cache.pop(k, None)
    _cache[(kind, key)] = (time.time(), val)
    return val


# 最近一次连不上上游的时刻（秒）。上游挂着时别静默交给模型——模型那层会直接挂住
_up_fail = [0.0]


def _post(params, timeout=8):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 缺 CITYNAME 或线路名不存在时上游回的是 HTML 错误页，不是 JSON
        return {"status": e.code, "msg": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        _up_fail[0] = time.time()
        # 连不上/握手超时也曾直接抛到消息处理里，群里看到的是「:( …出现异常」这一整段。
        # msg 会原样发给群友（render 直接 return error），异常细节只留在 detail 里
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
    # 区县名在句子里也能猜：「南海区8路」「伦教街道办事处公交线路」这类掌上不认区县当城市名
    dd = [k for k in DISTRICTS if k in msg]
    if dd:
        key = norm_city(DISTRICTS[max(dd, key=len)])
        if key in idx:
            return key, True
    # 「东胜区8路」：区县名不是城市，掌上也不认它当城市名，得先翻到属地级市
    if prefix and DISTRICTS.get(prefix):
        key = norm_city(DISTRICTS[prefix])
        if key in idx:
            return key, True
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


def _past_last(last, first=""):
    """末班过了没。字符串比较跨不了零点（'00:20' < '22:40'），按分钟算。"""
    def mins(s):
        try:
            h, m = (s or "").split(":")[:2]
            return int(h) * 60 + int(m)
        except ValueError:
            return None

    last_m = mins(last)
    if last_m is None:
        return False
    t = _now()
    now_m = t.tm_hour * 60 + t.tm_min
    first_m = mins(first)
    if first_m is not None:
        # 按运营窗口判：末班 23:44 的线路，早上 9 点当然还没开走
        if (now_m - first_m) % (24 * 60) <= (last_m - first_m) % (24 * 60):
            return False
        # 窗口外也是两种情况：末班 21:30 的夜班车中午问「离首班更近」，那是还没开，
        # 不是已经收班——不分开就会回「末班已经开走了」
        return (first_m - now_m) % (24 * 60) > (now_m - last_m) % (24 * 60)
    # 没首班就按「末班离现在多久」算，跨零点自然成立：00:02 问 22:40 → 已开走，
    # 23:00 问 00:10 → 还没到。
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
        # 目录拿到了、实时那一步超时：别当成「这条线这会儿没车」
        if payload.get("error") and "detail" in payload:
            return {"error": payload.get("msg") or "上游这会儿没响应，过会儿再问"}
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
            # 但只回「不提供」等于什么都没答，把这条线能查到的（首末班、站数）补上
            unknown = True
            note = f"上游不给{norm_city(city_name)}的实时车辆位置"
            lname, ldir = hit[0][1]["lineName"], hit[0][1]["upperOrDown"]
            n = len(_station_seq(city_name, lname, ldir))
            bits = [f"{first} 起、末班最晚 {last}"] if first and last else []
            if n:
                bits.append(f"{n} 站")
            if bits:
                note += "，这条线 " + "、".join(bits)
        elif last and _past_last(last, first):
            # 连带说首班：凌晨问日间线时，用户要知道的是「几点再来」
            note = f"末班 {last} 已经开走了" + (f"，首班 {first}" if first else "")
        elif live_known:
            note = "线路还在跑，只是车机没报位置"
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


def _veh(bus, veh):
    return veh in str((bus or ("",))[0])


def query(city_key, wants, src=None, veh=""):
    """两家依次试。有车就立刻用，两家都空车才算没车——否则会漏掉另一家有车的城。

    wants 是候选线路名，先试带前缀的「陆川1」，再试裸号「1」。
    src 是用户点名要的数据源（「从车来了看一下」），排它前面而已，不是只用它——
    点名的那家没有这条线时仍要能答上来。
    """
    ent = cities().get(city_key) or {}
    tried, blank, near, fallback, others = [], None, None, None, {}
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
                # 异常原文别拼进给群友的句子（以前就是这么把 urlopen 细节发出去的）
                tried.append(f"{LABEL[source]}异常，上游这会儿连不上")
                continue
            if got and "error" in got:
                if got["error"].startswith("是 "):
                    near = near or got      # 目录里有近似线名，另一家也许真有这条，先记着
                else:
                    tried.append(got["error"])
                continue
            if got and any(group for _, group in got["groups"]):
                if not veh:
                    return got
                # 点名一辆车：掌上的车牌是「桂K12590D」，车来了是「513」，得两家都翻一遍
                hit = [(lab, [b for b in grp if _veh(b, veh)]) for lab, grp in got["groups"]]
                if any(g for _, g in hit):
                    return dict(got, groups=hit)
                # 这家没这辆车：记下它路上有谁，接着问另一家——一家没有不等于没有
                fallback = fallback or got
                others[LABEL[source]] = [str(b[0]) for _, bs in got["groups"] for b in bs]
                continue
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
    if fallback:
        # 这条线有车，只是没有问的那辆：两家查到的一起报，别让群友以为只查了一家
        return dict(fallback, veh_others=others)
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


def _dir(msg):
    """「往呈贡广场方向」问的是单向：只留这个方向的车/时刻。"""
    m = re.search(r"(?:往|去|到)\s*([^，,。.？?！!\s]{2,12}?)\s*方向", msg or "")
    return m.group(1) if m else ""


def render(city_key, want, res, veh="", dir=""):
    if "error" in res:
        e = res["error"]
        if e.startswith("是 "):                       # 目录里有近似线名，报候选
            return f"没「{want}」这条，是 {e[2:]}"
        if e.startswith("没这条"):
            # 光说「没查到」，模型就会自己解释成「这个数据源没有这城市的线路」（泉州 K502 就是这样）
            near = _near(city_key, want)
            return f"没查到「{want}」这条线" + (f"，{city_key}这边有的是 {near}" if near else "")
        return e                                      # 其余文案本身已经是人话
    line = res["line"]
    groups = res["groups"]
    if veh:
        if not any(_veh(b, veh) for _, bs in groups for b in bs):
            per = res.get("veh_others") or {}
            here = ("；".join(f"{k} {'、'.join(v)}" for k, v in per.items() if v)
                    or "、".join(str(b[0]) for _, bs in groups for b in bs))
            return f"{line} 这会儿没跑 {veh} 这辆" + (f"。在路上的：{here}" if here else "")
        groups = [(lab, [b for b in bs if _veh(b, veh)]) for lab, bs in groups]
    if dir:      # 「往X方向」只留这个方向；方向名对不上就别瞎删
        only = [(lab, bs) for lab, bs in groups if dir in (lab or "")]
        if only:
            groups = only
            if not any(bs for _, bs in groups):
                # 这个方向没车报位置 ≠ 停运；昆明这类城上游不给到站预测，只能看车在哪
                return f"{line} 往{dir}方向这会儿没车报位置（这城上游不给到站时间，只能看车在哪）"
    total = sum(len(b) for _, b in groups)
    if not total:
        # 「没车」和「上游没给车辆位置」是两件事，后者会被乘客读成停运（见 issue #2）
        if "地铁" in norm_line(line):           # 地铁进得了目录，但全程 hasReal=0
            return f"{line} 没有实时车辆位置，地铁不上报车机，站表和首末班能查"
        tail = res.get("note") or res.get("desc") or ""
        head = f"{line} 查不到实时车" if res.get("unknown") else f"{line} 这会儿没车"
        return head + ("，" + tail if tail else "")
    multi = sum(1 for _, b in groups if b) > 1      # 只有一个方向有车时不念空方向
    rows = [f"有，{line} 现在 {total} 辆在路上" if not veh else f"{veh} 在 {line} 上："]
    for label, buses in groups:
        if not buses:
            if multi and label:
                rows.append(f"往{label}还没车")
            continue
        rows.append((f"往{label}：" if label else "") + "，".join(_car(*b) for b in buses))
    return "\n".join(rows)


def _near(city_key, want, n=4):
    """同城里跟它最像的几条线：K502 不存在就报 K501、K503，别让人以为这城根本没数据。"""
    name = (cities().get(city_key) or {}).get("mygolbs")
    rows = (_catalog(name).get("buslines") or []) if name else []
    w = norm_line(want)
    hit = sorted({r["lineName"] for r in rows},
                 key=lambda x: -difflib.SequenceMatcher(None, norm_line(x), w).ratio())[:n]
    return "、".join(hit) if hit and difflib.SequenceMatcher(None, norm_line(hit[0]), w).ratio() >= 0.5 else ""


HOP_RE = re.compile(r"从?\s*([^，,。？?！!\s]{2,12}?)\s*到\s*([^，,。？?！!\s]{2,12})")
# 「火车站坐什么车」要把尾巴砍掉再当站名查；「我在十字街」要把脑袋砍掉
TAIL = re.compile(r"(现在|目前|当前|[坐做]哪[个条路]?|[坐做]啥|怎么坐|坐什么|怎么去|有什么|有哪几|多少|几路车|坐几路|怎么走|咋坐|咋去|换乘|有直达|直达|路线|的?线路|的话|附近|周边|周围|还有|有没有|在?哪[里儿]|什么时候|几点到|多久到|还有多久|吗|呢|啊).*$")
# 站牌上不写「公交站」，用户嘴里常带：「丰州公交站」其实是「丰州」
STOPSUF = re.compile(r"(公交车站|公交站|车站|站台)$")
# 「G02-513 现在在哪」「513 号车」「现在 512 车到哪」：问的是一辆具体的车，不是整条线
# 「11 路车」中间隔着「路」，不会被当成车号
VEH_RE = re.compile(r"-\s*(\d{2,5})(?!\d)|(?<![\dA-Za-z])(\d{2,5})\s*[号]?车")
# @ 标记里的 QQ 号（[At:E2795…] / <@E8CBB…>）长得像「E8 路」，解析前先抹掉
AT_RE = re.compile(r"\[At:[^\]]*\]|<@[^>\s]*>")
# 图片/文件消息的占位标签：「description"eyJ0ZXh0…」里的 base64 长得像「yJ0 路」
MARKUP_RE = re.compile(r"<attachment[^>]*>|\[图片\]|\[Image[^\]]*\]", re.I)
LEAD = re.compile(r"^(我?在|我?从|去|到|等|坐)")
# 只剥带数据源的写法：「查桥站」这种真站名不能被「查」吃掉
SRCPRE = re.compile(r"^(?:从|用)?(?:车来了|掌上公交|掌上)\s*[，,、:：]?\s*"
                    r"(?:查一下|查询|查查|看一下|看看|查|看|搜一下|搜|问一下)?\s*[，,、:：]?\s*")


def stops(city_name, keyword):
    """CMD=102 站点搜索：带 lat/lng 和经停线路，是唯一能按站名反查线路的命令。"""
    return _cached("stop", (city_name, keyword), CATALOG_TTL,
                   lambda: _post({"CMD": "102", "CITYNAME": city_name,
                                  "KEYWORD": keyword}).get("busstations") or [])


def _same_rows(rows, hit):
    """同一个站被平台拆成的几条记录：「汽车总站」「（二医院）」「（大北路）」。
    站名不同但都在 100 多米内，是同一片站台，不是隔壁站。
    """
    base = hit["stationName"]
    out, seen = [], {base}
    for r in [hit] + list(rows):
        n = r.get("stationName") or ""
        # 只有「汽车总站（二医院）」这种加括号的才是同一个站的另一条记录；
        # 「芳村花园」「芳村西塱」是隔壁站，「芳村（地铁站）」是另一种车
        if n in seen or not n.startswith((base + "（", base + "(", base + "·")) \
                or "地铁" in n or "轨道交通" in n or not _near_same(hit, r):
            continue
        seen.add(n)
        out.append((n, list(r.get("relatedRoutes") or [])))
    return [(base, list(hit.get("relatedRoutes") or []))] + out


def _merge_same(rows, hit):
    """平台把同一个站拆成「汽车总站」和「汽车总站（二医院）」两条，前者的线路表是残的
    （G02 就只挂在后者名下），只取 rows[0] 会漏掉真正经过的线。

    「芳村」和「芳村（地铁站）」也同前缀，但那是两种车，别混进公交清单里。
    """
    routes = set()
    for _, rs in _same_rows(rows, hit):
        routes |= set(rs)
    return dict(hit, relatedRoutes=sorted(routes))


def _near_same(a, b):
    """「汽车总站（陆川）」也长成「汽车总站（…）」，可那是另一个县的站。
    用坐标分：同站的几条记录实测差 136–209 米，陆川那条 36.8 公里。
    shortcut: 0.01° 粗判（纬度约 1.1km），跨城级别足够了，要精确再上 haversine
    """
    try:
        return abs(float(a["lat"]) - float(b["lat"])) < 0.01 \
            and abs(float(a["lng"]) - float(b["lng"])) < 0.01
    except (KeyError, TypeError, ValueError):
        return True        # 平台没给坐标就退回按名字匹，跟以前一样


def find_stop(city_name, raw):
    """站名从长到短试，命中即止——用户嘴里不会带括号后缀，得让平台的站名去对。

    底线是 2 字（「丰州」这种站牌本名就这么短），但整段跟城市名同字的首字
    会被下面那句挡掉，所以「南宁机场」不会退成「南宁」搜出「南宁孔庙」。
    """
    for n in (8, 6, 5, 4, 3, 2):
        if len(raw) >= n:
            # 「石家庄131区间有没有车」退到 3 字就是城市名本身，会把「石家庄站」搜出来
            if norm_city(raw[:n]) == norm_city(city_name):
                continue
            rows = [r for r in stops(city_name, raw[:n]) or [] if r.get("stationName")]
            # 2 字兜底只认同名的：「石家」会搜出石家庄东站/石家庄站（地铁站），跟问的站没关系
            if n == 2:
                rows = [r for r in rows if r["stationName"] == raw[:n]]
            if rows:
                return _merge_same(rows, rows[0]), raw[:n]
    # 「陆川汽车总站」平台写作「汽车总站（陆川）」：县名被挪到括号里，顺序反过来了，
    # 拿尾巴当关键词搜、再用前缀认是哪一条（「北流汽车总站」这种真带前缀的也一起认）
    for n in (4, 3):
        if len(raw) > n + 1:
            head, tail = raw[:-n], raw[-n:]
            rows = [r for r in stops(city_name, tail) or [] if r.get("stationName")]
            hit = next((r for r in rows if head in r["stationName"]), None)
            if hit:
                return _merge_same(rows, hit), hit["stationName"]
    # 用户写的和站牌上的常常差一两个字（「玉林旅游客运站」vs「旅游汽车客运站」），
    # 拿尾巴当关键词搜一遍，再挑最像的那个，相似度不够宁可不认
    for n in (3, 2):
        # 「玉林汽车站」退到「车站」全是通用字，会匹上「玉林火车站」——那是另一个站
        if len(raw) > n and not re.fullmatch(r"[公交汽车火车车站台]+", raw[-n:]):
            rows = [r for r in stops(city_name, raw[-n:]) or [] if r.get("stationName")]
            best = max(rows, key=lambda r: difflib.SequenceMatcher(
                None, raw, r["stationName"]).ratio(), default=None)
            # 「石家庄北站」退到「北站」会匹上「石家庄东站」：带方位字的要求方位也对得上
            dirs = [c for c in "东南西北中" if c in raw]
            if best and dirs and not any(c in best["stationName"] for c in dirs):
                best = None
            if best and difflib.SequenceMatcher(None, raw, best["stationName"]).ratio() >= 0.6:
                return _merge_same(rows, best), best["stationName"]
    return None, raw


def hop(msg):
    """→ (出发站, 到达站)。只认「从X到Y」，别的句式不猜。"""
    m = HOP_RE.search(msg)
    if not m or LINE_RE.search(m.group(1)):     # 「坐1路到火车站」左边是线路号，不是站
        return None
    return m.group(1).lstrip("从"), TAIL.sub("", m.group(2)).rstrip("的了")


def _cands(city_name, msg):
    """从整句里抠出可能的站名：按标点切片，逐片砍掉「我在/坐什么」。"""
    city = norm_city(city_name)
    for part in re.split(r"[，,。.？?！!、\s]+", msg):
        cand = SRCPRE.sub("", part)      # 「车来了查X」里的数据源和动词
        if LINE_RE.search(cand):
            # 「G02到高新产业园」「11路还有多久到双溪口」：线路号后面那个才是站，先切再砍
            cand = re.split(r"到|去|往|在|经停", cand)[-1]
        cand = LEAD.sub("", TAIL.sub("", cand))
        whole = re.sub(r"^(地铁|轨道交通|公交)", "", cand)
        # 「广州地铁芳村站附近」：城市名和「地铁」都不是站名的一部分
        cand = re.sub(r"^(地铁|轨道交通|公交)", "", re.sub(rf"^{re.escape(city)}", "", cand))
        # 先试剥掉城市名的「芳村站」，不行再试原样的「玉林北站」（北站是站名的一部分）
        # 再退回剥掉「公交站」的写法：「丰州公交站」平台上是「丰州」
        bare = [STOPSUF.sub("", c) for c in (cand, whole)]
        for c in dict.fromkeys([cand, whole] + bare):
            # 「泉州公交 惠南工业区四期站」：切出来的「泉州公交」只剩城市名，拿去搜站会匹到
            # 「中化泉州公司」这种同名不同地的站
            core = re.sub(r"(公交)?(车?站|台)?$", "", c) or c
            if len(c) >= 2 and norm_city(c) != city and norm_city(core) != city \
                    and not LINE_RE.search(c):
                yield c


def pick_stop(city_name, msg):
    for cand in _cands(city_name, msg):
        got, _ = find_stop(city_name, cand)
        if got:
            return got["stationName"]
    return None


def _stop_lines(city_name, text):
    """这段文字里的站经停哪些线：「这四条线路哪个在跑」拿它把范围收到站上。"""
    name = pick_stop(city_name, text)
    rows = stops(city_name, name) if name else None
    if not rows:
        return None, []
    row = next((r for r in rows if r.get("stationName") == name), rows[0])
    return name, [n for _, rs in _same_rows(rows, row) for n in rs]


def _msg_line(city_name, msg):
    """句子里点名的真线名不是「数字+路」（「玉林北站定制夜班车」）：按目录认。"""
    for r in _catalog(city_name).get("buslines") or []:
        n = r.get("lineName") or ""
        if len(n) >= 4 and n in msg:
            return n
    return None


def _dest_after(city_name, msg, stop):
    """「G02到高新产业园，去玉林北站」：站名后面那个「去/往X」是方向，不是上车站。"""
    if not stop:
        return None
    m = re.search(r"(?:去|往|到)\s*([^，,。.？?！!\s]{2,12})", msg.split(stop)[-1])
    if not m:
        return None
    # 「往呈贡广场方向什么时候能到」：方向后面跟的是「方向」和问法，砍掉再搜
    raw = re.sub(r"方向.*$", "", re.split(r"要|等", m.group(1))[0])
    got, _ = find_stop(city_name, TAIL.sub("", raw))
    return (got or {}).get("stationName")


def _exact_line(city_name, msg, want):
    """目录里有更长的真线名整段出现在句子里（「新乐专1路」）就用它，别只拿裸号去匹。"""
    flat, w = norm_line(msg), norm_line(want)
    rows = _catalog(city_name).get("buslines") or []
    hit = []
    for r in rows:
        n = norm_line(r["lineName"])
        # 句子里把城市名也粘进了线名（「西安环山二号线」）：目录名是它的后缀也算。
        # 只认差 1–4 个汉字的情况（城市名被粘进线名），否则「G01」会被认成「1」
        head = w[:len(w) - len(n)] if w.endswith(n) and len(w) > len(n) else ""
        if not (w and n.endswith(w) or re.fullmatch(r"[\u4e00-\u9fa5]{1,4}", head or "")):
            continue
        # 「新乐专一路」的「一」也是数字，得和 1 等价地认
        cns = "零一二三四五六七八九"
        pat = "".join(f"[{d}{cns[int(d)]}]" if d.isdigit() else re.escape(d) for d in n)
        if n in flat or re.search(pat, flat):
            hit.append(r["lineName"])
    return max(hit, key=lambda n: len(norm_line(n)), default=None)


def _station_seq(city_name, line, direction):
    return _cached("stations", (city_name, line, direction), CATALOG_TTL,
                   lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                                  "DIRECTION": direction}).get("data") or [])


def _goes(city_name, line, a, b):
    """A 站是否在 B 站之前。返回 (方向, A 站序, B 站序)，都是 0 基；不顺路就 None。"""
    for d in ("1", "2"):
        names = [r.get("stationName") or "" for r in _station_seq(city_name, line, d)]
        # 先认同名，「汽车总站」不能被「北流汽车总站」顶掉
        idx = lambda s: next((i for i, n in enumerate(names) if n == s),
                             next((i for i, n in enumerate(names) if s in n), None))
        ia, ib = idx(a), idx(b)
        if ia is not None and ib is not None and ia < ib:
            return d, ia, ib
    return None


def _mins_to(hhmm):
    """距 hhmm 还有几分钟（按 24 小时取模，跨零点也对）。解析不了返回 None。"""
    try:
        h, m = (hhmm or "").split(":")[:2]
        tgt = int(h) * 60 + int(m)
    except ValueError:
        return None
    t = _now()
    return (tgt - (t.tm_hour * 60 + t.tm_min)) % (24 * 60)


def _hop_line(city_name, line, d, ia, ib):
    """这一路下一辆车几分钟到 A（上游预测），从 A 坐到 B 要几分钟（按每段均速累）。"""
    payload = _live_my(city_name, line, d, ia + 1)      # STATIONORDER 1 基
    eta = _etas(payload)
    if not eta:
        first, last = _first_last(city_name, line, d)
        if last and _past_last(last, first):
            return f"，末班 {last} 已经开走了" + (f"，首班 {first}" if first else "")
        # 起点站没到站预测时上游会写 tips，「等待发车」比「没车」准
        if "等待发车" in [r.get("busToStationTips") for r in payload.get("routeOnStationRTimeInfoList") or []]:
            return "，起点站的车在等发车"
        ahead = _mins_to(first)
        if ahead is not None and ahead < 12 * 60:
            return f"，首班 {first} 还没到"
        return "，这会儿没车报位置"
    return f"，下一辆约 {max(1, round(eta[0]))} 分钟后到，坐过去 {max(1, round(_run(payload, ia, ib)))} 分钟"


def render_hop(city_key, city_name, raw):
    """A 站到 B 站：拿两站的经停线路求交，再按站序验方向，验不过的不算直达。"""
    a, a_name = find_stop(city_name, raw[0])
    b, b_name = find_stop(city_name, raw[1])
    # 「从北流到玉林汽车总站」里的「北流」是地名不是站名：挑那一带能坐上车的站
    if not a and b:
        a = _place_stop(city_name, raw[0], b)
        a_name = (a or {}).get("stationName") or a_name
    elif not b and a:
        b = _place_stop(city_name, raw[1], a)
        b_name = (b or {}).get("stationName") or b_name
    if not a and not b:
        return None                              # 两边都不是站名，多半本来就不是问公交
    if not a or not b:
        return ""       # 有一边不是公交站（机场、外地地名）就交回给模型，别硬给一句话了事
    ra = set(a.get("relatedRoutes") or [])
    rb = set(b.get("relatedRoutes") or [])
    head = f"从{a['stationName']}到{b['stationName']}"
    if not ra & rb:
        plan = _hop_plan(city_name, a, a_name, b, b_name, ra, rb)
        if plan:
            return plan
        short = lambda s: "、".join(sorted(s)[:6]) + ("…" if len(s) > 6 else "")
        return f"{head}没直达车，{a['stationName']} 有 {short(ra)}，{b['stationName']} 有 {short(rb)}"
    direct = []
    for line in sorted(ra & rb)[:4]:            # shortcut: 只验前 4 条，交叉线多的城可能漏
        got = _goes(city_name, line, a_name, b_name)
        if got:
            direct.append((line, got))
    if not direct:
        plan = _hop_plan(city_name, a, a_name, b, b_name, ra, rb)
        return plan or f"{head}有同名线路但方向反了，得换乘"
    if len(direct) == 1:
        line, got = direct[0]
        return f"{head}坐 {line} 直达（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got)
    rows = [f"{head}能直达："]
    for line, got in direct:
        rows.append(f"{line}（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got))
    return "\n".join(rows)


def _place_stop(city_name, raw, other):
    """「从北流到…」：「北流」是地名，站牌上叫「北流汽车总站」「北流客运中心」。
    挑那一带能跟对面同一条线的站，挑不到就不猜。
    """
    rows = [r for r in stops(city_name, raw) or [] if r.get("stationName")]
    hit = next((r for r in rows
                if set(r.get("relatedRoutes") or []) & set(other.get("relatedRoutes") or [])), None)
    return _merge_same(rows, hit) if hit else None


def _hop_plan(city_name, a, a_name, b, b_name, ra, rb):
    """没直达车时算一次换乘：先坐 A 线到某站，再换 B 线到终点。

    「同一个地方」按坐标认——汽车总站（大北路）和汽车总站（二医院）站名不同，
    实际隔 140 米，走两步就能换上车。
    shortcut: 只算一次换乘、只取换得最早的那个方案，两次以上换乘交给模型说
    """
    ends = {}
    for r in _catalog(city_name).get("buslines") or []:
        ends.setdefault(r["lineName"], {})[str(r.get("upperOrDown"))] = r.get("to") or ""
    if a["stationName"] == b["stationName"]:
        return ""          # 站名匹到同一个站了，别答「从X到X换一次」
    legs = []
    for line in sorted(rb)[:4]:
        for d in ("1", "2"):
            pts = _seq_pts(city_name, line, d)
            ib = next((p["i"] for p in pts if b_name in p["name"]), None)
            if ib is not None:
                legs += [(line, d, p) for p in pts[:ib]]
    best = _plan_from(city_name, a_name, ra, legs)
    board = ()
    if not best:
        # 起点这一站的车接不上（机场 T1 只有 142/156，机场专线只停 T2）：到附近 1.5 公里内
        # 找一条能接上的线，走过去坐
        for d_m, r in sorted(((_dist_m(a, r) or 9999, r)
                              for r in stops(city_name, a_name[:2]) or []), key=lambda x: x[0]):
            if d_m > 1500 or r.get("stationName") == a["stationName"]:
                continue
            best = _plan_from(city_name, r.get("stationName") or "", set(r.get("relatedRoutes") or []), legs)
            if best:
                board = (d_m, r["stationName"])
                break
    if not best:
        return ""
    _, l1, d1, p1, (l2, d2, p2) = best
    walk = "" if p1["name"] == p2["name"] else \
        f"（跟{p2['name']}隔 {_dist_m(p1, p2)} 米，走两步）"
    return (f"从{a['stationName']}到{b['stationName']}没直达车，换一次：\n"
            + (f"先走 {board[0]} 米到 {board[1]}，" if board else "")
            + f"坐 {l1}（往{ends.get(l1, {}).get(d1) or '?'}）到 {p1['name']}{walk}，"
            f"换 {l2}（往{ends.get(l2, {}).get(d2) or '?'}）到 {b['stationName']}")


def _plan_from(city_name, at, lines, legs):
    """从 at 站上车，找第一个能在 legs 里接上的换乘点。"""
    best = None
    for line in sorted(lines)[:3]:
        for d in ("1", "2"):
            pts = _seq_pts(city_name, line, d)
            ia = next((p["i"] for p in pts if at in p["name"]), None)
            if ia is None:
                continue
            for p in pts[ia + 1:]:
                if p["i"] - ia >= (best[0] if best else 99):
                    continue
                hit = next((lb for lb in legs if lb[0] != line and _same_spot(p, lb[2])), None)
                if hit:
                    best = (p["i"] - ia, line, d, p, hit)
    return best


def _seq_pts(city_name, line, d):
    return [{"name": s.get("stationName") or "", "i": i,
             "lat": s.get("station_lat"), "lng": s.get("station_lon")}
            for i, s in enumerate(_station_seq(city_name, line, d))]


def _dist_m(p, q):
    """两站直线距离，够说「走两步」用的粗算；缺坐标返回 None。"""
    try:
        dlat = (float(p["lat"]) - float(q["lat"])) * 111000
        dlng = (float(p["lng"]) - float(q["lng"])) * 111000 * 0.92   # 北纬 22° 的经度收缩
    except (KeyError, TypeError, ValueError):
        return None
    return int((dlat ** 2 + dlng ** 2) ** 0.5)


def _same_spot(p, q, m=300):
    """同一个换乘点：站名不同但就在马路两侧。距离 0（同一个站）也算——`d or 9999` 会把 0 当没有。"""
    d = _dist_m(p, q)
    return d is not None and d <= m


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


def render_times(city_name, want, msg=""):
    """「G02 首班几点」问的是时刻表，不是实时车——有车没车都得回答时间。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    only = _dir(msg)
    out = []
    for name, row in hit:
        d = str(row.get("upperOrDown") or "")
        if d not in ("1", "2"):
            continue
        if only and only not in (row.get("to") or ""):     # 「往X方向的首末班」只念这个方向
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


def _stop_chelaile(city_name, msg):
    """车来了没有站点搜索接口：只能把目录里每条线的站表扫一遍找这个站（docs/车来了API.md §5.2）。"""
    cid = next((c.get("chelaile") for c in cities().values()
                if (c or {}).get("mygolbs") == city_name), None)
    if not cid:
        return ""
    try:
        rows = _cached("catalog-cl", cid, CATALOG_TTL, lambda: chelaile.lines(cid))
    except Exception:
        return ""
    note = f"（扫了 {min(len(rows), SWEEP_MAX)} 条线路）" if len(rows) > SWEEP_MAX else ""

    wants = [norm_city(x) for x in _cands(city_name, msg)]

    def one(row):
        n, lid = row
        try:
            sn = _cached("route-cl", (cid, lid), CATALOG_TTL,
                         lambda lid=lid: chelaile.stations(cid, lid))
        except Exception:
            return None
        # 两家站名不完全一样（「广西医科大玉林校区」↔「医科大学玉林校区」），得模糊匹
        hit = next((s for s in sn
                    if any(w in norm_city(s) or difflib.SequenceMatcher(None, w, norm_city(s)).ratio() >= 0.8
                           for w in wants)), None)
        return (n, hit) if hit else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = [g for g in pool.map(one, rows[:SWEEP_MAX]) if g]
    if not got:
        return ""
    names = []
    for n, _ in got:
        if norm_line(n) not in [norm_line(x) for x in names]:
            names.append(n)
    return f"{got[0][1]} 车来了这边有 {len(names)} 条线路{note}：" + "".join(f"\n{i}" for i in names)


def render_stop(city_key, city_name, msg, src=None):
    """「X 站有哪几路」：站点经停线路，每条带首末站。"""
    if src == "chelaile":       # 点名车来了就真给车来了的：两家站表不合并，线的集合也不一样
        out = _stop_chelaile(city_name, msg)
        if out:
            return out
    name = pick_stop(city_name, msg)
    if not name:
        return None
    # 再搜一次站名会撞上同名地铁站（「芳村」和「芳村（地铁站）」），按站名精确挑
    rows_ = stops(city_name, name) or []
    if not rows_:
        return None
    row = next((r for r in rows_ if r.get("stationName") == name), rows_[0])
    groups = _same_rows(rows_, row)
    if not any(rs for _, rs in groups):
        return None
    rows = _catalog(city_name).get("buslines") or []
    ends = {}
    for r in rows:
        ends.setdefault(r["lineName"], []).append(f"{r.get('from')} ↔ {r.get('to')}")
    # 别截 12 条：用户要的是全部，截了还得再问一轮「剩下的呢」，而那轮多半认不出站名
    out = ""
    if len(groups) == 1:
        routes = groups[0][1]
        items = [f"{n} {ends[n][0]}" if ends.get(n) else n for n in routes if ends.get(n)] or list(routes)
        out = f"{name}有 {len(items)} 条线路：" + "".join("\n" + i for i in items)
    else:
        # 同一片站台被拆成几条记录（汽车总站 / （二医院）/ （大北路）），说清哪路车在哪个站台等
        seen, blocks = set(), []
        for gname, routes in sorted(groups, key=lambda g: -len(g[1])):
            items = [f"{n} {ends[n][0]}" if ends.get(n) else n for n in routes
                     if n not in seen and ends.get(n)]
            seen |= set(routes)
            if items:
                blocks.append(f"{gname}（{len(items)} 条）：" + "".join("\n" + i for i in items))
        out = (f"{name}这一片分 {len(blocks)} 个站台，共 {len(seen)} 条线路：\n" + "\n".join(blocks))
    # 点名车来了却没扫到，别让掌上的结果冒充车来了的
    return out + ("\n（车来了没扫到这个站，上面是掌上的）" if src == "chelaile" else "")


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
    try:
        rows = _cached("catalog-cl", cid, CATALOG_TTL, lambda: chelaile.lines(cid))
    except Exception:
        return ""
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


def render_sweep(city_name, want_plates=False, only=None):
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
    if only:      # 「这四条线路哪个在跑」：只扫这个站的线路，别拿全城目录答
        want = {norm_line(x) for x in only}
        names = [n for n in names if norm_line(n) in want]
        if not names:
            return ""
    if not names:      # 掌上这座城没有线路目录（北海就是），换车来了扫
        return _sweep_chelaile(city_name)
    # 抽 3 条线看有没有实时：只问第一条，一条线的 hasReal 就能让整城短路
    if all(str(_live_my(city_name, n, dirs[n][0]).get("hasReal")) == "0" for n in names[:3]):
        if only:      # 站上的线查不出实时，别拿全城扫替答
            return ""
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
        cars = list(pool.map(lambda j: _live_my(city_name, j[0], j[1]).get("list") or [], jobs))
    per, pl = {n: 0 for n in names}, {}
    for (n, _), cs in zip(jobs, cars):
        per[n] += len(cs)
        pl.setdefault(n, []).extend(cs)
    live = sorted(((n, c) for n, c in per.items() if c), key=lambda x: -x[1])
    if only:      # 问的是这几条线：哪条在跑、哪条没车，都得说，别只报在跑的
        def row(n):
            if not per[n]:
                return f"\n{n} 没车报位置"
            if want_plates:
                return f"\n{n}：" + "、".join(
                    f"{c.get('busNumber')}（{c.get('stationName') or '在途'}）" for c in pl[n][:6]
                ) + (f" 等 {per[n]} 辆" if per[n] > 6 else "")
            return f"\n{n} {per[n]} 辆"
        return f"{city_name}这 {len(names)} 条线这会儿：" + "".join(row(n) for n in names)
    if not live:
        if note:
            return f"{city_name}这会儿抽查了 {len(names)} 条主线，都没车报位置，可能都收班了"
        return f"{city_name}这会儿全线都没车报位置，可能都收班了"
    if want_plates:      # 「每辆车的车牌」：掌上给 busNumber，车来了那套是内部编号，不冒充车牌
        # shortcut: 每线只列前 6 辆，线多的城要用户点名查
        return (f"{city_name}这会儿在跑的车{note}："
                + "".join("\n" + n + "：" + "、".join(
                    f"{c.get('busNumber')}（{c.get('stationName') or '在途'}）" for c in pl[n][:6])
                    + (f" 等 {len(pl[n])} 辆" if len(pl[n]) > 6 else "")
                    for n, _ in live))
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


def _one(city_key, name, num, prefix, msg, src, explicit, last):
    """一条线路的回答：时刻表 / 公司 / 站表 / 到站 / 实时，按问法选。"""
    wants = ([prefix + num] if prefix and norm_city(prefix) != city_key else []) + [num]
    # 目录里的线名常省掉行政后缀：「东胜区8路」在鄂尔多斯目录里叫「东胜8路」
    bare = re.sub(r"(区|县|旗|市|镇)$", "", prefix) if prefix else ""
    if len(bare) >= 2 and bare != prefix:
        wants.insert(len(wants) - 1, bare + num)
    # 有前缀要纠前缀截短；「环山二号线」这种自带汉字的线名也要纠（前缀是空的）
    if name and (prefix or not num[:1].isdigit()):
        cat = _catalog(name)
        # 网络挂了才不敢兜底：目录取不到时裸号会答成另一条线（「新乐专1路」→ 1 路）；
        # 业务上的「这城没目录」照旧往下走
        if cat.get("error") and "detail" in cat:
            return cat.get("msg") or ""
        # 前缀被 PREFIX_RE 截短过（「新乐专1路」→「庄新乐专」），目录里有真线名就用真的
        full = _exact_line(name, msg, num)
        if full:
            wants = [norm_line(full)]      # wants 里存的向来是归一化后的号
    last["line"] = wants[0]
    if name:
        for w in wants:
            if TIMES_RE.search(msg):          # 问首末班时刻表
                out = render_times(name, w, msg)
            elif COMPANY_RE.search(msg):      # 问公司/找失物/投诉
                out = render_company(name, w)
            elif STOPS_RE.search(msg):       # 要的是站表，别回实时车
                out = render_stops(name, w)
            else:
                out = render_at(city_key, name, w, msg)
            if out:
                return out
    vm = VEH_RE.search(msg)
    veh = next((g for g in vm.groups() if g), "") if vm else ""
    txt = render(city_key, wants[0], query(city_key, wants, src, veh), veh, _dir(msg))
    # 没明写城市就按默认城市查，查不到时别让用户以为「这线路不存在」
    if not explicit and txt.startswith("没查到"):
        txt += f"\n（你没写城市，我按{norm_city(city_key)}查的，别的城市前面带上城市名）"
    return txt


def answer(msg, default_city, ctx="", key=""):
    """ctx 是引用/上文里的文字，只用来认城市——追问「那 39 路往哪开」靠它。"""
    last = _last(key)
    t0 = time.time()
    msg = MARKUP_RE.sub(" ", AT_RE.sub(" ", msg))
    raw = msg
    sm = SRC_RE.search(msg)
    src = ("chelaile" if sm.group("cl") else "mygolbs") if sm else None
    # 「用掌上公交查G02路」：「掌上公交查」不是城市名，别让前缀截出「上公交」当城
    msg = SRCPRE.sub("", msg)
    line = parse(msg)
    if not line and ctx and not re.search(r"在跑|运营|哪条|哪些|全部|车牌|号牌", msg):
        # 引用别人那句追问「这路还有几辆」，线路号在被引用的话里。
        # 但本句问的是全城（「还有没有在跑的线路」「每辆车的车牌」）时不能拿引用里那一路顶替——
        # 引用的是「还有 5 条在跑」，parse(ctx) 会随便挑一条线答成它的实时
        line = parse(ctx)
    # 「从车来了的那个看一下数据」没写线路号：说的就是上一条问的那路车
    inherited = False
    if src and not line and last.get("line") and re.search(r"看|查|数据|接口", raw):
        line = (last["line"], "")
        inherited = True
    prefix = line[1] if line else ""
    # 「泉州公交11路」「泉州公交K502」：前缀截到的是「泉州公交」，去掉「公交」才是城市名，
    # 留着就会拼成「泉州公交K502」这种不存在的线
    prefix = re.sub(r"公交$", "", prefix)
    if prefix in ("那", "这", "该", "此", "本"):      # 指示词不是县名，别拼进线路名
        prefix = ""
    city_key, explicit = detect_city(msg + " " + ctx, prefix, default_city)
    base = norm_city(default_city)
    # 前缀自己就是一座城（「玉林 容县 11 路」）时以它为准：玉林的目录里没有容县11路，
    # 拿玉林的目录查裸号会静默答成玉林 11 路
    if prefix and norm_city(prefix) in cities():
        city_key, explicit = norm_city(prefix), True
    # 同群接着问（「那 K502 呢」）：没明写城市就沿用上一条问过的城市，别落回默认城。
    # 带地名前缀的（「陆川1路」）不适用——刚问过鄂尔多斯就问陆川1路，会被答成鄂尔多斯 1路
    if line and not explicit and not prefix and last.get("city") in cities() and last["city"] != city_key:
        city_key, explicit = last["city"], True
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
    prev = last.get("city")
    last["city"] = city_key
    got = hop(msg)
    # 先按当前城市的目录真查一遍：北流是县级市、自己有一条「北流汽车总站→玉林北站」的
    # 北流105路，只看两个站名属于哪座城会把这条线误判成跨城
    out = render_hop(city_key, name, got) if (got and name) else None
    # 站名里带裸县名（「元氏火车站」）：先在那个县所在的城查，别落回默认城交白卷
    if not out and got and not explicit:
        dc = _district_city(got[0] + got[1])
        dname = (cities().get(dc) or {}).get("mygolbs") if dc and dc != city_key else None
        if dname:
            out = render_hop(dc, dname, got)
            if out:
                city_key, name = dc, dname
                last["city"] = dc
    # 「从机场一号航站楼到元氏火车站」没写城市：默认城查不到，沿用上一条问过的城
    if not out and got and not explicit and prev in cities() and prev != city_key:
        city_key, name = prev, (cities().get(prev) or {}).get("mygolbs")
        last["city"] = city_key
        out = render_hop(city_key, name, got)
    if out:
        return out
    if got:
        ca = detect_city(got[0], "", "")[0]
        cb = detect_city(got[1], "", "")[0]
        # 两头都得站得住：要么站表里有这个站，要么名字自己带着别的城名。
        # 「玉林汽车站」平台没这个站、名字也没带外地城名，说成跨城等于替用户编地名
        known = lambda x: bool(stops(name or city_key, x) or detect_city(x, "", "")[0] not in ("", city_key))
        if ca and cb and ca != cb and known(got[0]) and known(got[1]):
            return f"{got[0]}在{ca}，{got[1]}在{cb}，跨城的公交串不起来，这段得坐高铁或长途"
    if got and name:
        return out or ""      # render_hop 返回空串 = 有一边不是公交站，交回给模型
    # 「那玉林北站定制夜班车呢？」线名不是数字+路，parse 认不出；目录里有就当场认
    if not line and name:
        hit = _msg_line(name, msg)
        if hit:
            line, prefix = (hit, ""), ""
    if name and TRANSFER_RE.search(msg):
        # 「这条线路每站能换什么」没有线路号，用上一条答过的线路
        want = (prefix + line[0] if prefix and not explicit else line[0]) if line else last.get("line") or ""
        out = render_transfer(name, want) if want else None
        if out:
            last["line"] = want
            return out
    if line:
        # 「车来了查XX校区的线路」带站点，别因为上一条问过 9 路就答成 9 路
        if inherited and name:
            out = render_stop(city_key, name, msg, src)
            if out:
                return out
        # 一句里问了两条线（「樟13 / 12路」）就都答，别只答第一条
        multi = _lines(msg)
        if len(multi) > 1:
            outs = [t for t in (_one(city_key, name, n, pre, msg, src, explicit, last)
                                for n, pre in multi) if t]
            if outs:
                return "\n\n".join(outs)
        # 前缀是线路名的一部分（容县11路），明写城市也不该丢——explicit 只管去哪个目录找
        return _one(city_key, name, line[0], prefix, msg, src, explicit, last)
    if name:
        # 「还运营吗」「哪条线在跑」没有站名，别拿去当站名搜（哈尔滨 → 哈尔滨站）
        pl = re.search(r"车牌|号牌", msg)
        if pl or (SWEEP_RE.search(msg) and not re.search(r"站|几路|哪路", msg)):
            # 「这四条线路哪条在跑」范围是刚才那个站，不是全城：引用里那句站表也算
            sname, only = _stop_lines(name, msg + " " + ctx)
            # 没引用的紧接追问（「那这四条线路哪条在跑」）靠指示词认，别把「全城还有哪些在跑」也收窄
            if (not only and last.get("stop")
                    and re.search(r"这四|这几|这些|上面|刚才|四条|几条|哪条|哪个", msg)):
                only = _stop_lines(name, last["stop"])[1]
            return render_sweep(name, bool(pl), only or None)
        out = render_stop(city_key, name, msg, src)
        # 「伦教街道办事处公交线路」没写城市：默认城没这个站就沿用上一条问过的城，
        # 佛山 342 路问完接着问伦教，别去玉林的站表里找
        if not out and not explicit and prev in cities() and prev != city_key:
            pname = (cities().get(prev) or {}).get("mygolbs")
            out = render_stop(prev, pname, msg, src) if pname else None
            if out:
                last["city"] = prev
        if out:
            # 记一句是哪个站，「这四条线路哪条在跑」要靠它收窄，不能拿全城目录答
            last["stop"] = pick_stop(name, msg) or last.get("stop")
            return out
        # 站名猜不出是哪座城的：问一句，别回空让模型自己圆话（「返回的是玉林全市正在跑的线路」）
        if not explicit and re.search(r"站|线路|几路", msg):
            hint = re.sub(r"公交$", "", STOPSUF.sub("", TAIL.sub("", msg))).strip()
            return (f"「{hint}」这个站没查到，是哪座城的？说个城市名我再查" if hint
                    else "这个站没查到，是哪座城的？说个城市名我再查")
    # 这一句只给「刚刚真的没连上、而且问的是公交」的情况：以前按时间窗判断，
    # 任何一句闲聊都会收到「上游没响应」，成了「彻底变成问公交工具」的由来
    return "上游这会儿没响应，过会儿再问" if (BUS_RE.search(msg) and _up_fail[0] >= t0) else ""
