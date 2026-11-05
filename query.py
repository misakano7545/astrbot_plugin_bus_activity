"""全国公交实时查询：掌上公交优先，车来了兜底。

只问「有没有车、车到哪」，不做换乘、不做历史。
接口细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md 与 docs/车来了API.md。
"""

import difflib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yulin          # 玉林导乘资料：只在上游给不出的地方补（换乘站表、上车点、联营线无实时）

try:  # AstrBot 按包加载时走相对导入，单文件调试时退回顶层模块
    from . import chelaile
    from .plates import PLATE
except ImportError:
    import chelaile
    from plates import PLATE

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

# 带字母的线号：B601 / K502 / D01 / 121A / 1001M / C621A（字母在前在后都有，最多五位）
# 必须至少含一个字母，否则「11路」「2026年」会被当线号
# 字母前后各最多一个：「9KB」「500MB」「3.2GB」这种量纲不是线号
# shortcut: 目录抽了 7 城 7 千多条线名，没有「数字+两个字母」的线号，出现再放宽
ALPHA_PAT = (r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]{2,6}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])"
             r"[A-Za-z]{0,1}\d{1,4}[A-Za-z]{0,1}(?![A-Za-z0-9])")
# 香港一个「1」对应五家公司（城巴/九巴/屿巴/新界小巴/港岛小巴）：点名单算一家，
# 长名字排前面，「小巴」放最后当统称（港岛小巴/新界小巴都含这两个字）
OPS = ("新界小巴", "港岛小巴", "九龙小巴", "城巴", "九巴", "屿巴", "龙运", "新巴", "港铁", "小巴")
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
    r"|(?P<alpha>" + ALPHA_PAT + r")(?:\s*路)?"
    # 带前导零的四位线号（重庆「0492」）口语里不加「路」字；裸的 492 不加路字就太宽（可能是数量）
    r"|(?<![0-9])(?P<zero>0\d{2,3})(?![0-9])"
    r"|(?<![0-9])(?P<num>\d{1,4})\s*(?:夜宵|夜班|通宵)?\s*[路线](?P<qual>夜班|区间|支线|快线|专线)?"
    # 「131区间」不带「路」也算线路——石家庄目录里就叫「131路区间」
    r"|(?<![0-9])(?P<inum>\d{1,4})\s*(?P<iqual>区间|支线|快线|专线)"
    r"|(?P<cnnum>[一二三四五六七八九十]{1,3})\s*路"
)
PREFIX_RE = re.compile(r"([\u4e00-\u9fa5]{1,4})\s*$")
# 「在市政广场要等多久等到17路」：问法黏在线号前面，先从句尾剥掉，剩下的才是站名
ASKPAST = re.compile(r"(?:还要|要|想)?\s*等?\s*(?:多久|多长时间|几分钟|几站)?\s*(?:才[能会]?)?\s*(?:等到|等|到)?$")
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
# 完整车牌：省简称 + 字母 + 5 位（普通）或 6 位（新能源，闽A17997D）
PLATE_PAT = (r"[京津沪渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新]"
             r"[A-Z][A-Z0-9]{4,6}(?![A-Za-z0-9])")
BUS_RE = re.compile(
    r"公交|巴士|公共汽车|公车|地铁|轨道交通|几路|哪路|路车|末班|首班|到站|几站|\d\s*号线|号线|"
    r"怎么坐|直达|换乘|车来了|掌上公交|车牌|号牌|自编号|自编|夜宵线|夜班线|(?<![今昨天每这那晚])夜班车|通宵线|(?<![0-9])0\d{2,3}(?![0-9])|\d{1,4}\s*[路线]|有(?:几|多少)条?线路|有哪些线路|哪几条线路|"
    r"(?:城巴|九巴|屿巴|新巴|龙运|小巴)\s*\d{1,4}|" + PLATE_PAT + r"|" + ALPHA_PAT +
    r"|[\u4e00-\u9fa5A-Za-z0-9]{2,10}(?:专线|快线|支线)|[\u4e00-\u9fa5]{2,6}[东南西北中]\d{1,4}(?![0-9])")
# 纯文字线名（「妇幼专线」「宝钢专线」）不含数字，也不是公交词——靠这两个专有线名尾巴认
TEXT_LINE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,10}(?:专线|快线|支线)")
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


def _line_pre(pre):
    """线名自带的县级市前缀：「安宁12」的安宁属昆明，可城市键被兰州的安宁区占了，
    剥掉前缀就会匹到昆明市区的 12 路。区县名自己（安宁区）不算这种前缀。"""
    if not pre or (pre in DISTRICTS and not pre.endswith("市")):
        return ""
    host = (cities().get(pre) or cities().get(norm_city(pre)) or {}).get("mygolbs") or ""
    return norm_city(pre) if host and norm_city(host) != norm_city(pre) else ""


def parse(msg):
    """→ (线路号, 紧贴着的汉字段)。县内线路「陆川1路」靠这个前缀区分。"""
    # 「9999x 8123等于多少」：乘号连着两个数字是算式，不是线号。
    # 数字+一个字母现在是合法形态（121A/1001M），9999X 光看形态分不出来
    if re.search(r"\d\s*[x×*]\s*\d", msg, re.I):
        return None
    m = LINE_RE.search(msg)
    # 「3D 打印」「5G 网络」「4K 视频」：一位数字＋量纲字母不是线号。
    # shortcut: 只看一位数，香港 10M/11D/11K 这种两位数线号照样认；哪天真有一位数线号再放宽
    if m and m.group("alpha") and re.fullmatch(r"[1-9][GDMK]", m.group("alpha")):
        return None
    if not m:
        # 「香港城巴1」香港人不带「路」字：算子名＋号也算一条线，前缀里留住算子名
        m = re.search(r"(" + "|".join(OPS) + r")\s*(\d{1,4})(?![0-9])", msg)
        if not m:
            # 「牡丹江21」：城市名＋线号，口语里不带「路」字。整句只有「城市名＋数字」才认，
            # 多一个字就是句子（「玉林2023年GDP」），留给模型，别把年份当线路
            t = re.sub(r"[?？。!！,，\s]+$", "", AT_RE.sub("", msg).strip())
            # 号后面跟一句问法（「绵阳25首末站」）先把问法剥掉，剩下的才拿来整句比
            t = re.sub(r"(?:的)?(?:" + ENDS_RE.pattern + "|" + TIMES_RE.pattern + "|"
                       + PRICE_RE.pattern + r").*$", "", t)
            t = TAIL.sub("", t)          # 「成都东16现在有车吗」：问法剥掉才是整句「城市名+号」
            m = re.fullmatch(r"([\u4e00-\u9fa5]{2,6})(\d{1,4})", t)
            if m and m.group(1)[-1] in "快慢" and m.group(1)[:-1] in cities():
                # 「柳州快1」：快/慢 是线名的一部分（目录里叫「快1号线」），不是城市名的尾巴
                return norm_line(m.group(1)[-1] + m.group(2)), m.group(1)[:-1]
            if not m or m.group(1) not in cities():
                # 「成都东16」：方位字是线名的一部分（成都目录里叫东16路），城市名后面剩一个方位字
                if m:
                    for i in range(len(m.group(1)) - 1, 1, -1):
                        if m.group(1)[:i] in cities() and m.group(1)[i:] in ("东", "南", "西", "北", "中"):
                            return norm_line(m.group(1)[i:] + m.group(2)), m.group(1)[:i]
                return None
            # 「安宁12」：城市键被同名区占掉了（掌上只有兰州的安宁区），线名里的前缀还得留着
            if _line_pre(m.group(1)):
                return norm_line(_line_pre(m.group(1)) + m.group(2)), m.group(1)
            return m.group(2), m.group(1)
        pre = JUNK_TAIL.sub("", JUNK_HEAD.sub("", msg[: m.end(1)]))
        return m.group(2), _snap(pre)
    # 「环山二号线」的线名自己就带汉字前缀，别再把它当县名重复拼一次
    # 先剥掉整段的动词再取尾巴：PREFIX_RE 只吃 4 个字，「帮我查一下东胜区」窗口里会剩
    # 「下东胜区」这种半截词，当成站名/线名前缀去查就成了一条不存在的线
    head = LEAD.sub("", ASKPAST.sub("", JUNK_HEAD.sub("", msg[: m.start()])))
    pre = "" if m.group("numline") else PREFIX_RE.search(head)
    num = (m.group("inum") + m.group("iqual")) if m.group("inum") else \
        (m.group("alpha") or m.group("num") or m.group("zero") or m.group("metro") or m.group("numline")
         or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
    pre = JUNK_TAIL.sub("", JUNK_HEAD.sub("", pre.group(1) if pre else ""))
    # 「柳州快1路」：快/慢 粘在线号前，被当成了前缀的尾巴——挪回线名里
    if pre[-1:] in ("快", "慢") and (not pre[:-1] or pre[:-1] in cities()):
        num, pre = pre[-1] + num, pre[:-1]
    # 「安宁12」：安宁市属昆明，可掌上只有兰州的安宁区，城市键被占了，线名里还得留着前缀
    # （安宁K12）。剥掉会匹到昆明市区的 12 路，答成别人的车。
    if pre and _line_pre(pre):
        num = norm_line(_line_pre(pre) + num)
    return norm_line(num), _snap(pre)


def _digitize(s):
    """线名里的中文数字换阿拉伯：「地铁一号」→「地铁1号」——目录里只有阿拉伯写法。"""
    return re.sub(r"[零一二三四五六七八九十两]{1,3}",
                  lambda m: str(_cn_num(m.group(0)) or m.group(0)), s)


# 群友把脚本贴进来时别当公交查：`x0 = iroot(n, d)` 里的 x0 被 ALPHA_PAT 认成线路号，
# 回一句「没查到「X0」这条线，南宁这边有的是 20路…」，还被群里人当笑话
CODE_RE = re.compile(r"def\s+\w+\s*\(|import\s+\w+|return\s|print\(|range\(|lambda\s|\*\*|```|"
                     r"while\s+\w|for\s+\w+\s+in\s|#include|console\.log|function\s*\(")


def looks_like_ask(msg):
    """这句像在问公交吗。快路命中线路号后先用它过一道，别把闲聊抢走。"""
    if CODE_RE.search(msg):          # 代码里的标识符不是线号
        return False
    if not re.search(r"[\u4e00-\u9fa5]", msg):
        return False                 # 一句中文都没有（「baga yalu: k3 4.1 gtp」）不是问公交，是闲聊
    return bool(ASK.search(msg)) or not CHAT.search(msg)


def _lines(msg):
    """句子里问了哪几条线：「樟13 / 12路」「11路和12路」都要答，别只答第一条。"""
    out, end = [], -1
    for m in LINE_RE.finditer(msg):
        if m.start() < end:            # 「131路区间」先匹到「131路」，别再算一条
            continue
        end = m.end()
        num = (m.group("inum") + m.group("iqual")) if m.group("inum") else \
            (m.group("alpha") or m.group("num") or m.group("zero") or m.group("metro") or m.group("numline")
             or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
        # 只认紧贴着的汉字前缀：多行里上一行的「樟」不算 12 路的前缀
        cut = msg[: m.start()]
        # 「地铁3/11号线」：前面的「3/」不是前缀，剥掉才能取到「地铁」
        slash = re.search(r"(\d{1,4})\s*/\s*$", cut)
        cut = cut[: slash.start()] if slash else cut
        pre = re.search(r"([\u4e00-\u9fa5]{1,4})$", JUNK_HEAD.sub("", cut))
        # 「环山二号线」的线名自带汉字，别再当县名拼一次；「地铁3/11号线」除外，它真带「地铁」
        pre = "" if m.group("numline") and not slash else JUNK_TAIL.sub(
            "", JUNK_HEAD.sub("", pre.group(1) if pre else ""))
        # 「11路和12路」：前缀截到的是「路和」，不是县名
        pre = re.sub(r"公交$", "", pre)
        pre = re.sub(r"^(?:路|和|与|跟|及|、)+|(?:路|和|与|跟|及)+$", "", pre)
        if pre in ("那", "这", "该", "此", "本"):
            pre = ""
        # 「苏州地铁3/11号线」：一个后缀管两个号，两条都要答；前缀一起带上，
        # 不然前一个号会被当成「3 路」
        if slash and pre and pre not in cities():
            # 前缀可能是「苏州地铁」：城市名剥掉，留下的「地铁」才是线名的一部分
            for ck in cities():
                if pre.startswith(ck) and len(pre) > len(ck):
                    pre = pre[len(ck):]
                    break
            out.append((norm_line(_digitize(pre + slash.group(1) + num[-1:])), ""))
            out.append((norm_line(_digitize(pre + num)), ""))
            continue
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
        # 区名剥掉后缀会顶掉同名县级市：「安宁」在掌上只有兰州的安宁区，用户说「安宁」
        # 多指昆明的安宁市。两边指向不同城、且父城在表里时，按县级市走。
        for _k, _v in DISTRICTS.items():
            _b = norm_city(_k)
            if not _k.endswith("市") or _b not in idx:
                continue
            _host = idx[_b].get("mygolbs") or ""
            if _host != _k and DISTRICTS.get(_host) not in (None, _v) and norm_city(_v) in idx:
                idx[_b] = dict(idx[norm_city(_v)])
                idx[_host] = {"mygolbs": DISTRICTS[_host]}   # 明写「安宁区」还查兰州，裸「安宁」才归昆明
        # 县级市掌上认（吴川市），车来了只给地级市的 id：补一份父城的，否则只在车来了
        # 目录里的线（吴川2路（梅菉-霞山旧线））永远查不到。只补掌上认得的城，区名不碰
        for _k, _v in DISTRICTS.items():
            _e = idx.get(norm_city(_k))
            if not _e or _e.get("chelaile") or not _e.get("mygolbs"):
                continue
            _p = idx.get(norm_city(_v)) or {}
            if _p.get("chelaile"):
                _e["chelaile"] = _p["chelaile"]
                _e["_borrow"] = True      # 借来的 id 只查带地名的线名，裸号会答成父城的车
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
    # 目录把号写在括号里（「大容山旅游专线（28路）」），归一化时括号连同号一起被剥掉，
    # 拿裸号 28 去比永远「没这条线」
    if want.isdigit():
        alias = [c for c in catalog
                 if want in re.findall(r"[（(]\s*(\d{1,4})\s*[路线]", c[0])]
        if alias:
            return alias
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


def _window(first, last):
    """(首班, 末班) 相对现在的位置：before 还没发首班 / open 在运营窗口内 / after 末班开走了。
    空串表示不知道。只回答「末班过了没」是不够的——夜班车早上问，末班当然没过，
    但车也不可能在路上（实测玉林 6路夜班 10:16 被答成「线路还在跑，只是车机没报位置」）。"""
    def mins(s):
        try:
            h, m = (s or "").split(":")[:2]
            return int(h) * 60 + int(m)
        except ValueError:
            return None

    last_m = mins(last)
    if last_m is None:
        return ""
    t = _now()
    now_m = t.tm_hour * 60 + t.tm_min
    first_m = mins(first)
    if first_m is None:
        # 没首班就按「末班离现在多久」算，跨零点自然成立：00:02 问 22:40 → 已开走，
        # 23:00 问 00:10 → 还没到。
        # shortcut: 以 12 小时为界判过去/将来，早班车式的末班（06:00）在下午会判错
        return "after" if (last_m - now_m) % (24 * 60) > 12 * 60 else "open"
    # 按运营窗口判：末班 23:44 的线路，早上 9 点当然还没开走
    if (now_m - first_m) % (24 * 60) <= (last_m - first_m) % (24 * 60):
        return "open"
    # 窗口外也是两种情况：末班 21:30 的夜班车中午问「离首班更近」，那是还没开，
    # 不是已经收班——不分开就会回「末班已经开走了」
    return "before" if (first_m - now_m) % (24 * 60) < (now_m - last_m) % (24 * 60) else "after"


def _past_last(last, first=""):
    """末班过了没。字符串比较跨不了零点（'00:20' < '22:40'），按分钟算。"""
    return _window(first, last) == "after"


def _window_note(w):
    """w = [(方向名, 首班, 末班)]，一个线号两个方向各有时刻。没车时说清现在是「没开」还是「收班」。
    都还在运营窗口内就返回空串——让调用方按原来的「车机没报位置」说。"""
    rows = [(lab, f, l, _window(f, l)) for lab, f, l in w if _window(f, l)]
    if not rows or all(s == "open" for *_, s in rows):
        return ""
    now = _now()
    st = {s for *_, s in rows}
    when = ("现在 %d:%02d，首班还没到" % (now.tm_hour, now.tm_min) if st == {"before"}
            else "末班已经开走" if st == {"after"} else "各方向运营时间不一样")
    return when + "：" + "；".join((f"往{lab} " if lab else "") + f"{f}–{l}"
                                   for lab, f, l, _ in rows)


def _seq_payload(city_name, line, direction):
    """CMD=103 整包（站序 + 首末班 + 票价的 commonts），首末班和票价共用这一份缓存。"""
    return _cached("firstlast", (city_name, line, direction), CATALOG_TTL,
                   lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                                  "DIRECTION": direction}))


def _first_last(city_name, line, direction):
    """(首班, 末班)，取不到给空串。只在整车数为 0 时调用，回答「是不是收班了」。"""
    fl = _seq_payload(city_name, line, direction).get("firstLast") or []
    return (min((x.get("first") or "" for x in fl), default=""),
            max((x.get("last") or "" for x in fl), default=""))


def _price(city_name, line):
    """票价只藏在 103 的 commonts 那句话里，各家写法不一样：
    「票价：2元/人次」「票价3元」「单程票价4元一票制。石家庄公交…」「票价2元。\\n热线…」
    shortcut: 只认带「元」的阿拉伯数字写法，中文数字票价（「票价一元」）会漏；只取方向 1，
    分段计价的线会不准。遇到了再补。
    """
    m = re.search(r"票价[:：]?\s*([^。\n；;]{1,30})",
                  _seq_payload(city_name, line, "1").get("commonts") or "")
    v = re.split(r"[\s，,（(]", (m.group(1) if m else "").strip())[0]
    return v if "元" in v else ""


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
    groups, live_known, first, last, live_flags, wins = [], False, "", "", [], []
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
            # 上下行各有自己的首末班，得按方向留着（玉林 6路夜班 17:47–22:40 / 18:47–23:10）
            if f0 and l0:
                wins.append((row.get("to") or "", f0, l0))
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
            ln = hit[0][1]["lineName"]
            # 联营线（玉林-樟木）本来就不给车辆定位，说成「上游不给」会被当成平台抽风
            note = yulin.no_real(ln) or f"上游不给{norm_city(city_name)}的实时车辆位置"
            lname, ldir = hit[0][1]["lineName"], hit[0][1]["upperOrDown"]
            n = len(_station_seq(city_name, lname, ldir))
            # 「末班最晚 20:30」是上下行混在一起的一个数：过了末班还说「起、末班最晚」，
            # 等于没说清这两个方向都收车了。有这个就按方向报，并说明现在是不是出窗口了
            wl = _window_note(wins)
            bits = [f"{first} 起、末班最晚 {last}"] if first and last and not wl else []
            if n:
                bits.append(f"{n} 站")
            if bits:
                note += "，这条线 " + "、".join(bits)
            if wl:
                note += "，" + wl
        elif (_wn := _window_note(wins)):
            note = _wn
        elif live_known:
            note = "线路还在跑，只是车机没报位置"
    return {"source": "掌上公交", "line": hit[0][1]["lineName"], "groups": groups,
            "note": note, "unknown": unknown}


def _chelaile(city_id, want):
    rows = _cached("catalog-cl", city_id, CATALOG_TTL, lambda: chelaile.lines(city_id))
    # 「香港城巴1」：同号五家公司各有车，点了名就只留这一家（名字里的括号就是公司）
    op = next((o for o in OPS if o in want), "")
    if op:
        rows = [r for r in rows if op in r[0]]
        want = want.replace(op, "")
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
            groups.append((f"{to}（{co.group(1)}）" if co and not op else to, d["buses"]))
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
            # 县级市借父城的车来了 id：「吴川9路」不能拿湛江市区的 9 路顶
            if source == "chelaile" and ent.get("_borrow") and not re.search(r"[\u4e00-\u9fa5]", want):
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
    # 「北流城南站方向还有没有」不带「往」，也得认；但「那个方向」「哪个方向」不是站名。
    # 先按带「往/去/到」的语法走，别让「219路往呈贡广场方向」把线号也吃进方向名
    msg = msg or ""
    m = re.search(r"(?:往|去|到)\s*([^，,。.？?！!\s]{2,12}?)\s*方向", msg)
    if m:
        return m.group(1)
    for m in re.finditer(r"(?:查看|看看|查一下|问一下|查|看|问)?\s*"
                         r"([^，,。.？?！!\s]{2,12}?)\s*方向", msg):
        if m.group(1)[0] not in "这那哪该此各两同反":
            return m.group(1)
    return ""


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
    if total:
        # 绵阳 25 路这种一条线号十三个支线：同一个终点名合并，方向多过 4 个时只说有车的，
        # 否则整屏都是「往X还没车」
        merged = {}
        for lab, bs in groups:
            merged.setdefault(lab or "", []).extend(bs)
        groups = list(merged.items())
    if not total:
        # 「没车」和「上游没给车辆位置」是两件事，后者会被乘客读成停运（见 issue #2）
        if "地铁" in norm_line(line):           # 地铁进得了目录，但全程 hasReal=0
            return f"{line} 没有实时车辆位置，地铁不上报车机，站表和首末班能查"
        tail = res.get("note") or res.get("desc") or ""
        head = f"{line} 查不到实时车" if res.get("unknown") else f"{line} 这会儿没车"
        return head + ("，" + tail if tail else "")
    multi = sum(1 for _, b in groups if b) > 1      # 只有一个方向有车时不念空方向
    many = len(groups) > 4                          # 支线网络，空方向属于噪音
    if many:                                        # 「25路(五大队观太)」不代表别的支线，抬头去掉括号
        line = re.sub(r"[（(].*?[)）]", "", line)
    rows = [f"有，{line} 现在 {total} 辆在路上" if not veh else f"{veh} 在 {line} 上："]
    for label, buses in groups:
        if not buses:
            if multi and label and not many:
                rows.append(f"往{label}还没车")
            continue
        rows.append((f"往{label}：" if label else "") + "，".join(_car(*b) for b in buses))
    return "\n".join(rows)


def _near(city_key, want, n=4):
    """同城里跟它最像的几条线：K502 不存在就报 K501、K503，别让人以为这城根本没数据。"""
    name = (cities().get(city_key) or {}).get("mygolbs")
    rows = (_catalog(name).get("buslines") or []) if name else []
    w = norm_line(want)
    # 「安宁12」：线名自带地名（安宁K12），报同地名开头的几条比按数字相似度挑出来的准
    head = re.match(r"[\u4e00-\u9fa5]{2,6}", w)
    if head:
        same = [r["lineName"] for r in rows if norm_line(r["lineName"]).startswith(head.group(0))]
        cl = (cities().get(city_key) or {}).get("chelaile")
        if cl:      # 车来了那边也有一批同地名的线（安宁K1…），别只报掌上目录里那一条
            same += [n for n, _ in (chelaile.lines(cl) or []) if norm_line(n).startswith(head.group(0))]
        same = list(dict.fromkeys(same))
        if same:
            return "、".join(same[:n])
    hit = sorted({r["lineName"] for r in rows},
                 key=lambda x: -difflib.SequenceMatcher(None, norm_line(x), w).ratio())[:n]
    return "、".join(hit) if hit and difflib.SequenceMatcher(None, norm_line(hit[0]), w).ratio() >= 0.5 else ""


HOP_RE = re.compile(r"从?\s*([^，,。？?！!\s]{2,12}?)\s*"
                    # 「看到/说到/想到/遇到/查不到」都是动词的「到」，不是「A 站到 B 站」——闲聊被当成换乘问过
                    r"(?<![看说想做达遇收得感觉听找等拿买卖学记念料猜想跑走来回送带拉请叫问答碰见闻触意识考虑注意查不])到\s*"
                    r"([^，,。？?！!\s]{2,12})")
# 「A 咋去 B」「A 怎么去 B」：没「到」字，靠疑问词认；只有「去」一个动词的说法太松，不认
HOP2_RE = re.compile(r"(?:从\s*)?([^，,。？?！!\s]{2,12}?)\s*"
                     r"(?:咋|怎么|怎样|如何|咋样)\s*去\s*([^，,。？?！!\s]{2,12})")
# 「火车站坐什么车」要把尾巴砍掉再当站名查；「我在十字街」要把脑袋砍掉
TAIL = re.compile(r"(现在|目前|当前|[坐做]哪[个条路]?|[坐做]啥|怎么坐|坐什么|怎么去|有(?:几|多少)条?(?:公交车|公交|巴士|公车)?(?:线路|线)|哪几条线路|有什么|有哪些|有哪几|多少|几路车|坐几路|怎么走|咋坐|咋去|换乘|有直达|直达|路线|的?(?:公交)?线路|的话|附近|周边|周围|还有|有没有|在?哪[里儿]|什么时候|几点到|多久到|还有多久|要多久|多久|最近|吗|呢|啊|等).*$")
# 站牌上不写「公交站」，用户嘴里常带：「丰州公交站」其实是「丰州」
STOPSUF = re.compile(r"(公交车站|公交站|车站|站台)$")
# 「G02-513 现在在哪」「513 号车」「现在 512 车到哪」：问的是一辆具体的车，不是整条线
# 「11 路车」中间隔着「路」，不会被当成车号
VEH_RE = re.compile(r"-\s*(\d{2,5})(?!\d)|(?<![\dA-Za-z])(\d{2,5})\s*[号]?车")
# 完整车牌：省简称 + 字母 + 5 位（普通）或 6 位（新能源，闽A17997D）
PLATE_RE = re.compile("(" + PLATE_PAT + ")")
# 「桂K06833D 的自编号是啥」：车牌谁都有，问的是内部车号——两家都没有对照关系
SELFNO_RE = re.compile(r"自编号|自编|车号|车辆编号|内部编号")
# @ 标记里的 QQ 号（[At:E2795…] / <@E8CBB…>）长得像「E8 路」，解析前先抹掉
# 群里 @机器人 的三种写法：OneBot 的 CQ 码、QQ 官方的 [At:xxx]、以及没转换掉的「@大肥鱼 」
AT_RE = re.compile(r"\[At:[^\]]*\]|<@[^>\s]*>|(?<!\S)@\S+\s*")
# 引用别人的消息会整段塞进 message_str（[引用消息(: 内容)]），里面的线路号会抢答正文的问题
QUOTE_RE = re.compile(r"\[引用消息\((.*?)\)\]", re.S)
# 微信的引用是整段 XML 塞进正文：<title> 是这一句，<refermsg><content> 是被引用的那句。
# 不剥掉的话 wxid_m9ngvm9jcku022 / <signature>…V1_8V… 里的数字字母会被当成线号，
# 一条追问能回出「没查到「022」这条线」「没查到「V1」这条线」四五遍
WX_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
WX_REF = re.compile(r"<refermsg>.*?<content>(.*?)</content>.*?</refermsg>", re.S)
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


def _same_rows(rows, hit, base=None):
    """同一个站被平台拆成的几条记录：「汽车总站」「（二医院）」「（大北路）」。
    站名不同但都在 100 多米内，是同一片站台，不是隔壁站。
    """
    base = base or hit["stationName"]
    # 「市一职校（玉林技师学院）」和「玉林技师学院（市一职中）」是同一所学校的两个门，
    # 各挂各的线：括号里的别名也算这个名字
    bases = [base] + re.findall(r"[（(]([^）)]+)[）)]", base)
    out, seen = [], {base}
    for r in [hit] + list(rows):
        n = r.get("stationName") or ""
        # 只有「汽车总站（二医院）」这种加括号的才是同一个站的另一条记录；
        # 「芳村花园」「芳村西塱」是隔壁站，「芳村（地铁站）」是另一种车
        # 路口的四个站牌（「新街口东/西/南/北」）也是一片站台，问「新街口有哪些公交」得一起报
        if n in seen or not any(n == b or n.startswith((b + "（", b + "(", b + "·"))
                                 or re.fullmatch(re.escape(b) + r"[东南西北中](（\d站台）|\(\d站台\))?", n)
                                 for b in bases) \
                or "地铁" in n or "轨道交通" in n or not _near_same(hit, r):
            continue
        seen.add(n)
        out.append((n, list(r.get("relatedRoutes") or [])))
    return [(base, list(hit.get("relatedRoutes") or []))] + out


def _full_stop(city_name, st):
    """平台把同一所学校拆成几条站记录，各挂各的线：「市一职校（玉林技师学院）」只挂城际，
    7路/G01 挂在「玉林技师学院（市一职中）」名下。括号里的别名再搜一遍，照 _merge_same 并起来。"""
    rows = [r for r in stops(city_name, st["stationName"]) or [] if r.get("stationName")]
    for alias in re.findall(r"[（(]([^）)]{2,})[）)]", st["stationName"]):
        rows += [r for r in stops(city_name, alias) or [] if r.get("stationName")]
    hit = next((r for r in rows if r["stationName"] == st["stationName"]), None)
    return _merge_same(rows, hit) if hit and len(rows) > 1 else st


def _alias(s):
    """站名括号里的别名也算这个名字：「市一职校（玉林技师学院）」和「玉林技师学院（市一职中）」。"""
    return ({s, re.sub(r"[（(][^）)]*[）)]", "", s or "")}
            | set(re.findall(r"[（(]([^）)]{2,})[）)]", s or "")))


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


def _bus_row(rows):
    """站表把同名地铁站排在前面（「新街口（地铁站）」），问公交时别拿它当那片站台的主名。"""
    return next((r for r in rows if "地铁" not in (r.get("stationName") or "")), rows[0])


# 站名从长到短试时，砍掉的尾巴只能是这类通用字（「饮马口站」→「饮马口」）；
# 「北流汽车新城」砍掉「新城」匹上「北流汽车总站」，那是另一个站
STOP_TRIM_OK = re.compile(r"[公交汽车火车客运总站台路口地铁]+")
# 口语站名和平台站名对不上的常用别名（「玉林公交公司」平台上叫「公共汽车公司」）
STOP_ALIAS = {"公交公司": "公共汽车公司"}


def find_stop(city_name, raw):
    """站名从长到短试，命中即止——用户嘴里不会带括号后缀，得让平台的站名去对。

    底线是 2 字（「丰州」这种站牌本名就这么短），但整段跟城市名同字的首字
    会被下面那句挡掉，所以「南宁机场」不会退成「南宁」搜出「南宁孔庙」。
    """
    # 平台有的站带「市」有的不带：「市体育中心」站牌上叫「体育中心」
    if raw[:1] in "市县区" and len(raw) > 3:
        rows = [r for r in stops(city_name, raw[1:]) or [] if r.get("stationName")]
        if rows:
            return _merge_same(rows, _bus_row(rows)), raw[1:]
    for n in (8, 6, 5, 4, 3, 2):
        if len(raw) >= n:
            # 「石家庄131区间有没有车」退到 3 字就是城市名本身，会把「石家庄站」搜出来
            # 城市名后面只剩数字/字母的也不是站名：「北京345快」退到「北京3」会匹到「北京三十九中学」
            if raw[n:] and not STOP_TRIM_OK.fullmatch(raw[n:]):
                continue
            tail = raw[:n].replace(norm_city(city_name), "", 1)
            if norm_city(raw[:n]) == norm_city(city_name) or (
                    tail != raw[:n] and not re.search(r"[\u4e00-\u9fa5]", tail)):
                continue
            rows = [r for r in stops(city_name, raw[:n]) or [] if r.get("stationName")]
            # 2 字兜底只认同名的：「石家」会搜出石家庄东站/石家庄站（地铁站），跟问的站没关系。
            # 但平台会给站牌补一个后缀（「谈固」→「谈固站」），补一个后缀以内的也认
            if n == 2:
                rows = [r for r in rows if r["stationName"] in (raw[:n], raw[:n] + "站", raw[:n] + "口")]
            if rows:
                return _merge_same(rows, _bus_row(rows)), raw[:n]
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
            # 「湛江东简客运站」靠「客运站」搜出来的「吴川东升客运站」跟问的没关系：
            # 去掉通用后缀，剩下的字得真在候选名里按顺序出现
            core = re.sub(r"(?:客运站|汽车站|火车站|公交站|客运中心|站)$", "", raw)
            core = re.sub(rf"^{re.escape(norm_city(city_name))}", "", core)
            if best and len(core) >= 2 and not _subseq(core, best["stationName"]):
                best = None
            if best and difflib.SequenceMatcher(None, raw, best["stationName"]).ratio() >= 0.6:
                return _merge_same(rows, best), best["stationName"]
    # 「玉林八中」平台写作「玉州区第八中学」：学校简称（八中/一小）按口语搜一次，
    # 全城只有这一个才认，免得「南宁机场」这种整词被抹掉城市名后匹到不相干的站
    core = raw.replace(norm_city(city_name), "", 1)
    if len(core) >= 2 and core[-1] in "中小":
        rows = [r for r in stops(city_name, core) or [] if r.get("stationName")]
        names = {r["stationName"] for r in rows}
        if len(names) == 1 and core in next(iter(names)):
            return _merge_same(rows, rows[0]), core
    return None, raw


def hop(msg):
    """→ (出发站, 到达站)。只认「从X到Y」和「X咋去Y」，别的句式不猜。"""
    m = HOP_RE.search(msg)
    if m and LINE_RE.search(m.group(1)):     # 「坐1路到火车站」左边是线路号，不是站
        m = None
    if not m:
        # 「玉林火车站咋去北流万达」：没「到」字，靠疑问词+「去」认；光一个「去」太松，不认
        m = HOP2_RE.search(msg)
        if not m or LINE_RE.search(m.group(1)):
            return None
    a = LEAD.sub("", m.group(1).lstrip("从"))   # 「我在十字街咋去火车站」
    # 「从市体育中心坐到玉林北站」：HOP_RE 按「到」切，动词「坐」粘在出发站后面
    a = re.sub(r"[坐走骑乘搭]$", "", a)
    # 「石家庄从谈固到…」：城名和「从」一起粘在出发站前面，剥掉；只认这个形状，
    # 「广州公园前」里的城名是站名的一部分（要不要跨城，交给后面认城市）
    for ck in cities():
        if a.startswith(ck + "从"):
            a = a[len(ck) + 1:]
            break
    return a, TAIL.sub("", m.group(2)).rstrip("的了")


def _cands(city_name, msg):
    """从整句里抠出可能的站名：按标点切片，逐片砍掉「我在/坐什么」。"""
    city = norm_city(city_name)
    for part in re.split(r"[，,。.？?！!、\s]+", msg):
        cand = SRCPRE.sub("", part)      # 「车来了查X」里的数据源和动词
        if LINE_RE.search(cand):
            # 「G02到高新产业园」「11路还有多久到双溪口」：线路号后面那个才是站，先切再砍
            # 「g02离玉林北站最近的公车要多久」也得认，离 也当前后景分隔
            # shortcut: 站名本身带「离」（山西离石）会被切坏，遇到了再收窄
            cut = re.split(r"到|去|往|在|经停|离", cand)[-1]
            if not re.sub(r"[A-Za-z]?\d{1,4}\s*[路线]", "", cut).strip():
                # 「在市政广场要等多久等到17路」：站名在线号前面，中间还夹着问法，
                # 光按「到」切会把站名切没——从号前面那截取，先把问法剥掉
                cut = ASKPAST.sub("", re.sub(r"[A-Za-z]?\d{1,4}\s*[路线].*$", "", cand))
            cand = cut
        # 站名在线路号前面（「我在玉林火车站等9路」「玉林火车站9路还有几站」）时号后面没站名，
        # 得把号本身刮掉再当站名试
        # shortcut: 不认站名里带阿拉伯数字的（「3号门」），遇到了再收窄
        cand = re.sub(r"[A-Za-z]?\d{1,4}\s*[路线]", "", cand)
        cand = LEAD.sub("", TAIL.sub("", cand))
        pre = cand                       # 剥「公交/地铁」前缀之前的原名，「公交公司」得靠它换写法
        whole = re.sub(r"^(地铁|轨道交通|公交)", "", cand)
        # 「广州地铁芳村站附近」：城市名和「地铁」都不是站名的一部分
        cand = re.sub(r"^(地铁|轨道交通|公交)", "", re.sub(rf"^{re.escape(city)}", "", cand))
        # 先试剥掉城市名的「芳村站」，不行再试原样的「玉林北站」（北站是站名的一部分）
        # 再退回剥掉「公交站」的写法：「丰州公交站」平台上是「丰州」
        bare = [STOPSUF.sub("", c) for c in (cand, whole)]
        # 「玉林公交公司」先换成平台上的写法再试，否则会被砍成「公司」搜不出东西
        alias = [c.replace(k, v) for c in (pre, whole, cand)
                 for k, v in STOP_ALIAS.items() if k in c]
        for c in dict.fromkeys(alias + [cand, whole] + bare):
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


def _subseq(short, long):
    """short 的每个字都能在 long 里按顺序找到（「大容山专」⊂「大容山旅游专」）。"""
    it = iter(long)
    return all(c in it for c in short)


def _msg_line(city_name, msg, city_key=""):
    """句子里点名的真线名不是「数字+路」（「玉林北站定制夜班车」）：按目录认。"""
    def _look(names):
        for n in names:
            if len(n) >= 4 and n in msg:
                return n
        # 简称：「大容山专线」少说了「旅游」。字基本都在、比例够、且全城只有一条对得上才认，
        # 认不出来宁可去查站名，也不能硬塞一条别的线
        # 「现在有玉林北站夜班车吗」：TAIL 收了「现在」，长在最前面会把整句吃空，先砍掉
        m = re.sub(r"^(?:现在|目前|当前)有?", "", msg.strip())
        t = norm_line(TAIL.sub("", m).replace(norm_city(city_name), ""))
        if len(t) < 4:
            return None
        # 分母也剥掉城名：「玉林北站夜班车」丢的是「定制」，不该因为城名长短被算低
        hit = {n for n in names
               if len(t) / max(len(norm_line(n).replace(norm_city(city_name), "")), 1) >= 0.6
               and _subseq(t, norm_line(n))}
        return hit.pop() if len(hit) == 1 else None

    hit = _look([r.get("lineName") or "" for r in _catalog(city_name).get("buslines") or []])
    if hit:
        return hit
    # 掌上目录里没有的线（湛江妇幼专线只在车来了那边）也得认：认不出就会沿用上一条问过的线，
    # 变成「用车来了查湛江妇幼专线 → 没查到玉林北站定制夜班车这条线」
    cid = (cities().get(city_key or norm_city(city_name)) or {}).get("chelaile")
    if cid:
        try:
            return _look([n for n, _ in chelaile.lines(cid) or []])
        except Exception:            # 车来了目录挂了就当没有，别把这条问题整句废掉
            return None
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


def _exact_line(city_name, msg, want, city_key=""):
    """目录里有更长的真线名整段出现在句子里（「新乐专1路」）就用它，别只拿裸号去匹。"""
    flat, w = norm_line(msg), norm_line(want)
    w2 = _digitize(w)          # 「地铁一号」在目录里叫「地铁1号线」

    def scan(names):
        hit = []
        for nm in names:
            n = norm_line(nm)
            # 句子里把城市名也粘进了线名（「西安环山二号线」）：目录名是它的后缀也算。
            # 只认 head 就是这座城的名字——「安宁12」的「安宁」是线名本身（安宁K12），
            # 当成城市名剥掉会把昆明市区的 12 路答成安宁的车
            tail = w2 if n.endswith(w2) else w
            head = tail[:len(tail) - len(n)] if tail.endswith(n) and len(tail) > len(n) else ""
            if not (n.endswith(tail) or (head and norm_city(head) == norm_city(city_name))):
                continue
            # 「新乐专一路」的「一」也是数字，得和 1 等价地认
            cns = "零一二三四五六七八九"
            pat = "".join(f"[{d}{cns[int(d)]}]" if d.isdigit() else re.escape(d) for d in n)
            if n in flat or re.search(pat, flat):
                hit.append(nm)
        return hit

    hit = scan([r["lineName"] for r in _catalog(city_name).get("buslines") or [] if r.get("lineName")])
    best = max(hit, key=lambda n: len(norm_line(n)), default=None)
    # 掌上没这条、或只匹到裸号（「吴川二路」）：真线名可能只在车来了目录里（吴川2路）
    cid = (cities().get(city_key or norm_city(city_name)) or {}).get("chelaile")
    if cid and (not best or norm_line(best) == w):
        try:
            # 只收比裸号更长的：「吴川9路」不能拿湛江市区的 9 路顶（上游没这条就说没这条）
            hit += [n for n in scan([n for n, _ in chelaile.lines(cid) or []])
                    if len(norm_line(n)) > len(w)]
        except Exception:
            pass
        best = max(hit, key=lambda n: len(norm_line(n)), default=None)
    return best


def _station_seq(city_name, line, direction):
    return _cached("stations", (city_name, line, direction), CATALOG_TTL,
                   lambda: _post({"CMD": "103", "CITYNAME": city_name, "LINENAME": line,
                                  "DIRECTION": direction}).get("data") or [])


def _goes(city_name, line, a, b):
    """A 站是否在 B 站之前。返回 (方向, A 站序, B 站序)，都是 0 基；不顺路就 None。"""
    for d in ("1", "2"):
        names = [r.get("stationName") or "" for r in _station_seq(city_name, line, d)]
        # 先认同名，「汽车总站」不能被「北流汽车总站」顶掉
        def idx(s):
            i = next((i for i, n in enumerate(names) if n == s), None)
            if i is None:
                i = next((i for i, n in enumerate(names) if s in n), None)
            if i is None:      # 括号里的别名对得上也是同一个门（一职校 / 技师学院两条记录）
                i = next((i for i, n in enumerate(names) if _alias(s) & _alias(n)), None)
            return i
        ia, ib = idx(a), idx(b)
        if ia is not None and ib is not None and ia < ib:
            return d, ia, ib
    return None


def _hop_line(city_name, line, d, ia, ib):
    """这一路下一辆车几分钟到 A（上游预测），从 A 坐到 B 要几分钟（按每段均速累）。"""
    payload = _live_my(city_name, line, d, ia + 1)      # STATIONORDER 1 基
    eta = _etas(payload)
    if not eta:
        first, last = _first_last(city_name, line, d)
        w = _window(first, last)
        if w == "before":
            return f"，首班 {first} 还没到" + (f"（末班 {last}）" if last else "")
        if w == "after":
            return f"，末班 {last} 已经开走了" + (f"，首班 {first}" if first else "")
        # 起点站没到站预测时上游会写 tips，「等待发车」比「没车」准
        if "等待发车" in [r.get("busToStationTips") for r in payload.get("routeOnStationRTimeInfoList") or []]:
            return "，起点站的车在等发车"
        return "，这会儿没车报位置"
    return f"，下一辆约 {max(1, round(eta[0]))} 分钟后到，坐过去 {max(1, round(_run(payload, ia, ib)))} 分钟"


def _ask111(city_name, a, b):
    d = _cached("plan", f"{city_name}|{a}|{b}", 60, lambda: _post({
        "CMD": "111", "CITYNAME": city_name, "STARTPOINTNAME": a, "STARTPOINTLNG": "",
        "STARTPOINTLAT": "", "ENDPOINTNAME": b, "ENDPOINTLNG": "", "ENDPOINTLAT": ""}))
    info = d.get("info") if isinstance(d, dict) else None
    return info if isinstance(info, list) else []


def render_plan(city_name, a, b):
    """掌上自己的换乘方案（CMD=111）：两次换乘也排、总时长和步行距离都算好还排了序。
    我们手算的只换一次、按「换得最早」挑，经常不是最快的那个。
    shortcut: 只取前 2 个方案，群消息再长没人看；坐标留空，按站名查就够
    """
    info = _ask111(city_name, a, b)
    # 起点站牌名和用户写法对不上时上游会自己换成附近站（「市体育中心」→「文体人民路口」），
    # 那种一条线直达的方案不能交回 render_hop——那条线根本不从用户说的站发车
    ra = (find_stop(city_name, a)[0] or {}).get("stationName")
    if not info:
        # 群里说的是「玉林汽车总站」，平台站牌就写「汽车总站」——名字对不上上游排不出方案
        rb = (find_stop(city_name, b)[0] or {}).get("stationName")
        if ra or rb:
            info = _ask111(city_name, ra or a, rb or b)
    rows = []
    for k, p in enumerate(info[:2]):
        legs = [lg for key in ("routeUpDownSimple1", "routeUpDownSimple2", "routeUpDownSimple3")
                for lg in (p.get(key) or []) if lg.get("routeName")]
        if k == 0 and len(legs) < 2 and (legs[0].get("upStationName") or "") in (a, ra):
            return ""       # 能直达就让 render_hop 答——它带车在哪、首末班，这里排不出这些
        ride = ""
        for leg in legs:
            nm = leg["routeName"]
            if nm in ride:
                continue
            up = leg.get("upStationName") or ""
            # 第一条腿不从用户说的站发车（上游换成了附近站），得说清去哪上车
            ride += (f"，到{up}换 {nm}" if ride else
                     f"到{up}坐 {nm}" if up and up not in (a, ra) else f"坐 {nm}")
        if not ride:
            continue
        bits = [x for x in (p.get("stationNum"),
                            p.get("totalTime") and f"约{p['totalTime']}",
                            p.get("totalWalkDistance") and f"走路{p['totalWalkDistance']}") if x]
        rows.append(ride + (f"，{'、'.join(bits)}" if bits else ""))
    if not rows:
        return ""
    if len(rows) == 1:
        return f"从{a}到{b}：{rows[0]}"
    return f"从{a}到{b}：\n{rows[0]}\n或者{rows[1]}"


def render_ways(line, table):
    """一条线的主要换乘站（玉林导乘资料）——上游 104/115 只给站名和经停，没有这层。"""
    rows = [f"{line} 主要换乘站："]
    rows += [f"{st}：{s}" for st, s in table.items()]
    n = yulin.note(line)
    return "\n".join(rows) + (f"\n{n}" if n else "")


def render_hop(city_key, city_name, raw, only_direct=False):
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
    a, b = _full_stop(city_name, a), _full_stop(city_name, b)
    ra = set(a.get("relatedRoutes") or [])
    rb = set(b.get("relatedRoutes") or [])
    head = f"从{a['stationName']}到{b['stationName']}"
    if not ra & rb:
        if only_direct:
            return ""
        plan = _hop_plan(city_name, a, a_name, b, b_name, ra, rb)
        if plan:
            return plan
        g = yulin.plan(ra, rb)          # 站序算不出来时，用玉林导乘资料的换乘站表兜一次
        if g:
            return f"{head}没直达，换一次：坐 {g[0]} 到{g[1]}换 {g[2]}"
        short = lambda s: "、".join(sorted(s)[:6]) + ("…" if len(s) > 6 else "")
        return f"{head}没直达车，{a['stationName']} 有 {short(ra)}，{b['stationName']} 有 {short(rb)}"
    direct = []
    for line in sorted(ra & rb)[:4]:            # shortcut: 只验前 4 条，交叉线多的城可能漏
        got = _goes(city_name, line, a_name, b_name)
        if got:
            direct.append((line, got))
    if not direct:
        if only_direct:
            return ""
        plan = _hop_plan(city_name, a, a_name, b, b_name, ra, rb)
        if plan:
            return plan
        g = yulin.plan(ra, rb)
        return (f"{head}没直达，换一次：坐 {g[0]} 到{g[1]}换 {g[2]}" if g
                else f"{head}有同名线路但方向反了，得换乘")
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


def _same_station(x, y):
    """「元氏客运站」和线路站序里的「元氏车站」是同一个站：把通用字去掉再比。"""
    strip = lambda s: re.sub(r"(客运站|汽车站|公交站|车站|站台|站)$", "", s or "")
    return strip(x) == strip(y)


def _into_city(city_name, a_name, other):
    """跨城：出发城的站表里有站名带目的城名的站（玉林表里的「北流客运中心」），
    起点同线能坐到最深那一站就报它——别一句「跨城串不起来」把 28 路打发掉。"""
    a = find_stop(city_name, a_name)[0]
    if not a:
        return ""
    best = None
    for r in stops(city_name, other) or []:
        n = r.get("stationName") or ""
        if not n.startswith(other):
            continue
        for line in sorted(set(a.get("relatedRoutes") or []) & set(r.get("relatedRoutes") or [])):
            got = _goes(city_name, line, a["stationName"], n)
            if got and (not best or got[2] > best[2]):
                best = (line, n, got[2], got[1], got[0])
    if not best:
        return ""
    line, n, j, i, d = best
    seq = _seq_pts(city_name, line, d)
    nxt = seq[j + 1]["name"] if j + 1 < len(seq) else ""
    tail = f"，或再往前一站 {nxt}" if nxt else ""
    return (f"跨城的，但 {line} 从{a['stationName']}一直开到{n}（{j - i} 站，已经进{other}了）{tail}，"
            f"坐到那儿再换{other}的市内线路，走两步也行")


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
            ib = next((p["i"] for p in pts if _same_station(b_name, p["name"])), None)
            if ib is not None:
                legs += [(line, d, p) for p in pts[:ib]]
    best = _plan_from(city_name, a_name, ra, legs)
    if not best:
        # 起点站线路多时（谈固站 23 条）按字母序只试前 3 条会漏掉真正接得上的那条：
        # 直接问换乘站都有哪几路车，跟起点的线路求交，只验这一条
        # shortcut: 只看前 10 个换乘候选站（长线单方向就 30 站，看全了每次都要多等好几秒）
        for l2, d2, p in legs[:10]:
            rows = [r for r in stops(city_name, p["name"]) or []
                    if r.get("stationName") == p["name"]]
            mine = set().union(*[set(r.get("relatedRoutes") or []) for r in rows]) if rows else set()
            if mine & ra:
                best = _plan_from(city_name, a_name, mine & ra, [(l2, d2, p)])
                if best:
                    break
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
    got, dead = {}, []                # 方向名 -> (分钟, 车牌, 站数)；没预测的进 dead
    near = False                      # 这条线到底过不过我这个站
    for line_name, row in hit:
        d = row["upperOrDown"]
        names = [s.get("stationName") or "" for s in _station_seq(city_name, line_name, d)]
        # 两家站名写法不一样（「玉林火车站」↔「火车站」），互相包含也算同一个站
        idx = next((i + 1 for i, n in enumerate(names)
                    if station_name in n or (len(n) >= 2 and n in station_name)), None)
        if not idx:
            continue
        near = True
        # 「G02 到高新产业园，去玉林北站」问的是在产业园上车、往玉林北站那个方向，别把终点当上车站
        if dest and dest != station_name:
            g = _goes(city_name, line_name, station_name, dest)   # 只留能开到 dest 的方向
            if not g or g[0] != d:
                continue
        lab = row.get("to") or ""
        pay = _live_my(city_name, line_name, d, idx)
        eta = _etas(pay)
        if not eta:
            # 上游对没车的方向一律给 busToStationTips（「非运营时间」也算），
            # 光抄它不行：运营窗口里它照样这么写。得对首末班才知道是真收班还是这一站没预测
            tip = next((r.get("busToStationTips") for r in pay.get("routeOnStationRTimeInfoList") or []
                        if r.get("busToStationTips")), "")
            f, l = _first_last(city_name, line_name, d) if tip else ("", "")
            dead.append((lab, f, l))
            got.setdefault(lab, None)     # 方向还在，只是这会儿没车报过来
            continue
        mins, left, plate = eta
        got[lab] = (max(1, round(mins)), plate, "马上到" if left <= 0 else f"还有 {left} 站")
    if not got:
        if not hit:
            return None                  # 目录里根本没这条线，交给上层说「没查到」
        if not near:
            return f"{hit[0][1]['lineName']} 不经过{station_name}"
        return None
    line_name = hit[0][1]["lineName"]
    live = {k: v for k, v in got.items() if v}
    if not live:
        # 这条线确实过我这一站、但两个方向这会儿都没有车往这儿开（刚过站 / 还没发车）。
        # 不能返 None 落到「每辆车离它旁边那个站 181 米」的清单上——站在站台的人读写没意义
        dirs = "、".join(dict.fromkeys(k for k, _, _ in dead))
        wins = [(k, x, y) for k, x, y in dead if x or y]
        st = {s for k, x, y in wins if (s := _window(x, y))}
        # 「非运营时间」是上游对「这一站没有到站预测」的说法，运营窗口里也这么说，不能照抄：
        # 定制夜班车 20:00 往北站方向明明在跑（18:50–21:30），抄下来就成了「别等了」
        where = ({"after": "末班已经开走", "before": "首班还没到"}.get(next(iter(st)), "")
                 if len(st) == 1 else "各方向运营时间不一样" if st else "")
        if not where:
            return f"{line_name} 这会儿没有开往{station_name}的车（往{dirs}方向都没有）"
        span = (f"{wins[0][1] or '?'}–{wins[0][2] or '?'}" if len({(x, y) for _, x, y in wins}) == 1
                else "；".join(f"往{k} {x or '?'}–{y or '?'}" for k, x, y in wins))
        return f"{line_name} 这会儿没车报过来，{where}：{span}"

    # 「N 路离 X 站大约几分钟」：站在站台的人要的是这一句，别念「往X：桂K… 约 18:10 到」
    def one(lab, v):
        return (f"往{lab} " if lab else "") + f"大约 {v[0]} 分钟（{v[1]}，{v[2]}）"

    if len(got) == 1:                     # 只有一个方向过这站（或点了方向），不用念方向名
        return f"{line_name} 离{station_name} " + one("", live[list(live)[0]])
    # 中途站两个方向都要报：只报一边会被当成哪边都能坐；哪个方向没车也如实说
    def dead_txt(lab, x, y):
        s = _window(x, y)
        if s == "before":
            return f"往{lab} 首班 {x} 还没到"
        if s == "after":
            return f"往{lab} 末班 {y} 已经开走"
        return f"往{lab} 这会儿没车报过来"

    parts = [one(k, v) for k, v in live.items()] + [dead_txt(*d) for d in dead]
    return f"{line_name} 离{station_name}：" + "；".join(parts)


# 「9路站点」这种只说两个字的也要认；「到站点了没」「哪个站点」问的是实时位置，别抢
STOPS_RE = re.compile(r"所有?站点|全部站点|站(点)?列(表|出来)|经过哪些站|有哪些站|停哪些站|站序|沿途|(?<![到哪个几])站点")


def render_stops(city_name, want):
    """「B19 路所有站点」要的是站表，不是实时车。两个方向站一样，只念主方向。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick([(r["lineName"], r) for r in rows], want)
    out = []
    for line_name, row in hit:
        names = [s.get("stationName") or "" for s in _station_seq(city_name, line_name, row["upperOrDown"])]
        if names:
            body = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
            # 线名自带括号（「大容山旅游专线（28路）」）时就别套括号，否则连成「）（」
            br = ("", "") if line_name.endswith(("）", ")")) else ("（", "）")
            out.append(f"{line_name}{br[0]}往{row.get('to') or '?'}，{len(names)} 站{br[1]}：\n{body}")
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
        p = _price(city_name, name)
        if f or l:                                   # 两个方向时刻不同就各念一行
            out.append(f"{name} 首班 {f or '?'}，末班 {l or '?'}"
                       f"（{row.get('from') or '?'} → {row.get('to') or '?'}）"
                       + (f" 票价 {p}" if p else ""))
    return "\n".join(dict.fromkeys(out)) or None


def render_dir_window(city_name, want, d):
    """「往X方向还有没有车」：只念这个方向的首末班，再说这会儿在不在窗口里。"""
    hit = _pick_city_line([(r["lineName"], r) for r in _catalog(city_name).get("buslines") or []],
                          want, city_name)
    for name, row in hit:
        ud = str(row.get("upperOrDown") or "")
        if ud not in ("1", "2") or d not in (row.get("to") or ""):
            continue
        f, l = _first_last(city_name, name, ud)
        if not (f or l):
            continue
        return (f"{name} 往{row.get('to')}方向 {f or '?'}–{l or '?'}，"
                + {"after": "这会儿末班已经开走", "before": "这会儿首班还没到"}
                .get(_window(f, l), "这会儿还在运营时段里"))
    return None


def render_ends(city_name, want):
    """起讫站。目录里每个方向一条记录，上行下行是反的，按站对去重。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    seen, out = set(), []
    for name, row in hit:
        # 只念上行：下行是反过来的同一条，站名写法还常带方向后缀（「富乐汽车站（仙海大道）」）
        if str(row.get("upperOrDown") or "") != "1":
            continue
        f, t = row.get("from") or "", row.get("to") or ""
        if not (f and t) or frozenset((f, t)) in seen:
            continue
        seen.add(frozenset((f, t)))
        out.append(f"{name} 首末站：{f} ↔ {t}")
    if len(out) > 6:                      # 绵阳 25 路那种十几条支线，别刷屏
        out = out[:6] + [f"…还有 {len(out) - 6} 条支线"]
    return "\n".join(out) or None


PRICE_RE = re.compile(r"票价|多少钱|几块钱?|收费")
# 「首末站」是公交迷的说法，问的是起讫站，不是首末班时间——「玉林6路的首末站是啥」被答成了实时车
# 「起点和终点」「起终点」也算——「起点站」「终点站」匹不上不带「站」的写法；开头的「起」
# 保证「到终点站还要多久」这种不可能误中
ENDS_RE = re.compile(r"首末站|起讫站|起点站|终点站|首站|末站|头末站|起[点]?[和与跟、]?终点")
# 「玉林公交公司电话是多少」「广州巴士集团电话」没有线路号，问的是整座城。
# 光认「公交公司」会抢闲聊（「司机和公交公司救援」），要带问联系方式的词或者「有哪些」才认
CITY_CO_RE = re.compile(r"[\u4e00-\u9fa5]{0,4}(?:公交|巴士|客运|交通|地铁)[\u4e00-\u9fa5]{0,6}"
                        r"(?:电话|热线|客服|投诉)"
                        r"|(?:有哪些|哪几家|几家公司)[\u4e00-\u9fa5]{0,4}(?:公交|巴士|客运)公司")
# 「往X方向」在 render_price 里也得认，跟 render_times 一个口径


CITYLINES_RE = re.compile(r"(?:有哪些|哪些|几条|多少条|所有|全部)[\u4e00-\u9fa5]{0,4}(?:线路|公交线|公交(?!公司))")


def _city_wide(msg, city_name):
    """这句问的是整座城吗：把城市名和问法都抹掉，还剩地名就是在问那个地方。"""
    t = msg.replace(norm_city(city_name), "", 1)
    t = re.sub(r"公交线路|公交线|线路|公交车|公交|有哪些|有没有|哪些|几条|多少条|所有|全部"
               r"|这边|那里|这儿|那边|这里|这|那|该|市|县|有|的|呢|吗|啊|？|\?|\s", "", t)
    return len(t) < 2


CITYLINES_MAX = 40


def render_city_lines(city_name):
    """全城线路清单。119 目录一个方向一条记录，按线名合并成「A ↔ B」。"""
    rows = _catalog(city_name).get("buslines") or []
    ends = {}
    for r in rows:
        n = r.get("lineName")
        if n:
            ends.setdefault(n, []).append(r.get("to") or "")
    if not ends:
        return None
    names = list(ends)
    # 650 条一次发不完，群消息会被平台截断：先列一截，剩下的让他直接报线号
    more = len(names) > CITYLINES_MAX
    out = [f"{norm_city(city_name)}这边 {len(ends)} 条线路"
           + (f"，先列前 {CITYLINES_MAX} 条：" if more else "：")]
    for n in names[:CITYLINES_MAX]:
        ts = [t for t in dict.fromkeys(ends[n]) if t]
        out.append(f"{n}：{' ↔ '.join(ts)}" if ts else n)
    if more:
        out.append(f"还有 {len(names) - CITYLINES_MAX} 条没列，想看哪条直接报线路号")
    return "\n".join(out)


def render_price(city_name, want, msg=""):
    """票价：掌上写在 103 的 commonts 里，车来了整家没有这个字段，那边只能说没标。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    only = _dir(msg)
    out = []
    for name, row in hit:
        if only and only not in (row.get("to") or ""):
            continue
        p = _price(city_name, name)
        out.append(f"{name} 票价 {p}" if p else f"{name} 平台上没标票价")
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


def render_city_company(city_name, msg=""):
    """一座城有哪些公交公司、电话多少——没带线路号时问的就是这个，别回空。
    shortcut: 大城要拉全目录（北京 3 秒 8MB），只在明确问公司时才调，挂 1 小时缓存。
    """
    if not city_name:
        return None
    d = _cached("bean-all", city_name, CATALOG_TTL,
                lambda: _post({"CMD": "114", "CITYNAME": city_name, "KEYWORD": ""}))
    co = {}
    for b in d.get("buslines") or []:
        r = b.get("routeBean") or {}
        c = (r.get("company") or "").strip()
        if not c:
            continue
        co.setdefault(c, "")
        m = (r.get("companyMobile") or "").strip()
        if m and not co[c]:
            co[c] = m.rsplit(":", 1)[-1].strip()      # 「骏马客运（接听时间：06:00-21:00）:010-…」
    if not co:
        return None
    # 「广州巴士集团电话」点了名就只报这家；泛指（「玉林公交公司」）照旧全列
    kw = CITY_CO_RE.search(msg or "")
    if kw:
        core = re.sub(r"(?:公司|集团|电话|热线|客服|投诉)$", "", kw.group())
        # 先整段试（「广州地铁」本身就是公司名），再去掉城名试（「广州一汽巴士」→「一汽巴士」）
        for k in (core, core.replace(norm_city(city_name), "")):
            same = {c: t for c, t in co.items() if len(k) >= 3 and k in c}
            if same:
                co = same
                break
    rows = [f"{city_name}这边 {len(co)} 家公交公司：{'、'.join(co)}"]
    tel = [f"{c} {t}" for c, t in co.items() if t]
    rows.append("电话：" + "；".join(tel) if tel else
                "平台没挂这几家的咨询电话，打 12345 问公交服务热线")
    return "\n".join(rows)


def render_transfer(city_name, want, msg=""):
    """逐站能换哪几路：上下行站几乎一样，按站名去重只算一份。

    问「主要换乘站」时用玉林这份导乘表——逐站扫出来是几十条，人问的是「在哪换最顺」。
    """
    if re.search(r"主要|最方便|在哪", msg):
        w = yulin.ways(want)
        if w:
            return render_ways(*w)
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


# 车来了整目录没翻完时文案里带这句，render_stop 靠它判断要不要补掌上的完整站表：
# 两处共用一个常量，别再各写一份字面量（改一处漏一处已经踩过一次）
SWEEP_CUT = "只能一条条翻"


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
    # 城市名已经在外面：「湛江麻章车站」和「麻章车站」是同一个站；「麻章」这种更短的候选
    # 会把「麻章汽车站」「麻章一中」全带出来，只留最长的那个
    ws = [re.sub(rf"^{re.escape(norm_city(city_name))}", "", norm_city(x))
          for x in _cands(city_name, msg)]
    wants = [w for w in dict.fromkeys(ws) if w and not any(w != o and w in o for o in ws)]
    # 大目录翻不完（深圳 2581 条方向 20 秒只翻得到四分之一）：掌上说得出这个站挂哪几路，
    # 就拿这几个线名直接去车来了核对，比整目录翻快两个数量级。
    # shortcut: 掌上拆站台时只拿到一部分线，大目录会少报几条；小目录仍整扫不受影响，
    # 想 100% 覆盖得让车来了开放站点搜索
    nm = pick_stop(city_name, msg) if len(rows) > 1000 else None
    if nm:
        hit = next((x for x in (stops(city_name, nm) or []) if x.get("stationName") == nm), {})
        want = {norm_line(r) for r in hit.get("relatedRoutes") or []}
        rows = [r for r in rows if norm_line(r[0]) in want] or rows

    def one(row):
        n, lid = row
        try:
            sn = _cached("route-cl", (cid, lid), CATALOG_TTL,
                         lambda lid=lid: chelaile.stations(cid, lid))
        except Exception:
            return None
        # 两家站名不完全一样（「广西医科大玉林校区」↔「医科大学玉林校区」），得模糊匹；
        # 但 4 字以内的短站名只认包含关系：「麻章车站」和「麻章汽车站」差一个字也算 0.89，
        # 那是隔壁站牌，太平-麻章A线就这么混进了麻章车站的线路表
        hit = next((s for s in sn
                    if any(w in norm_city(s)
                           or (len(w) >= 5 and difflib.SequenceMatcher(None, w, norm_city(s)).ratio() >= 0.8)
                           for w in wants)), None)
        return (n, hit, sn[0], sn[-1]) if hit and sn else None

    # 按目录顺序整目录扫：写死扫前 60 条会漏（湛江 8 路在第 88 位）；
    # 大目录按秒数收口，扫了多少就报多少
    got, done, t0 = [], 0, time.time()
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        for i in range(0, len(rows), 40):
            chunk = rows[i:i + 40]
            got += [g for g in pool.map(one, chunk) if g]
            done += len(chunk)
            if time.time() - t0 > STOP_SWEEP_SECONDS:
                break
    finally:
        pool.shutdown(wait=False)
    if not got:
        return ""
    items, seen = [], set()
    for n, _, a, b in got:
        if norm_line(n) not in seen:      # 两个方向算一条线
            seen.add(norm_line(n))
            items.append(f"{n} {a} ↔ {b}" if a and b else n)
    # 车来了没有站点搜索，只能一条条翻站表；翻到哪算哪，别让人以为这就是全部
    note = (f"（车来了{SWEEP_CUT}，这次翻了前 {done} 条，全城 {len(rows)} 条方向）"
            if done < len(rows) else "")
    return f"{got[0][1]} 车来了这边有 {len(items)} 条线路{note}：" + "".join(f"\n{i}" for i in items)


def render_stop_best(city_name, name, routes):
    """「X 站哪辆车来得最快」：每条经停线每个方向问该站的到站预测（STATIONORDER），比分钟数。
    没有预测的线路单独点名，不能当成「没车」——末班车、刚发车都会空。"""
    to = {}
    for r in _catalog(city_name).get("buslines") or []:
        to[(r.get("lineName"), str(r.get("upperOrDown")))] = r.get("to") or ""
    hits, blank = [], []
    for ln in routes:
        for d in ("1", "2"):
            seq = _station_seq(city_name, ln, d)
            i = next((k + 1 for k, s in enumerate(seq)
                      if (s.get("stationName") or "") == name), None)
            if not i:
                continue
            eta = _etas(_live_my(city_name, ln, d, i))
            if eta:
                hits.append((eta[0], ln, to.get((ln, d)) or "", eta[1], eta[2]))
            elif ln not in blank:
                blank.append(ln)
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    out = (f"{name}来得最快的是 {hits[0][1]}"
           + (f" 往{hits[0][2]}方向" if hits[0][2] else "")
           + f"：{hits[0][4]} 约 {_clock(hits[0][0])} 到（{max(1, round(hits[0][0]))} 分钟，还有 {hits[0][3]} 站）")
    for m, ln, dest, cnt, plate in hits[1:]:
        out += (f"\n其次 {ln}" + (f" 往{dest}方向" if dest else "")
                + f"：{plate} {max(1, round(m))} 分钟（还有 {cnt} 站）")
    if blank:
        blank = [b for b in blank if b not in {h[1] for h in hits}]
    if blank:
        out += f"\n{'、'.join(blank)} 这会儿没有到站预测（没发车或已过末班）"
    return out


def render_stop(city_key, city_name, msg, src=None):
    """「X 站有哪几路」：站点经停线路，每条带首末站。"""
    cl = ""
    if src == "chelaile":       # 点名车来了就真给车来了的：两家站表不合并，线的集合也不一样
        out = _stop_chelaile(city_name, msg)
        if out and SWEEP_CUT not in out:
            return out
        # 大目录翻不完时后头的线路压根没到（深圳 2581 条方向 20 秒只到一千出头，
        # 关山月美术馆②的 M555 就在后面）。点名要车来了的仍先给车来了那份，
        # 掌上完整站表跟在后面补，别拿掌上的顶掉它
        cl = out
    name = pick_stop(city_name, msg)
    if not name:
        # 站名只在车来了那边有（湛江文化中心：掌上 551 个站名里没有），别直接说没查到
        return _stop_chelaile(city_name, msg) or None
    # 再搜一次站名会撞上同名地铁站（「芳村」和「芳村（地铁站）」），按站名精确挑
    # 拿用户写的那个词再搜一遍：pick_stop 只回一个站牌名（「新街口西」），拿它搜就只剩那一个
    # 站台，新街口北/东/·正洪街全丢了；stops 有缓存，这一遍不再发请求
    rows_ = []
    for cand in _cands(city_name, msg):
        got, _ = find_stop(city_name, cand)
        if got and got.get("stationName") == name:
            rows_ = [r for r in stops(city_name, cand) or [] if r.get("stationName")]
            break
    # 「饮马口站有哪些线路」拿带「站」的那个词只搜回一个站牌，「饮马口（平原路）」被丢了；
    # 换站牌本名再搜一遍，同一片站台交给 _same_rows 合（stops 有缓存，多半不发请求）
    rows_ = rows_ + [r for r in stops(city_name, name) or [] if r.get("stationName")]
    rows_ = rows_ or stops(city_name, name) or []
    # 「市一职校（玉林技师学院）」和「玉林技师学院（市一职中）」是同一所学校的两个门，
    # 各挂各的线：括号里的别名也搜一遍，交给 _same_rows 合并
    for alias in re.findall(r"[（(]([^）)]+)[）)]", name):
        rows_ = rows_ + (stops(city_name, alias) or [])
    if not rows_:
        return _stop_chelaile(city_name, msg) or None
    row = next((r for r in rows_ if r.get("stationName") == name), rows_[0])
    # 「新街口」在平台上拆成东/西/南/北四个站牌：问的是一整片，只报被挑中的那一角等于漏答
    gb = re.sub(r"[东南西北中]$", "", name)
    if (gb != name and len(gb) >= 2 and norm_city(gb) != norm_city(city_name)
            and sum(1 for r in rows_ if (r.get("stationName") or "").startswith(gb)) > 1):
        name = gb
    groups = _same_rows(rows_, row, name)
    if not any(rs for _, rs in groups):
        return None
    # 「这站哪辆车来得最快」问的是实时比较，不是有几条线：逐线查该站的到站预测再比
    if BEST_RE.search(msg):
        got = render_stop_best(city_name, name, list(dict.fromkeys(n for _, rs in groups for n in rs)))
        if got:
            return got
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
        # 只有一个站台有线路（别名的线路已被它覆盖）：不用分组说
        if len(blocks) == 1:
            out = f"{name}有 {len(seen)} 条线路：{blocks[0].split('：', 1)[1]}"
        else:
            out = (f"{name}这一片分 {len(blocks)} 个站台，共 {len(seen)} 条线路：\n"
                   + "\n".join(blocks))
    # 「阿房宫线」问的其实是地铁：公交站那片若还有「（地铁站）」的记录，单独报一行。
    # _same_rows 故意把地铁剔出公交清单，整句只报 10 条公交线，问地铁的人就白问
    seen_metro = set()
    key = re.sub(r"地铁站$", "", name)      # 「阿房宫南地铁站」搜不到旁边的「阿房宫南（地铁站）」
    for r in (stops(city_name, key) if key != name else rows_):
        n = r.get("stationName") or ""
        if n == name or n in seen_metro or "地铁" not in n or not _near_same(row, r):
            continue
        seen_metro.add(n)
        lines = r.get("relatedRoutes") or []
        if lines:
            out += f"\n{n} 是地铁站，经停 {'、'.join(lines)}"
    # 枢纽站的乘车点只有导乘资料里有（玉林北站：公交站场和站前广场两个站都能上）
    out += ("\n" + yulin.note(name)) if yulin.note(name) else ""
    # 点名车来了却没扫到，别让掌上的结果冒充车来了的
    if cl:      # 车来了翻不完：那份先给，掌上的完整站表跟在后面
        return f"{cl}\n\n掌上这边完整的站表：\n{out}"
    return out + ("\n（车来了没扫到这个站，上面是掌上的）" if src == "chelaile" else "")


# 「这站哪路车来得最快／等的最少」：站查询要按实时比，不报线路清单
BEST_RE = re.compile(r"最快|最少|哪[辆部个条]?先|先到|等得最少|等的最少")
# 「玉林还有哪条线路有车」这类全城问题没法靠线路号答，得把目录扫一遍。
# 放在站点判断之后：能认出站名就以站点为准，「十字街有几路车」不会被抢走。
SWEEP_RE = re.compile(r"哪条|哪几|哪路|还有|在跑|运营")
# 「新乡胖东来三胖有哪些公交」：问的是那个地方有哪些线，不是整城清单、也不是哪条在跑
STOPASK_RE = re.compile(r"(?:有哪些|有没有|有什么|有几条?|有多少条?|哪几条)[\u4e00-\u9fa5]{0,5}"
                         r"(?:公交车|公交|巴士|公车|线路)?[？?吗呢啊]*$")
# 「南宁今晚还有没有公交」问的是这会儿有没有车，不是某个地方——留给 SWEEP_RE
_NOTPLACE = re.compile(r"今晚|今天|现在|目前|几点|多久|什么时候|在跑|运营|还有")
SWEEP_MAX = 60          # 扫 60 条线就是 120 个请求；再大的城市让用户点名，别硬扫
STOP_SWEEP_SECONDS = 20  # 车来了没站点搜索只能逐线扫；大目录按秒数收口
# 按车牌查只能逐线翻 104（两家都没这个接口）：一个城扫到此为止，再多群里等不起
PLATE_MAX = 2600      # 深圳 2310 个方向，够了；北京 3477 / 上海 3262 仍只扫前 2600
PLATE_TTL = 60


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
        # 「这个站还在跑的公交线路有几条」：先把条数说出来，别让人自己去数哪行有车
        run = [n for n in names if per[n]]
        order = run + [n for n in names if not per[n]]
        return (f"{city_name}这 {len(names)} 条线这会儿有 {len(run)} 条在跑："
                + "".join(row(n) for n in order))
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


def _plate_key(p):
    """车牌归一：去掉省简称和分隔符，只比字母数字（闽A·17997D / 闽a17997d / A17997D 都对）。"""
    return re.sub(r"[^0-9A-Z]", "", (p or "").upper())


def _plate_core(p):
    """深圳这类城的号两边写得不一样：掌上「BS05070D」，车来了「粤B05070D」。
    只有数字往后那截是一样的，拿它当第二把钥匙。"""
    return re.sub(r"^[^0-9]+", "", _plate_key(p))


def _plate_index(city_name):
    """全城逐线翻一遍，车牌 → (线路, 方向, 站位)。两家都没按车牌查的接口，只能这样。

    返回 (索引, 扫了几个方向, 全城几个方向)，扫不完时前两个不等。
    """
    rows = _catalog(city_name).get("buslines") or []
    dirs = {}
    for r in rows:
        d = str(r.get("upperOrDown") or "")
        if d in ("1", "2"):
            dirs.setdefault(r["lineName"], []).append(d)
    jobs = [(n, d) for n, ds in dirs.items() for d in ds]
    total = len(jobs)
    if total > PLATE_MAX:
        # shortcut: 上千条线的城市扫不满，先扫按号排的主线再补其余的，扫不全别报成「全城都没有」
        trunk = sorted((n for n in dirs if n.endswith("路") and n[:-1].isdigit()),
                       key=lambda n: int(n[:-1]))
        names = trunk + [n for n in dirs if n not in set(trunk)]
        jobs = [(n, d) for n in names for d in dirs[n]][:PLATE_MAX]
    idx, core = {}, {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        got = pool.map(lambda j: _live_my(city_name, j[0], j[1]).get("list") or [], jobs)
        for (name, d), cs in zip(jobs, got):
            for c in cs:
                b = _plate_key(c.get("busNumber"))
                if not b:
                    continue
                hit = (name, d, c.get("stationName") or "在途")
                idx[b] = hit
                k = _plate_core(b)
                if k != b:      # 有些城的号只在数字那截一样；撞了就作废，别把两辆车认成一辆
                    core[k] = None if k in core and core[k] != hit else hit
    for k, hit in core.items():
        if hit and k not in idx:
            idx[k] = hit
    return idx, len(jobs), total


def render_plate(city_name, plate, dir_msg=""):
    """「闽A17997D 在哪」：扫全城找这一辆，报它在哪条线、哪个方向、到了哪站。"""
    try:
        idx, scanned, total = _cached("plate-idx", city_name, PLATE_TTL,
                                      lambda: _plate_index(city_name))
    except Exception:
        return f"{city_name}这边扫车牌的时候上游没响应，过会儿再问一次"
    key = _plate_key(plate)
    if key not in idx:      # 「粤B05070D」在掌上叫「BS05070D」，只有数字那截一样
        key = _plate_core(key)
    hit = idx.get(key)
    if not hit:
        part = (f"只扫了前 {scanned} 条（全城 {total} 条方向）" if scanned < total
                else f"{scanned} 条方向全扫了")
        return f"{city_name}这会儿{part}，没看到 {plate} 这辆车，可能收班了或者在别的城市跑"
    name, d, station = hit
    ends = {r["lineName"]: f"{r.get('from')} ↔ {r.get('to')}"
            for r in (_catalog(city_name).get("buslines") or [])}
    to = ends.get(name, "")
    side = ""
    if to and " ↔ " in to:
        side = to.split(" ↔ ")[1] if d == "1" else to.split(" ↔ ")[0]
    return (f"{plate} 在 {name} 上" + (f"，往{side}方向" if side else "") +
            f"，刚过 {station}")


def _selfno_mate(city_key, city_name, name):
    """一条线上，掌上的车牌和车来了的自编号按同一刻 GPS 一对一配上。
    返回 ({车牌key: (对面号, 距离米)}, {车牌key: 车})。"""
    cid = (cities().get(city_key) or {}).get("chelaile")
    rows = []
    for d in ("1", "2"):
        rows += [c for c in (_live_my(city_name, name, d).get("list") or [])
                 if float(c.get("bus_lat") or 0) and float(c.get("bus_lng") or 0)]
    # 车来了同名的线全拿进来：两家的方向名写法不一样（掌上「汽车总站（二医院）」vs 车来了
    # 「玉林汽车总站」），按 to 过滤会把整个方向滤掉；反方向的车隔着几公里，靠距离就能分开
    cand = []
    if cid and rows:
        try:
            for nm, lid in chelaile.lines(cid):
                if norm_line(nm) == norm_line(name):
                    cand += list((chelaile.live(cid, lid).get("pos") or {}).items())
        except Exception:
            cand = []
    pairs = sorted((m, c["busNumber"], no) for c in rows for no, p in cand
                   if (m := _dist_m({"lat": c["bus_lat"], "lng": c["bus_lng"]}, p)) is not None)
    # shortcut: 两家 GPS 刷新节奏不同，同一辆车常差 500 米上下，1.5km 是实测上限；
    # 车队密集到两辆离同一辆都很近时会配错，那种情况命中不了就走「对不上」
    used_p, used_n, mate = set(), set(), {}
    for m, pn, no in pairs:
        if m > 1500:
            break
        if pn in used_p or no in used_n:
            continue
        used_p.add(pn), used_n.add(no)
        mate[_plate_key(pn)] = (no, m)
    return mate, {_plate_key(c["busNumber"]): c for c in rows}


def _selfno_of(own, other):
    """两边哪个像车牌，另一个就是自编号。深圳反过来：掌上给 BS05070D，车来了给粤B05070D。
    两边都是车牌（车来了也有直接挂车牌的）就没有自编号可报，返空串。"""
    if PLATE_RE.fullmatch(other or ""):
        return own if not PLATE_RE.fullmatch(own or "") else ""
    return other or ""


def render_line_selfno(city_key, city_name, want):
    """「这几辆车的自编号分别是多少」：整条线的车一次问完，不用一辆一辆来。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    if not hit:
        return None
    name = hit[0][0]
    mate, cars = _selfno_mate(city_key, city_name, name)
    if not cars:
        return f"{name} 这会儿没有车报位置，自编号也就没有对照"
    out = [f"{name} 这会儿 {len(cars)} 辆，自编号："]
    miss = 0
    for k, c in cars.items():
        got = mate.get(k)
        own = c.get("busNumber")
        sn = _selfno_of(own, got[0]) if got else ""
        if sn:
            out.append(f"{own} → {sn}")
        else:
            miss += 1
            out.append(f"{own} → 没配上（车来了那边给它挂的也是车牌）" if got
                       else f"{own} → 没配上（车来了那边对不上位置）")
    if miss:
        out.append(f"没配上的 {miss} 辆多半是车来了那条线没报这辆车，隔一分钟再问会变")
    return "\n".join(out)


def render_selfno(city_key, city_name, plate):
    """「XX 的自编号是啥」：掌上只给车牌（104 的 busNumber），车来了只给自编号（licence），
    两家没有共同的键。唯一能对上的东西是这一刻的 GPS —— 同方向里取离它最近的那辆，
    只有「近得离谱、且第二名远得多」才敢报，对不准就说对不上。"""
    try:
        idx, _scanned, _total = _cached("plate-idx", city_name, PLATE_TTL,
                                        lambda: _plate_index(city_name))
    except Exception:
        return f"{city_name}这边查车牌的时候上游没响应，过会儿再问一次"
    full = _plate_key(plate)
    core = _plate_core(full)
    key = full if full in idx else core      # 「粤B05070D」在掌上叫「BS05070D」，只有数字那截一样
    hit = idx.get(key)
    if not hit:
        return f"没扫到 {plate} 这辆车，自编号更对不上"
    name = hit[0]
    mate, cars = _selfno_mate(city_key, city_name, name)
    got = next((v for pn, v in mate.items() if _plate_core(pn) == core), None)
    if got:
        own = next((c.get("busNumber") for pn, c in cars.items()
                    if _plate_core(pn) == core), plate)
        sn = _selfno_of(own, got[0])
        if sn:
            return (f"{plate} 在 {name}上，这会儿的位置对着呢：自编号是 {sn}"
                    f"（两家 GPS 差 {got[1]} 米，这条路上离它最近的一辆）")
        return f"{plate} 在 {name}上，车来了那边给它挂的也是车牌，没另给自编号"
    head = f"{plate} 在 {name} 上，两家的号没对上哪辆"
    return head + "；这条线的车号这会儿配不齐，隔一分钟再问"


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
    # num 里已经带着这个前缀（「安宁市12路」→ 安宁12）时别再拼一次，否则成了「安宁安宁12」
    wants = ([prefix + num] if prefix and norm_city(prefix) != city_key
              and not num.startswith(norm_city(prefix)) else []) + [num]
    # 「香港城巴1」：同号五家公司各有 1 路，只给点名的这家——城市由 city_key 定，算子名留给 _chelaile 分流
    op = next((o for o in OPS if o in prefix), "")
    if op:
        wants = [op + num]
    # 目录里的线名常省掉行政后缀：「东胜区8路」在鄂尔多斯目录里叫「东胜8路」
    bare = re.sub(r"(区|县|旗|市|镇)$", "", prefix) if prefix else ""
    if len(bare) >= 2 and bare != prefix and not num.startswith(bare):
        wants.insert(len(wants) - 1, bare + num)
    # 有前缀要纠前缀截短；「环山二号线」这种自带汉字的线名也要纠（前缀是空的）
    if name and (prefix or not num[:1].isdigit()):
        cat = _catalog(name)
        # 网络挂了才不敢兜底：目录取不到时裸号会答成另一条线（「新乐专1路」→ 1 路）；
        # 业务上的「这城没目录」照旧往下走
        if cat.get("error") and "detail" in cat:
            return cat.get("msg") or ""
        # 前缀被 PREFIX_RE 截短过（「新乐专1路」→「庄新乐专」），目录里有真线名就用真的
        # 点名单（「城巴1」）时不能纠：香港掌上目录里只有一个 1 路（九巴的），纠过去就答错公司
        full = None if op else _exact_line(name, msg, num, city_key)
        if full:
            wants = [norm_line(full)]      # wants 里存的向来是归一化后的号
    last["line"] = wants[0]
    if name:
        for w in wants:
            # 「G02 这几辆的自编号」：整条线一次列完，别只答一辆
            if SELFNO_RE.search(msg) and not PLATE_RE.search(msg):
                out = render_line_selfno(city_key, name, w)
            # 「到终点站还要多久」问的是时长，不是起讫站
            elif ENDS_RE.search(msg) and not re.search(r"多久|几分钟|几站|多远", msg):
                out = render_ends(name, w)
            elif PRICE_RE.search(msg):        # 问票价（掌上把价格写在 103 的 commonts 里）
                out = render_price(name, w, msg)
            elif TIMES_RE.search(msg):        # 问首末班时刻表
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
    if CODE_RE.search(msg):      # 群里贴的脚本不查公交，交给模型
        return ""
    # 微信的引用整段是 XML：真要答的那句在 <title>，被引用的话在 <refermsg><content>
    if "<refermsg>" in msg or "<appmsg>" in msg:
        rm = WX_REF.search(msg)
        if rm:
            ctx = (ctx + " " + html.unescape(re.sub(r"<[^>]+>", " ", rm.group(1)))).strip()
        tm = WX_TITLE.search(msg)
        msg = html.unescape(tm.group(1)) if tm else re.sub(r"<[^>]+>", " ", msg)
    last = _last(key)
    t0 = time.time()
    msg = MARKUP_RE.sub(" ", AT_RE.sub(" ", msg))
    qm = QUOTE_RE.search(msg)
    if qm:
        # 引用内容只当上下文：引用「G02 现在 2 辆」再问「桂K06833D 自编号是啥」，G02 会把车牌问题顶掉
        ctx = (ctx + " " + qm.group(1)).strip()
        msg = msg.replace(qm.group(0), " ")
    raw = msg
    sm = SRC_RE.search(msg)
    src = ("chelaile" if sm.group("cl") else "mygolbs") if sm else None
    # 「用掌上公交查G02路」：「掌上公交查」不是城市名，别让前缀截出「上公交」当城
    msg = SRCPRE.sub("", msg)
    line = parse(msg)
    # 本句自己写了文字线名（「用车来了查湛江宝钢专线」）时 parse 认不出，但那就是用户要的那条；
    # 别拿 ctx 里的线号顶掉它——先问过 M308 再问这句，会答成「湛江没查到 M308」
    if not line and ctx and not TEXT_LINE.search(msg) \
            and not re.search(r"在跑|运营|哪条|哪些|有几|几条|条线路|全部|车牌|号牌", msg) \
            and not PLATE_RE.search(msg):
        # 引用别人那句追问「这路还有几辆」，线路号在被引用的话里。
        # 但本句问的是全城（「还有没有在跑的线路」「每辆车的车牌」）时不能拿引用里那一路顶替——
        # 引用的是「还有 5 条在跑」，parse(ctx) 会随便挑一条线答成它的实时
        line = parse(ctx)
    # 「从车来了的那个看一下数据」没写线路号：说的就是上一条问的那路车。
    # 但「用车来了查化州-湛江线」也带「查」，那是点名的另一条文字线名（parse 认不出带「-」的），
    # 当成追问上一条就会答成上一轮的 506——只有指着上一条说（这个/那条）或整句只剩追问词时才续
    _bare = re.sub(r"[看查询数据接口一下怎么样呢吗的了，。？?！!\s]", "", SRCPRE.sub("", raw))
    inherited = False
    if src and not line and last.get("line") and re.search(r"看|查|数据|接口", raw) and (
            re.search(r"这个|那个|这条|那条|该线|这路|那路", raw) or len(_bare) <= 3):
        line = (last["line"], "")
        inherited = True
    prefix = line[1] if line else ""
    # 「泉州公交11路」「泉州公交K502」：前缀截到的是「泉州公交」，去掉「公交」才是城市名，
    # 留着就会拼成「泉州公交K502」这种不存在的线
    prefix = re.sub(r"公交$", "", prefix)
    # 「济宁城际公交C601a路」的「城际」是叫法不是线名（目录里叫 C601路），留着会答成「没查到「城际C601A」」
    if prefix in ("城际", "城乡", "市郊", "城市", "市域"):
        prefix = ""
    if prefix in ("那", "这", "该", "此", "本"):      # 指示词不是县名，别拼进线路名
        prefix = ""
    # 本句先单独判：一直问深圳、下一句突然问湛江，ctx 里那句深圳也会被 detect_city 匹到，
    # 两个城名一样长，取谁全看字典顺序——湛江就被顶成深圳查了
    city_key, explicit = detect_city(msg, prefix, default_city)
    if not explicit:
        city_key, explicit = detect_city(msg + " " + ctx, prefix, default_city)
    base = norm_city(default_city)
    # 「闽AYC505」没写城市：车牌头两位就是车管所，能定到哪座城（否则会落回默认城白扫一遍）
    pm = PLATE_RE.search(msg)
    if pm and not explicit:
        pc = PLATE.get(pm.group(1)[:2].upper())
        if pc and norm_city(pc) in cities():
            city_key, explicit = norm_city(pc), True
    # 前缀自己就是一座城（「玉林 容县 11 路」）时以它为准：玉林的目录里没有容县11路，
    # 拿玉林的目录查裸号会静默答成玉林 11 路
    if prefix and (prefix in cities() or norm_city(prefix) in cities()):
        city_key, explicit = (prefix if prefix in cities() else norm_city(prefix)), True
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
        # 「刚才看到」「听说」也是两个汉字的前缀，但不问公交，别拿它去问上游、更别回「这座城没数据」
        if not city_key and len(prefix) >= 2:
            bn = (cities().get(base) or {}).get("mygolbs")
            # 前缀截到的常是站名的尾巴（「我在汽车总站等 9 路」→「车总站等」）：
            # 它在默认城里是个站，就按默认城查——这条不看 looks_like_ask，「我在…等」会被当成闲聊
            if line and bn and find_stop(bn, prefix)[0]:
                city_key = base
            elif looks_like_ask(msg):
                if not _covered(prefix):
                    # 别写「查不了」：main.py 的 FALSE_CLAIM 会把这句当假话整句吞掉，用户啥也收不到
                    return f"{prefix}这座城暂时没有公交数据，掌上和车来了都没覆盖"
                city_key = norm_city(prefix)
    if not city_key:
        return ("" if not looks_like_ask(msg) else
                f"「{prefix or '这个城市'}」是哪儿的公交？没认出来，写「{prefix} {line[0] if line else '1'} 路」这种试试")
    # 表外城市直接用用户写的中文名当 CITYNAME（掌上就吃中文名）
    name = (cities().get(city_key) or {}).get("mygolbs") or (prefix if city_key not in cities() else None)
    prev = last.get("city")
    last["city"] = city_key
    # 「北流有哪些公交线路」问的是线路清单，别被下面的 CITY_CO_RE 抢成公司清单。
    # 「伦教街道办事处公交线路」问的是那个地方的站，只有明写了城市名（北流/玉林）才算整城
    # 「从玉林汽车总站到玉林北站坐哪条公交线路呢」是问换乘，不是要整城清单
    # 「玉林八中有没有什么公交线路」问的是那所学校的线路，不是整城 25 条
    if not line and name and not hop(msg) and not re.search(r"在跑|运营|还有", msg) and (
            CITYLINES_RE.search(msg) or (explicit and "公交线路" in msg)) and _city_wide(msg, name):
        out = render_city_lines(name)
        if out:
            return out
    # 「玉林公交公司电话是多少」：没带线路号，问的是整座城的公司，别回空交给模型（它会编）
    if not line and CITY_CO_RE.search(msg):
        out = render_city_company(name, msg)
        if out:
            return out
    # 「闽A17997D 在哪」：只给车牌没给线路，全城逐线翻一遍找这辆
    # 「深圳M470路粤B08269D自编号」带了线路名，问的仍是自编号，别答成实时车
    if pm and (not line or SELFNO_RE.search(msg)):
        if SELFNO_RE.search(msg):
            return render_selfno(city_key, name, pm.group(1))
        return render_plate(name, pm.group(1))
    got = hop(msg)
    # 「有一条线路查不到实时信息我就不写出来了（）」：HOP_RE 按「到」切出两个假站名，
    # 整句没问法、两个端点也不在站表里——是吐槽，不是问路，别抢答（交给模型接话）
    if (got and name and not re.search(r"[?？]|怎么|咋|如何|怎样|哪|多少|多久|几站|[坐乘搭]", msg)
            and not (find_stop(name, got[0])[0] or find_stop(name, got[1])[0])):
        got = None
    # 先按当前城市的目录真查一遍：北流是县级市、自己有一条「北流汽车总站→玉林北站」的
    # 北流105路，只看两个站名属于哪座城会把这条线误判成跨城
    # 掌上自己有换乘方案（CMD=111：两次换乘也排、按总时长排序），手算的只换一次、经常不是最快那条，
    # 所以没直达才交给它排
    # 能直达就先让 render_hop 答——它带方向、车在哪、首末班；111 那边如果起点站记录只挂了
    # 一条线（同一所学校拆成好几条记录），会把城际公交当前半段，排出来的方案比直达还差
    out = ((render_hop(city_key, name, got, only_direct=True)
            or render_plan(name, got[0], got[1])
            or render_hop(city_key, name, got))
           if (got and name) else None)
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
            # 跨城不等于坐不上：玉林28路一路开进北流（北流客运中心、国际陶瓷贸易城），
            # 这种线就在出发城的目录里，先看能不能坐到对面城再拒
            ride = _into_city(name or city_key, got[0], cb)
            return ride or f"{got[0]}在{ca}，{got[1]}在{cb}，跨城的公交串不起来，这段得坐高铁或长途"
    if got and name:
        # 有一边不是这座城的站（render_hop 的空串）：问一句，别返空让模型干等着没话说
        if explicit:
            miss = [x for x in (got[0], got[1]) if not find_stop(name, x)[0]]
            return (f"没查到从「{got[0]}」到「{got[1]}」的换乘方案："
                    f"{norm_city(name)}站表里没找到" + "、".join(f"「{m}」" for m in miss or got))
        return (f"「{got[0]}」到「{got[1]}」在{norm_city(name)}的站表里没找全，"
                    f"是哪座城的？说个城市名我再查")
    # 「那玉林北站定制夜班车呢？」线名不是数字+路，parse 认不出；目录里有就当场认。
    # 这句自己点了线名就别沿用上一条的号：刚问完 12 路再问「用车来了查宝钢专线」会答成 12 路
    if name and (not line or inherited):
        hit = _msg_line(name, msg, city_key)
        if not hit and not explicit and prev in cities() and prev != city_key:
            # 「妇幼专线」这种纯文字线名只在车来了那边有：默认城目录里没有，
            # 沿用上一条问过的城（刚问完湛江吴川二路，再问妇幼专线就是湛江的）
            pname = (cities().get(prev) or {}).get("mygolbs")
            hit = _msg_line(pname, msg, prev) if pname else None
            if hit:
                city_key, name = prev, pname
                last["city"] = prev
        if hit:
            # _pick 拿归一化名做全等比，原始线名（带「（28路）」）一个都匹不上
            line, prefix = (norm_line(hit), ""), ""
            inherited = False
    # 「北流城南站方向还有没有」：这句没写线号，问的是上一条那路车的某一个方向
    if name and _dir(msg) and (not line or not line[0]):
        cl = parse(ctx)                      # 微信引用里那句话带着线号
        want = ((cl[1] + cl[0]) if cl and cl[1] else (cl[0] if cl else "")) or last.get("line") or ""
        out = render_dir_window(name, want, _dir(msg)) if want else None
        if out:
            return out
    if name and TRANSFER_RE.search(msg):
        # 「这条线路每站能换什么」没有线路号，用上一条答过的线路
        want = (prefix + line[0] if prefix and not explicit else line[0]) if line else last.get("line") or ""
        out = render_transfer(name, want, msg) if want else None
        if out:
            last["line"] = want
            return out
    # 「这三辆车的自编号分别是多少」：没写线路号时沿用上一条问过的线，整条线一次列完
    if not line and name and SELFNO_RE.search(msg) and not PLATE_RE.search(msg):
        want = last.get("line") or ""
        out = render_line_selfno(city_key, name, want) if want else None
        if out:
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
        # 「新乡胖东来三胖有哪些公交」：站表里没这个站就直说。回空的话模型会自己改口去问
        # 「新乡有哪些公交线路」，把全城 93 条倒进群里
        if STOPASK_RE.search(msg) and not _NOTPLACE.search(msg):
            place = STOPASK_RE.sub("", msg).replace(norm_city(name), "", 1).strip()
            if len(place) >= 2:
                core = re.sub(r"(?:客运站|汽车站|火车站|公交站|客运中心|站)$", "", place)
                names = {r.get("stationName") for r in stops(name, core) or [] if r.get("stationName")}
                if core and core != place and len({n for n in names if n.startswith(core)}) == 1:
                    got = render_stop(city_key, name, next(iter(n for n in names if n.startswith(core))))
                    if got:
                        return got
                return f"{norm_city(name)}站表里没查到「{place}」这个站"
        # 站名猜不出是哪座城的：问一句，别回空让模型自己圆话（「返回的是玉林全市正在跑的线路」）。
        # 「有一条线路查不到实时信息我就不写出来了（）」是吐槽，反问就抢话了
        if not explicit and re.search(r"站|线路|几路", msg) \
                and not re.search(r"查不到|不写|没查到|找不到", msg):
            hint = re.sub(r"公交$", "", STOPSUF.sub("", TAIL.sub("", msg))).strip()
            return (f"「{hint}」这个站没查到，是哪座城的？说个城市名我再查" if hint
                    else "这个站没查到，是哪座城的？说个城市名我再查")
        # 纯文字线名（「妇幼专线」）当前城目录里也没有：问一句是哪座城的，
        # 别回空——回空模型只能自己圆成「查不到」
        if not explicit:
            hint = re.sub(r"^(?:用车来了|掌上公交|掌上|查询|查一下|查下|看看|看一下|看看有)[\s，,]?",
                          "", TAIL.sub("", msg)).strip()
            if TEXT_LINE.fullmatch(hint):
                return f"「{hint}」是哪座城的线？说个城市名我再查"
    # 这一句只给「刚刚真的没连上、而且问的是公交」的情况：以前按时间窗判断，
    # 任何一句闲聊都会收到「上游没响应」，成了「彻底变成问公交工具」的由来
    return "上游这会儿没响应，过会儿再问" if (BUS_RE.search(msg) and _up_fail[0] >= t0) else ""
