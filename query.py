"""全国公交实时查询：掌上公交优先，车来了兜底。

只问「有没有车、车到哪」，不做换乘、不做历史。
接口细节见姊妹项目 yulin-bus-activity 的 docs/掌上公交API.md 与 docs/车来了API.md。
"""

import difflib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yulin          # 玉林导乘资料：只在上游给不出的地方补（换乘站表、上车点、联营线无实时）
try:
    import watch       # 到站提醒：说了「2路到汽车总站叫我」就在后台盯着
except ImportError:
    from . import watch

try:  # AstrBot 按包加载时走相对导入，单文件调试时退回顶层模块
    from . import buspedia, chelaile, mygolbs
    from .plates import PLATE
except ImportError:
    import buspedia
    import chelaile
    import mygolbs
    from plates import PLATE

HERE = Path(__file__).parent
CHELAILE_CITIES = json.loads((HERE / "cities.json").read_text("utf-8"))
# 车来了单独开成城市的县级市：归一化城名 → (cityId, 掌上原名)。
# 安宁（昆明下辖县级市）在车来了是独立城市 342「安宁公交」，线名带县名前缀
# （安宁1 / 安宁12 / 安宁K12）。以前借父城昆明的 081，只查得到昆明市区的线，
# 群里问「安宁12」被答成「两家都没收，只有建档资料」——可车来了明明有实时。
# 出处：车来了 H5 的城市配置（web.chelaile.net.cn/customer_ch5/scripts/scripts-*.js）。
CHELAILE_OWN = {
    "安宁": ("342", "昆明市"),
}
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

# 协议、请求、缓存都在 mygolbs.py 里，别的组件也能直接 import 那两个模块
CITY_TTL, CATALOG_TTL, LIVE_TTL = mygolbs.CITY_TTL, mygolbs.CATALOG_TTL, mygolbs.LIVE_TTL
LABEL = {"mygolbs": "掌上公交", "chelaile": "车来了"}
_cache = mygolbs.cache
_cached = mygolbs.cached
# 连不上上游的时刻由 mygolbs.post 记，answer 末尾要靠它决定回不回「过会儿再问」
_up_fail = mygolbs.last_fail

# 带字母的线号：B601 / K502 / D01 / 121A / 1001M / C621A（字母在前在后都有，最多五位）
# 必须至少含一个字母，否则「11路」「2026年」会被当线号
# 字母前后各最多一个：「9KB」「500MB」「3.2GB」这种量纲不是线号
# shortcut: 目录抽了 7 城 7 千多条线名，没有「数字+两个字母」的线号，出现再放宽
ALPHA_PAT = (r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]{2,8}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])"
             # 深圳 DK401 / 聊城 KS7 / 苍南 CX01 / 日照 601B2：两个字母前缀、号中间夹字母都真的存在
             # 后缀两个字母必须跟着数字（601B2），否则「9KB」「MP3」这种就被认成线号
             # 双字母前缀只认全大写——目录里 43 个双字母前缀只有「Cj」例外，
             # 放开大小写会把「hy4」这类闲聊吃进来
             r"(?:[A-Z]{2}|[A-Za-z]{0,1})\d{1,4}(?:[A-Za-z]\d{0,3}|[A-Za-z]{2}\d{1,3})?(?![A-Za-z0-9])")
ALPHA_RE = re.compile(ALPHA_PAT)
# 香港一个「1」对应五家公司（城巴/九巴/屿巴/新界小巴/港岛小巴）：点名单算一家，
# 长名字排前面，「小巴」放最后当统称（港岛小巴/新界小巴都含这两个字）
OPS = ("新界小巴", "港岛小巴", "九龙小巴", "城巴", "九巴", "屿巴", "龙运", "新巴", "港铁", "小巴")
# 「玉林 G02 路」「B1 有车吗」「105路」都认；裸数字必须带「路」，防「8点」误判
# 「路」后面紧跟的定性词（夜班/专线）得一起吃掉，否则「6路夜班」会被截成「6」
LINE_RE = re.compile(
    # 真地铁也在这套目录里：「地铁10号线」「轨道交通1号线」「地铁广佛线」
    # 「地铁快线R101路」是东莞的一条线，不是「地铁快线」：定性字后面不算线名
    r"(?P<metro>(?:地铁|轨道交通)\s*\d+\s*号线|地铁\s*(?![快慢支专干微高])[\u4e00-\u9fa5]{1,3}线)"
    # 乌鲁木齐/厦门的 BRT：「BRT1号线」不算地铁，也不是裸数字。
    # 后缀可以省：「北京BRT4」口语里就这么写，目录里叫「快速公交4线」
    r"|(?P<brt>BRT\s*\d{1,3}\s*(?:号线|线|路)?)"
    # 「环山二号线」「高新二号线」：号码夹在中间，不带「地铁」也是目录里的真线名。
    # shortcut: 只吃 4 个汉字前缀，「玉林二号线」这种城市名+号线的写法会被整段吃掉
    # 前面带「地铁/轨道交通」的归上面那条（最左匹配会先把城市名一起吃进来，得挡掉）
    r"|(?<![A-Za-z0-9])(?![\u4e00-\u9fa5]{0,4}(?:地铁|轨道交通))"
    r"(?P<numline>[\u4e00-\u9fa5]{0,4}?[一二三四五六七八九十\d]{1,3}\s*号线)"
    r"|(?P<alpha>" + ALPHA_PAT + r")(?:\s*路)?"
    # 带前导零的四位线号（重庆「0492」）口语里不加「路」字；裸的 492 不加路字就太宽（可能是数量）
    r"|(?<![0-9])(?P<zero>0\d{2,3})(?![0-9])"
    r"|(?<![0-9])(?P<num>(?:\d{1,4}-)?\d{1,4})\s*(?:夜宵|夜班|通宵)?\s*(?:[路线]|号线)(?P<qual>夜线|夜班线|夜宵线|夜班|区间|支线|快线|专线)?"
    # 「济宁76夜线」不带「路」字：目录里叫「76路夜线」，少写一个字也得认成这条夜线，不能削成 76路
    r"|(?<![0-9])(?P<nnum>\d{1,4})\s*(?P<nqual>夜线|夜班线|夜宵线)"
    # 「131区间」不带「路」也算线路——石家庄目录里就叫「131路区间」
    r"|(?<![0-9])(?P<inum>\d{1,4})\s*(?P<iqual>区间|支线|快线|专线)"
    r"|(?P<cnnum>[一二三四五六七八九十]{1,3})\s*路"
    # 「北流105配车」：问配车时口语不带「路」字，号后面直接跟问法
    r"|(?<![0-9])(?P<fnum>\d{1,4})\s*(?=配[车了几台]|用什么车|什么车(?:型|跑)?)"
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
                       r"搜一下|搜搜|一下|帮我|麻烦|你|我|再|还|也|就|那|这|"
                       # 裸「查」（「查南宁朝阳广场的公交线路」）：「查询/查一下」在上面已吃
                       # 后面得跟 4 个字以上才剥，不然「查桥站」这种真站名会被剥成「桥站」
                       r"查(?=[\u4e00-\u9fa5]{4,})|"
                       # 「那你去查南宁朝阳广场」：LEAD 只剥开头，剥完「那」才露出「去查」，补一次
                       r"去)+")
JUNK_TAIL = re.compile(r"(?:看看|查查|看看有|看|查|有|的|了|来|台|部|辆|只|几)+$")
# 像在问公交才去查。线路/路线/站点这三个词在闲聊里到处都是（「人生路线」「这个站点好难」），
# 不单独算信号；实词、线号、数据源名才是。
# 完整车牌：省简称 + 字母 + 5 位（普通）或 6 位（新能源，闽A17997D）
PLATE_PAT = (r"[京津沪渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新]"
             r"\s*[A-Z]\s*[A-Z0-9]{4,6}(?![A-Za-z0-9])")
BUS_RE = re.compile(
    r"公交|巴士|公共汽车|公车|地铁|轨道交通|几路|哪路|路车|末班|首班|到站|几站|\d\s*号线|号线|"
    # 「北京BRT4」「厦门BRT1」：群里就这么写，不加这条快路压根不触发，全靠模型想起来调工具
    r"BRT\s*\d{1,3}(?![0-9])|快速公交\s*\d{1,3}(?![0-9])|"
    r"怎么坐|直达|换乘|车来了|掌上公交|车牌|号牌|自编号|自编|夜宵线|夜班线|夜线|(?<![今昨天每这那晚])夜班车|通宵线|(?<![0-9])0\d{2,3}(?![0-9])|\d{1,4}\s*[路线]|有(?:几|多少)条?线路|有哪些线路|哪几条线路|"
    r"(?:城巴|九巴|屿巴|新巴|龙运|小巴)\s*\d{1,4}|" + PLATE_PAT + r"|配车|配属|" + ALPHA_PAT +
    r"|[\u4e00-\u9fa5A-Za-z0-9]{2,10}(?:专线|快线|支线)|[\u4e00-\u9fa5]{2,6}[东南西北中]\d{1,4}(?![0-9])|"
    r"提醒我|叫我|喊我|到站提醒|取消提醒|我的提醒|提醒列表|准备到了|快到了|哪[辆部台].{0,6}(?:快|先)|有?多久来车")
# 纯文字线名（「妇幼专线」「宝钢专线」）不含数字，也不是公交词——靠这两个专有线名尾巴认
TEXT_LINE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,10}(?:专线|快线|支线)")
# 城市名后面跟裸号（「西安616」，不带「路」）。BUS_RE 认不出这种写法，快路不触发、整句
# 交给模型，模型一声不吭——群友那次就是这么没回。进快路后还得查这城市真在表里，
# 不然「今天616」也会被当公交查
CITYNUM_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}\s*\d{1,4}(?![\d号线路])")
# 快路抢答前的最后一道：带问法就答；像叙述（「11路那车真挤」「今天坐9路回家累死了」）
# 就交给模型。默认答，只挡明显的叙述句——挡多了会把「谈固站有什么线路」也挡掉。
ASK = re.compile(r"多久|几站|几分钟|几辆|几台|几点|有车|有没有|有吗|还有|在哪|到哪|到吗|怎么坐|怎么去|"
                 r"换乘|直达|首班|末班|时刻|查|看一下|看看|多少|什么车|哪几路|哪路|所有站|站点|站表|班车|"
                 r"能不能|可以|来吗|准备到了|快到了|快到站|来车")

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
    s = name or ""
    # 「宜州606路（快班）」和「宜州606路快线」是同一条：括号里的快班/普班是定性词，
    # 直接跟着括号一起剥掉就分不出快慢两条线了（掌上括号写法、车来了写在线名尾巴）
    s = re.sub(r"[（(]\s*(快|普)(?:班|线|车)?\s*[)）]", r"\1", s)
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"\s+", "", s).replace("轨道交通", "地铁")
    s = re.sub(r"(线路|路|线)", "", s)
    # 快线/快班/快车 与单个「快」等价（车来了叫「宜州606路快线」，用户写「宜州606快线」）
    s = re.sub(r"(快|普)(?:班|线|车)", r"\1", s)
    return s.upper()


def norm_city(name):
    s = (name or "").strip()
    short = SUFFIX.sub("", s)
    # 剥完不足两个字说明这个缀本来就是名字的一部分，原样保留
    return short if len(short) >= MIN_CITY_KEY else s


def _slash_parts(name):
    """目录里一个名字写两条线时（连云港「B1路/B1路夜」、郑州「888路/郑州-中牟」），斜杠两边各算一条。

    返回各段的归一化名，问「B1」就靠它命中「B1路/B1路夜」。
    """
    return [norm_line(x) for x in re.split(r"[/／]", name or "") if x.strip()]


# BRT 的叫法三家各写各的：车来了北京叫「快速公交4线」、掌上厦门叫「BRT1号线」，
# 群里人写「BRT4」「快速公交4」「快4」。同一个号，得互相认得，否则
# 「北京BRT4」只能靠模糊匹配落到通州的 T4 路上（假答案）
_BRT = re.compile(r"^(?:BRT|快速公交|快)\s*(\d{1,3})(?:号?线|路)?$")


def brt_alias(want):
    """同一个 BRT 号的其它写法（已归一化）。查不到时按这个顺序再试一遍目录。"""
    m = _BRT.fullmatch(norm_line(want))
    if not m:
        return []
    n = m.group(1)
    out = [f"快速公交{n}", f"快{n}", f"BRT{n}号", f"BRT{n}"]
    return [x for x in dict.fromkeys(out) if x != norm_line(want)]


def _place_head(msg):
    """线号前面那个词是不是地名（不是站名）：「惠州博罗233路」的「博罗」、「安宁区8路」的「安宁区」。

    博罗这类县名不带区/县后缀，光看后缀认不出；拿它当站名搜会命中「博罗天虹」，
    然后回一句「233路 不经过博罗天虹」——问车的人读这个没意义。
    """
    head = re.split(r"\d", (msg or "").strip(), 1)[0].strip()
    head = re.sub(r"^(?:帮我|麻烦|请问|问一下|查一下|查下|看看|看一下|查询)", "", head).strip()
    if not head:
        return False
    if head.endswith(("区", "县")):
        return True
    rest = head
    for k in sorted(cities(), key=len, reverse=True):
        if rest.startswith(k):
            rest = rest[len(k):]
            break
    return bool(rest) and _is_place(rest)


def _is_place(name):
    """这个词是不是地名（县/区/县级市）：有数据依据的才算，靠后缀或区县表。

    反向用例：「快速公交1线」的 prefix 剥掉「公交」后剩「快速」，目录里又正好有
    「快速直达专线1路」，光看「目录里有以它开头的线」会把这个叫法当成县名。
    """
    s = (name or "").strip()
    if not s:
        return False
    if s in DIST_BARE or s in DISTRICTS or s in cities():
        return True
    return bool(re.search(r"[区县市旗镇盟州]$", s)) and len(s) >= 2


# 区县名后面跟这些字，说明它是在当地方说（「元氏火车站」「博罗233」），不是被夹在更长的词里
_PLACE_TAIL = re.compile(r"^(?:火车站|汽车站|高铁站|客运站|站|路口|路|街|大道|镇|乡|村|桥|"
                         r"小区|广场|公园|医院|学校|中学|小学|大学|机场|码头|门|口)")
# 区县名前面跟这些字，说明是「我在/到/从 X」那种说法
_PLACE_LEAD = "我在到从去至的和与、在"


def _at_place_boundary(text, name):
    """区县名在 text 里是不是「当地方说」的位置，而不是被夹在更长的词里。

    「金龙湾」里的「龙湾」是温州市龙湾区，可它前面粘着「金」——那是站名金龙湾，不是地名。
    句首、前面是标点/空格/「我在到从」、后面接地名后缀（站、路口）或线路号，这几种才算地名。
    只看右边不够：「金龙湾」的「龙湾」正好在句尾。
    """
    if not name:
        return False
    text = text or ""
    i = text.find(name)
    while i >= 0:
        j = i + len(name)
        lead_ok = (i == 0 or (not ("\u4e00" <= text[i - 1] <= "\u9fff"))
                   or text[i - 1] in _PLACE_LEAD)
        tail_ok = (j < len(text) and ((text[j].isascii() and text[j].isalnum())
                                      or _PLACE_TAIL.match(text[j:])))
        # 后面接地名后缀（「机场一号航站楼元氏火车站」）比前面重要：那本身是个完整站名
        if i == 0 or tail_ok or (lead_ok and j >= len(text)):
            return True
        i = text.find(name, i + 1)
    return False


def _district_city(text):
    """「元氏火车站」里带的是裸县名：翻成所在地级市，比落回默认城强。

    光看「区县名有没有出现在句子里」会把站名撕成地名（见 _at_place_boundary），
    所以两侧的边界必须卡住。
    """
    hits = [b for b, v in DIST_BARE.items() if v and _at_place_boundary(text, b)]
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
    if m and m.group("alpha") and re.fullmatch(r"[1-9][GDMK]|MP\d", m.group("alpha")):
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
    num = (m.group("nnum") + m.group("nqual")) if m.group("nnum") else \
        (m.group("inum") + m.group("iqual")) if m.group("inum") else \
        (m.group("alpha") or m.group("num") or m.group("zero") or m.group("metro") or m.group("brt")
         or m.group("numline") or m.group("fnum") or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
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
    if _chatty_code(msg):            # 「…那个 h5 起步的啊」：h5 是网页版本，不是线号
        return False
    if _about_someone(msg):
        return False
    # 「G02到玉林北站叫我」「我的提醒」没有问法是祈使句，但确实是公交（答应了得真去盯）
    if WATCH_RE.search(msg) or WATCH_OFF_RE.search(msg) or WATCH_Q_RE.search(msg):
        return True
    # 「我在汽车总站等1路和G02」：第一人称+所在站，是站在站台上问车；「我」被 CHAT 当成闲聊了
    if re.search(r"我在.{0,12}(?:站|路口|广场|公园|医院|学校|门口|桥)", msg):
        return True
    # 「用s5注册（」「2F有车吗」「E4呢」：两位的「字母+数字」被认成线号，可整句没写「路/线/号」、
    # 也没城市名，不知道是哪座城的线，答了只会是「没查到 2F，玉林这边有的是…」
    # （G02 / M470 / 聊城KS7 这种带多位数字的仍算线号）
    if re.search(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,2}\d|\d{1,2}[A-Za-z])(?![A-Za-z0-9])", msg) \
            and not re.search(r"路|线|号", msg) and not detect_city(msg, "", "")[1]:
        return False
    # 「微信ZFB收眯 一天8OO0」：广告把 O 当 0 写，「8OO0」被 ALPHA_PAT 当成线号，
    # 整句没问法词也没闲聊词就落进兜底。数字和字母混排的（8OO0/9999x/9KB/3D打印/5G网络/601B2）
    # 没公交字眼也没城市名就不是问公交；G02/M470/DK401 字母开头的线号不受影响
    if re.search(r"(?<![A-Za-z0-9])\d+[A-Za-z]+\d*(?![A-Za-z0-9])", msg) \
            and not re.search(r"路|线|号|[站车]|公交|地铁", msg) and not detect_city(msg, "", "")[1]:
        return False
    # 「c2c出呗」：C2C/B2B 这种「一两位字母夹一两位数字」的缩写被 ALPHA_PAT 当线号（真线号
    # C621A 是三位数，B1K/KS7 这类要带「路/线/号」或问法），整句又只剩「出呗」这种话，
    # 落进最后那条「没闲聊词就当问句」的兜底 → 群里收到「没查到「C2C」这条线，玉林这边有的是 2路…」
    if re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{1,2}[A-Za-z]{1,2}(?![A-Za-z0-9])", msg) \
            and not re.search(r"路|线|号|[站车]|公交|巴士|地铁", msg) \
            and not detect_city(msg, "", "")[1] and not ASK.search(msg):
        return False
    # 「哦，最早是玉林那个 h5 起步的啊」：小写字母+一位数字被 ALPHA_PAT 当线号（h5）。
    # 目录里小写的线名都是两位数字（南宁 k10路、昆明 Cj10路），一位数字的一个都没有
    if _chatty_code(msg):
        return False
    return bool(ASK.search(msg)) or not CHAT.search(msg)


def _chatty_code(msg):
    """句子里那个「像线号」的东西其实是口语里的版本号/型号：「拿玉林那个 h5 起步的」。

    目录里小写线名都是两位数字（k10路、Cj10路），一位数字（h5、k3）一个都没有；
    这类词只在句子里聊到网页/软件时出现，只要没写「路/线/号」就别当线号。
    """
    return bool(re.search(r"(?<![A-Za-z0-9])[a-z]{1,2}\d(?![A-Za-z0-9])", msg or "")) \
        and not re.search(r"路|线|号", msg or "")


def _about_someone(msg):
    """跟群里别人说话、不是问机器人：「不是，他查的是公交公司门口的公交站」。
    裸「查」进了 ASK，第三人称的叙述句也会被当问句接住 → 真去反问「是哪座城的」。
    带线路号或问法的照旧放行（「不是，G02 现在有车吗」）。"""
    return bool(re.search(r"(?<!其)他|她", msg)) and not re.search(r"\d", msg) \
        and not re.search(r"哪|几|多少|有车|有没有|有吗|怎么|咋|多久|几点", msg)


def _lines(msg):
    """句子里问了哪几条线：「樟13 / 12路」「11路和12路」都要答，别只答第一条。"""
    out, end = [], -1
    for m in LINE_RE.finditer(msg):
        if m.start() < end:            # 「131路区间」先匹到「131路」，别再算一条
            continue
        end = m.end()
        num = (m.group("nnum") + m.group("nqual")) if m.group("nnum") else \
            (m.group("inum") + m.group("iqual")) if m.group("inum") else \
            (m.group("alpha") or m.group("num") or m.group("zero") or m.group("metro") or m.group("brt")
             or m.group("numline") or str(_cn_num(m.group("cnnum")))) + (m.group("qual") or "")
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


def _merge_cities(ch_ids, my_names):
    """把两家的城市表并起来：{归一化名: {"mygolbs": 平台原名, "chelaile": cityId}}。

    单独抽出来是为了能自测（真跑 cities() 要连网拉掌上的城市表）。
    """
    idx = {}
    for name, cid in ch_ids.items():
        idx.setdefault(norm_city(name), {})["chelaile"] = cid
    for name in my_names:
        idx.setdefault(norm_city(name), {})["mygolbs"] = name
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
    # 车来了把某些县级市单独开了一个城市，线名全部带县名前缀（安宁公交 cityId=342，
    # 里面是「安宁1」「安宁12」「安宁K12」…）。这些城不能借父城的 id：借 081（昆明）
    # 只查得到昆明市区的线，安宁12 会被答成「两家都没收」。
    # 来源：车来了 H5 包里的城市配置（web.chelaile.net.cn/customer_ch5/scripts/*.js）。
    for _k, (_id, _my) in CHELAILE_OWN.items():
        _e = idx.setdefault(_k, {})
        _e.pop("_borrow", None)
        _e["chelaile"] = _id
        if _my and not _e.get("mygolbs"):
            _e["mygolbs"] = _my
    return idx


def cities():
    """归一化城市名 → {"mygolbs": 平台原名, "chelaile": cityId}"""
    return _cached("cities", "", CITY_TTL,
                   lambda: _merge_cities(CHELAILE_CITIES, mygolbs.city_names()))


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
    dd = [k for k in DISTRICTS if _at_place_boundary(msg, k)]
    if dd:
        key = norm_city(DISTRICTS[max(dd, key=len)])
        if key in idx:
            return key, True
    # 群里写的是裸县名：「新郑站有什么线路」里没有「新郑市」，districts.json 的键带「市」
    bb = [k for k, v in DIST_BARE.items() if v and _at_place_boundary(msg, k)]
    if bb:
        key = norm_city(DIST_BARE[max(bb, key=len)])
        if key in idx:
            return key, True
    # 「东胜区8路」：区县名不是城市，掌上也不认它当城市名，得先翻到属地级市
    if prefix and DISTRICTS.get(prefix):
        key = norm_city(DISTRICTS[prefix])
        if key in idx:
            return key, True
    # 「佛256线」：城市名少写一个字，但只有佛山以「佛」开头——别去问「佛是哪儿的公交」
    if prefix:
        cand = [k for k in idx if k.startswith(prefix)]
        if len(cand) == 1:
            return cand[0], True
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
        r = mygolbs.catalog(raw)
    except Exception:
        return False
    return r.get("status") == "1" and bool(r.get("buslines"))


def _tail_line(name, want):
    """name 以 want 结尾，且 want 是一个完整的号（前面不粘数字）。

    「金城江8」算「8」，「金城江18」不算——1 和 8 挨着，读作 18。
    """
    if not name.endswith(want):
        return False
    i = len(name) - len(want)
    return i == 0 or not (name[i - 1].isdigit() and want[:1].isdigit())


def _pick(catalog, want):
    """目录里挑线路：先全等，再唯一前缀，最后按短优先消歧。

    前缀候选 ≥ 3 个时不再直接放弃：11/12/13 路同时在的城市问「1」确实没法唯一定，
    但 B12/B13/B14 之外问「B1」应该命中「B1」本身，短的那个优先（见 issue #3）。
    catalog 元素是 (显示名, 原样对象)。
    """
    exact = [c for c in catalog if norm_line(c[0]) == want]
    if exact:
        return exact
    # 一个名字里写着两条线：连云港把昼夜两班写在一起「B1路/B1路夜」，郑州「888路/郑州-中牟」
    # 是两城联营。斜杠两边各自算全等，问「B1」要命中它，不能被下面那堆 B11/B12/B1K 顶掉
    seg = [c for c in catalog if want in _slash_parts(c[0])]
    if seg:
        return seg
    # 目录把号写在括号里（「大容山旅游专线（28路）」），归一化时括号连同号一起被剥掉，
    # 拿裸号 28 去比永远「没这条线」
    if want.isdigit():
        alias = [c for c in catalog
                 if want in re.findall(r"[（(]\s*(\d{1,4})\s*[路线]", c[0])]
        if alias:
            return alias
    # 括号里的别名号不一定是纯数字：深圳「云海专线B（T105B/定制巴士1893号）」
    alias = [c for c in catalog
             if any(re.search(rf"(?<![0-9A-Z]){re.escape(want)}(?![0-9A-Z])", x)
                    for x in re.findall(r"[（(]([^）)]*)[)）]", c[0]))]
    if alias:
        return alias
    near = [c for c in catalog if norm_line(c[0]).startswith(want)]
    if not near:
        # 「永嘉51路」「忻州201路」：城名/县名写在线名里，用户只写裸号，靠尾巴对上。
        # 取名字最短的那批，「永嘉51」不能被「永嘉151」顶掉（数字前面粘着数字的不算）
        near = [c for c in catalog if _tail_line(norm_line(c[0]), want)]
        if near:
            short = min(len(norm_line(c[0])) for c in near)
            near = [c for c in near if len(norm_line(c[0])) == short]
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


def _now(t=None):
    # 从 UTC 起算，不吃宿主机时区——本机是 CST、实例是 UTC，用 localtime 会差 8 小时
    return time.gmtime((time.time() if t is None else t) + TZ_OFF)


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


def _hours_note(w):
    """[(方向名, 首班, 末班)] → 「往岜仑路口 07:00–17:00、往龙江桥头 08:00–18:00」。

    没车时那句「线路还在跑」太空：「河池20路」只有一辆车，回场待发那阵两家都看不到它，
    只回「没车」会被读成查不到。带上两个方向的首末班，至少知道它什么时候有班。
    """
    return "、".join((f"往{lab} " if lab else "") + f"{f}–{l}"
                    for lab, f, l in w if f and l)


_seq_payload = mygolbs.seq_payload
_first_last = mygolbs.first_last
_price = mygolbs.price
_live_my = mygolbs.live


def _last_bus(city_name, line, direction):
    return _first_last(city_name, line, direction)[1]


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



# 同一个缓存 key 只能有一种形状——之前这里存 dict、别处存 buslines 列表，
# 谁先跑谁把对方写坏（render_at 就是这样崩的）。形状由 mygolbs.catalog 定
_catalog = mygolbs.catalog


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
        if not near:
            return None
        # 「沛县 1 路」只有 11/17/18 路是少打一位；「新郑2路」命中的 201/202 是另一批线，
        # 念成「是 201 吗」等于替用户改口
        if any(len(norm_line(n)) == len(want) + 1 for n in near):
            return {"error": "是 " + " / ".join(near) + " 吗"}
        return {"error": "没这条线路"}
    groups, live_known, first, last, live_flags, wins, pos = [], False, "", "", [], [], {}
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
        # 留着坐标：另一家报的车要按 GPS 配掉重复的，没有坐标就只能盲并（会多出同一辆车）
        for c in cars:
            if float(c.get("bus_lat") or 0) and float(c.get("bus_lng") or 0):
                pos[c.get("busNumber")] = {"lat": c["bus_lat"], "lng": c["bus_lng"]}
    note, unknown = "", False
    if not any(g for _, g in groups):
        if live_flags and all(live_flags):
            # hasReal=0 = 上游这座城根本不给车辆位置（北京、上海、成都等），
            # 不能当成「没车」报，那是假消息（实测玉林 0 车时 hasReal 仍是 1）
            # 但只回「不提供」等于什么都没答，把这条线能查到的（首末班、站数）补上
            ln = hit[0][1]["lineName"]
            # 「末班最晚 20:30」是上下行混在一起的一个数：过了末班还说「起、末班最晚」，
            # 等于没说清这两个方向都收车了。有这个就按方向报，并说明现在是不是出窗口了
            wl = _window_note(wins)
            # 过了末班/没到首班：为什么没车已经答清了，别再补一句「上游不给这座城实时」
            # ——昆明掌上整城不给车机，车来了照样有车，那句话读起来像这城根本没数据
            unknown = not wl
            note = yulin.no_real(ln) or ("" if wl else f"上游不给{norm_city(city_name)}的实时车辆位置")
            lname, ldir = hit[0][1]["lineName"], hit[0][1]["upperOrDown"]
            n = len(_station_seq(city_name, lname, ldir))
            bits = [f"{first} 起、末班最晚 {last}"] if first and last and not wl else []
            if n:
                bits.append(f"{n} 站")
            if bits:
                note += ("，这条线 " if note else "这条线 ") + "、".join(bits)
            if wl:
                note += ("，" if note else "") + wl
        elif (_wn := _window_note(wins)):
            note = _wn
        elif live_known:
            note = "线路还在跑，只是车机没报位置"
            # 「河池20路」这种定班线就一辆车在跑，车回场待发时两家同时看不到它，
            # 只回一句「没车」群友读成「查不到」。把两个方向的首末班带上，
            # 至少能自证这条线在、什么时候有班（实测 20 路 07:00–17:00 / 08:00–18:00）
            hours = _hours_note(wins)
            if hours:
                note += "（" + hours + "）"
    return {"source": "掌上公交", "line": hit[0][1]["lineName"], "groups": groups,
            "note": note, "unknown": unknown, "pos": pos}


def _city_of_chelaile(city_id):
    return next((k for k, v in cities().items() if (v or {}).get("chelaile") == city_id), "")


def _bare_line_exists(city_id, want):
    """这城的掌上目录里有没有跟 want 同名的那条线（裸号写法）。"""
    ck = _city_of_chelaile(city_id)
    my = (cities().get(ck) or {}).get("mygolbs") if ck else ""
    if not my:
        return False
    rows = _catalog(my).get("buslines") or []
    return any(norm_line(r.get("lineName") or "") == norm_line(want) for r in rows)


def _static_my(city_name, wants):
    """实时取不到时，只用目录（起讫/站数）回一份——总比「上游没响应」强。

    河池 8 路：有车有站表，只是刚才那一步超时；说清「这条线在、实时没取到」比换一条邻县的线答强。
    """
    if not city_name:
        return None
    rows = _catalog(city_name).get("buslines") or []
    for want in wants:
        hit = [r for r in rows if norm_line(r.get("lineName") or "") == norm_line(want)]
        if not hit:
            continue
        line = hit[0]["lineName"]
        groups = [(r.get("to") or "", []) for r in hit]
        note = ""
        first, last = "", ""
        try:
            first, last = _first_last(city_name, line, str(hit[0].get("upperOrDown") or "1"))
        except Exception:
            pass
        n = 0
        try:
            n = len(_station_seq(city_name, line, str(hit[0].get("upperOrDown") or "1")))
        except Exception:
            n = 0
        bits = [f"{first} 起、末班 {last}"] if first and last else []
        if n:
            bits.append(f"{n} 站")
        if bits:
            note = "、".join(bits)
        return {"source": "掌上公交", "line": line, "groups": groups, "note": note,
                "stale": True}
    return None


def _my_terminals(city_name, want):
    """掌上目录里这条线的终点站名（两个方向都算），用来跟车来了的「往X」对账。"""
    rows = (_catalog(city_name).get("buslines") or []) if city_name else []
    w = norm_line(want)
    out = set()
    for r in rows:
        if norm_line(r.get("lineName") or "") == w:
            for k in ("from", "to"):
                if r.get(k):
                    out.add(r[k])
    return out


def _same_terminals(tos, terms):
    """车来了那条线的终点跟掌上这条线的终点对得上吗（站名写法两家不完全一样，互相包含就算）。"""
    for t in tos:
        for m in terms:
            if t and m and (t in m or m in t):
                return True
    return False


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
    # 车来了把城里的县市写进线名（河池：「金城江8」「宜州8」），掌上却是裸号「8路」。
    # 名字对不上时不能直接丢：金城江8 就是 8 路本身（终点 御景龙庭/公共交通公司 一致），
    # 宜州8 才是别的线（终点 东门商场/新火车站）。靠终点站对账，下面拿到 to 再判。
    terms = set()
    if norm_line(name) != norm_line(want) and _bare_line_exists(city_id, want):
        ck = _city_of_chelaile(city_id)
        terms = _my_terminals((cities().get(ck) or {}).get("mygolbs") if ck else "", want)
        if terms:
            # 名字撞上的可能不止一条（河池 8：「宜州8」和「金城江8」），按终点站挑出同一趟车
            cands = [(n, l) for n, l in rows
                     if n == name or _tail_line(norm_line(n), norm_line(want))]
            picked = None
            for nm in dict.fromkeys(n for n, _ in cands):
                ids = [l for n, l in cands if n == nm]
                try:
                    tos = [_cached("live-cl", (city_id, i), LIVE_TTL,
                                   lambda x=i: chelaile.live(city_id, x)).get("to") for i in ids]
                except Exception:
                    continue
                if _same_terminals(tos, terms):
                    picked = nm
                    break
            if not picked:
                return None      # 终点都不对：那个县市自己的另一条线
            hit = [(n, l) for n, l in cands if n == picked]
            name = picked
            terms = set()        # 已经挑完，下面不用再判
    # 目录是方向级的：同一条线上下行是两个 lineId，全查再合并（见 issue #6）。
    # 香港一个「1」对应五家公司（九巴/城巴/屿巴/新界小巴/港岛小巴），名字里的括号
    # 就是公司，按名字分组查，别取前两条（前两条可能分属两家，混着报）。
    groups, desc, pos, states = [], "", {}, []
    hit_ids = {}
    for nm in dict.fromkeys(n for n, _ in hit):
        co = re.search(r"[（(](.+?)[)）]", nm)
        for line_id in [lid for n, lid in hit if n == nm]:
            d = _cached("live-cl", (city_id, line_id), LIVE_TTL, lambda i=line_id: chelaile.live(city_id, i))
            # state=-1 等待发车、-2 临时停运、-3 末班已过，desc 是现成中文文案
            desc = desc or d.get("desc") or ""
            to = d.get("to") or ""
            groups.append((f"{to}（{co.group(1)}）" if co and not op else to, d["buses"]))
            pos.update(d.get("pos") or {})
            states.append(d.get("state"))
            hit_ids[nm] = line_id
    # 多家公司同名时抬头别写成「1（九巴） 有 7 辆」，那不是一家的车
    if terms and not _same_terminals([lab for lab, _ in groups], terms):
        return None      # 终点对不上：这是那个县市自己的另一条线（宜州8），不是用户问的 8 路
    res = {"source": "车来了", "line": re.sub(r"[（(].*?[)）]", "", name)
           if len({n for n, _ in hit}) > 1 else name, "groups": groups, "desc": desc,
           "pos": pos}
    # state=-5 是「这城根本不给实时车辆」：北京全城都是 -5（1路/52路/300快内/快速公交1线
    # 一起验过），desc 只剩「首班…末班…」。当成「这会儿没车」报是假消息（BRT4 19:26 明明在跑），
    # 照实说上游不给车辆位置，把时刻表和站数补上
    if states and all(s == -2 for s in states) and not any(g for _, g in groups):
        # -2 = 临时停运/暂无车辆时间（河池金城江15 就是这样）：desc 原文「暂无车辆时间」
        # 直接甩给群友等于没说，先把状态说清，再看能不能补站数
        n = 0
        if hit_ids:
            try:
                n = len(chelaile.stations(city_id, next(iter(hit_ids.values()))))
            except Exception:
                n = 0
        res["paused"] = True
        res["note"] = ("车来了那边这条线标的是" + (f"「{desc}」" if desc else "「临时停运」")
                       + "（停运状态）" + (f"，这条线 {n} 站" if n else ""))
    if states and all(s == -5 for s in states) and not any(g for _, g in groups):
        res["unknown"] = True
        ck = next((k for k, v in cities().items() if (v or {}).get("chelaile") == city_id), "")
        city_label = norm_city(ck) if ck else "这城"
        # -5 有两种：北京是全城都不给（1路/52路/300快内/快速公交1线 一起验过都是 -5），
        # 昆明只有个别的线没接实时（C61 是 -5，同城 108 路照样有车）。分不清就说不清，
        # 会把「这条线没实时」说成「昆明不给实时车辆位置」——昆明 108 路明明有车
        city_wide = not any(
            (probe or {}).get("state") != -5 or (probe or {}).get("buses")
            for probe in (_probe_cl(city_id, lid) for lid in _other_ids(rows, hit)))
        # desc 是「首班 07:30 末班 18:00」：22 点问 C61 时先说是收班了，
        # 比「上游没给实时」更回答得了「为什么查不到」
        win = re.search(r"首班\s*(\d{1,2}:\d{2}).*?末班\s*(\d{1,2}:\d{2})", desc or "")
        wl = _window_note([("", win.group(1), win.group(2))]) if win else ""
        if wl:
            note = wl
            city_wide = False
        else:
            note = (f"上游不给{city_label}的实时车辆位置" if city_wide
                    else f"车来了这条线的实时车辆位置没给（{city_label}别的线有）")
        bits = [] if wl else ([desc] if desc else [])
        n = 0
        if hit_ids:
            try:
                n = len(chelaile.stations(city_id, next(iter(hit_ids.values()))))
            except Exception:
                n = 0
        if n:
            bits.append(f"{n} 站")
        if bits:
            note += ("，只有时刻表：" if desc and not wl else "，")
            note += "、".join(bits)
        res["note"] = note
    return res


def _other_ids(rows, hit, n=2):
    """同城另外几条线的 lineId：用来分辨「全城没实时」还是「这条线没实时」。"""
    taken = {lid for _, lid in hit}
    out = []
    for _, lid in rows:
        if lid not in taken and lid not in out:
            out.append(lid)
            if len(out) >= n:
                break
    return out


def _probe_cl(city_id, line_id):
    try:
        return _cached("live-cl", (city_id, line_id), LIVE_TTL,
                       lambda: chelaile.live(city_id, line_id))
    except Exception:
        return {}


# 掌上这座城不给车辆位置（hasReal=0：北京、成都）时，下次把车来了排前面，省一次空等
PREFER_CL = {}


def _veh(bus, veh):
    return veh in str((bus or ("",))[0])


# 「现在呢」「到哪了」这类纯追问：用户上一句问的是某一辆车时，接着问的还是那辆
_FOLLOW_VEH = re.compile(r"到哪|在哪|哪儿|走了|跑到|这辆|那辆|它|呢")


def _vehicle_talk(msg):
    """短句里在问「那辆车现在到哪」：没写线号，指的是上一句那条线上的某辆车。

    「现在513去哪里了」「现在呢」「那辆呢」都算；「现在几点了」「晚上吃点啥呢」不算——
    没有车号、也没有「这辆/那辆/它」指着某一辆，就是闲聊。
    """
    m = re.sub(r"[，,。.？?！!、~\s]", "", msg or "")
    if len(m) > 14 or re.search(r"路|站|车票|票价|几点|多久", m):
        return False
    if re.search(r"\d{2,5}", m):
        return bool(re.search(r"到哪|在哪|哪儿|去哪|哪了|走了|跑到|现在|呢", m))
    return bool(re.search(r"(?:这辆|那辆|它)(?:呢|到哪|在哪|哪了|走了|跑到)", m)
                or m in ("现在呢", "呢", "到哪了", "在哪"))


def _line_real(city_name, want):
    """目录里的真线名（「G02」→「G02路」）：掌上的实时接口只认带「路」的那个。"""
    for r in _catalog(city_name).get("buslines") or []:
        if norm_line(r.get("lineName") or "") == norm_line(want):
            return r["lineName"]
    return want


def _selfno_token(msg):
    """句子里那个像自编号的号：2~5 位纯数字，且不是线号（513路 / G02 / 通 3 号线）。"""
    for m in re.finditer(r"(?<![\dA-Za-z])(\d{2,5})(?![\dA-Za-z])", msg or ""):
        tok, nxt = m.group(1), msg[m.end():m.end() + 1]
        if nxt in ("路", "线", "号") or (m.start() and msg[m.start() - 1] in "号线路"):
            continue
        return tok
    return ""


def _veh_tokens(msg):
    """句子里所有像车号的东西（车牌、纯数字自编号、8A9E8AA8 这种机器号），按出现顺序。"""
    out = []
    for pm in PLATE_RE.finditer(msg or ""):
        out.append(pm.group(1))
    for m in re.finditer(r"(?<![\dA-Za-z])([0-9][0-9A-Za-z]{2,9})(?![\dA-Za-z])", msg or ""):
        tok, nxt = m.group(1), (msg or "")[m.end():m.end() + 1]
        after = (msg or "")[m.end():m.end() + 2]
        if nxt in ("路", "线"):
            continue                      # 513路 / G02线 是线号
        if nxt == "号" and after[1:2] != "车":
            continue                      # 397号线 是线号，「397号车」是车
        if _id_kind(tok):
            out.append(tok)
    return list(dict.fromkeys(out))


def _veh_from(res, msg, city_key="", city_name="", line=""):
    """句子里点的那辆车 → 实时清单里对应的那一辆。

    「现在513去哪里了」「513高新产业园那辆车」这种只夹个车号的说法以前匹不上，于是整条线的车
    全报出来——问一辆、回两辆。两家给的号格式各城不同（玉林掌上是车牌、车来了是 513 这种自编号，
    河池车来了是 000105，武汉两家都是 8A9E8AA8 这种机器号），所以：
    先按形状无关的规则直接匹，匹不上就按同一刻 GPS 把两家的号配一次。
    """
    ids = [str(b[0]) for _, bs in (res.get("groups") or []) for b in bs if b]
    if not ids or not msg:
        return ""
    toks = _veh_tokens(msg)
    for tok in toks:
        hit = [i for i in ids if _id_eq(i, tok)]
        if len(hit) == 1:
            return hit[0]
    if not toks or not city_name or not line:
        return ""
    # 号对不上：多半是拿另一家那套号问的（自编号 ↔ 车牌、或两家写法不同）。按 GPS 配一次
    try:
        mine_src = "chelaile" if str(res.get("source") or "").startswith(LABEL["chelaile"]) else "mygolbs"
        ids_by = _line_ids(city_key, city_name, _line_real(city_name, line))
        mate = _pair_ids(ids_by.get(mine_src) or {},
                         ids_by.get("chelaile" if mine_src == "mygolbs" else "mygolbs") or {})
    except Exception:
        return ""
    for a_id, (b_id, _m) in mate.items():
        for x, y in ((b_id, a_id), (a_id, b_id)):     # 号可能在任一家那一套里
            if any(_id_eq(x, t) for t in toks):
                hit = [i for i in ids if _id_eq(i, y)]
                if len(hit) == 1:
                    return hit[0]
    return ""


def _union(a, b):
    """两家报在路上的车不完全一样（6路实测 掌上 12、车来了 13，各自都有对方没报的车），
    只报一家就必然差一两辆——这就是「有时候漏车或者多车」的来源。
    按 GPS 一对一配掉重复的，再把另一家独有那几辆补进来。"""
    pa, pb = a.get("pos") or {}, b.get("pos") or {}
    lab_of = {str(z[0]): lab for lab, bs in a["groups"] for z in bs}
    pairs = sorted((m, x, y) for x, p in pa.items() for y, q in pb.items()
                   if (m := _dist_m(p, q)) is not None)
    mate, taken = {}, set()
    for m, x, y in pairs:
        # 同一辆车两家刷新节奏差几十秒，位置就差几百米（实测 53~902m）
        if m > 1000:
            break
        if x in mate or y in taken:
            continue
        mate[x] = y
        taken.add(y)
    extra = [(lab, z) for lab, bs in b["groups"] for z in bs
             if str(z[0]) in pb and str(z[0]) not in taken]
    if not extra:
        return a
    groups = [(lab, list(bs)) for lab, bs in a["groups"]]
    for lab, z in extra:
        q = pb[str(z[0])]
        # 归到离它最近的已报车那一组：同一条线的反方向隔几公里，距离分得开
        seat = min(((_dist_m(q, pa[x]), lab_of.get(x, lab)) for x in pa if x in lab_of),
                   default=(None, lab))[1]
        for i, (gl, bs) in enumerate(groups):
            if gl == seat:
                groups[i] = (gl, bs + [z])
                break
        else:
            groups.append((seat, [z]))
    return dict(a, groups=groups, source=f'{a["source"]}+{b["source"]}')


def _merge_blank(a, others):
    """两家都没车时把两家的说法合起来。

    河池 15 路：掌上「线路还在跑，只是车机没报位置」+ 车来了「暂无车辆时间」（临时停运）。
    只留先到的那家，等于把另一家的信息丢掉——哪家先挂全看当时网络。
    """
    notes = []
    for r in [a] + list(others):
        note = (r.get("note") or r.get("desc") or "").strip()
        if note and note not in notes:
            notes.append(note)
    # 一家说「临时停运」、另一家说「线路还在跑只是没报位置」，两句放一起就是自相矛盾。
    # 停运是更强的信号（车来了按发车计划判的），留它一句，别把"还在跑"也念出来
    if any("停运" in n for n in notes):
        notes = [n for n in notes if not re.search(r"线路还在(?:跑|运营)", n)] or notes
    out = dict(a)
    if notes:
        out["note"] = "；".join(notes)
    # 全城都不给实时才算 unknown：另一家只是这条线没数据时别栽给整城
    out["unknown"] = bool(a.get("unknown")) and all(r.get("unknown") for r in others)
    return out


def query(city_key, wants, src=None, veh=""):
    """两家依次试，都有车就按 GPS 合起来报（一家可能少报一两辆）。

    wants 是候选线路名，先试带前缀的「陆川1」，再试裸号「1」。
    src 是用户点名要的数据源（「从车来了看一下」），排它前面而已，不是只用它——
    点名的那家没有这条线时仍要能答上来。
    """
    ent = cities().get(city_key) or {}
    tried, blank, near, fallback, others, best, second = [], None, None, None, {}, None, None
    blanks = []
    order = ("chelaile", "mygolbs") if PREFER_CL.get(city_key) else ("mygolbs", "chelaile")
    if src:
        order = (src,) + tuple(s for s in order if s != src)
    for want in wants:
        for source in order:
            fetch = {"mygolbs": _mygolbs, "chelaile": _chelaile}[source]
            target = ent.get(source)
            if not target:
                continue
            # 掌上这座城整城不给车辆位置（hasReal=0，北京/成都），上一问已经知道了，
            # 现在两家都要问，跳过它省一整轮
            # 只跳过裸号：带汉字前缀的线名（新郑12、东胜8）只有掌上的目录认，车来了那边是裸号体系
            if source == "mygolbs" and PREFER_CL.get(city_key) and not re.search(r"[\u4e00-\u9fa5]", want):
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
                    # 另一家可能报得更多：先记下，问完再合，别拿一家的清单当全部。
                    # 点名了数据源就不合：「用车来了查」混进掌上的车牌，用户一眼看出串了
                    if best is None:
                        best = got
                    elif second is None and not src and got.get("source") != best.get("source"):
                        second = got
                    continue
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
                # 两家都没车时两家的说法都要留着：河池 15 路掌上一挂，
                # 就只剩车来了那句「暂无车辆时间」，首末班和站数全丢了
                if blank is None:
                    blank = got
                else:
                    blanks.append(got)
        if best:
            if second:
                break
        elif blank:
            return _merge_blank(blank, blanks) if blanks else blank   # 有这条线，只是当下两端都没车
    if not ent:
        # 城市表里没这个键：要么名字写错，要么两家都不覆盖，别把话说成「两家都没有」
        return {"error": "这个城市名没认出来，写全称或把城市补在路线前面试试"}
    if best:
        return _union(best, second) if second else best
    if fallback:
        # 这条线有车，只是没有问的那辆：两家查到的一起报，别让群友以为只查了一家
        return dict(fallback, veh_others=others)
    if near:
        return near
    if tried:
        # 两家都没答上来（多半是实时那一步超时）：目录里真有这条线就照实说「在，实时没取到」，
        # 别只回一句「上游没响应」，更别拿邻县的同号线顶（河池 8 路 ↔ 宜州8）
        st = _static_my(ent.get("mygolbs"), wants)
        if st:
            return st
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


def _renamed(city_name, term):
    """建档里记成「已停运」的老线号：同城目录里起讫对得上的是它现在的号（苏州 55 → 555）。"""
    ends = {e.strip() for e in (term or "").split("↔") if e.strip()}
    if len(ends) < 2:
        return ""
    for r in (_catalog(city_name).get("buslines") or []):
        if {r.get("from"), r.get("to")} == ends:
            return r.get("lineName") or ""
    return ""


def _variants(city_key, want):
    """「614」在佛山分成 614A/614B：同城目录里只有「号+字母」派生的，就是它俩。"""
    ent = cities().get(city_key) or {}
    rows = [r["lineName"] for r in (_catalog(ent.get("mygolbs")).get("buslines") or [])] \
        if ent.get("mygolbs") else []
    if ent.get("chelaile"):
        rows += [n for n, _ in chelaile.lines(ent["chelaile"])]
    pat = re.compile(re.escape(want) + r"[A-Za-z]$")
    return list(dict.fromkeys(n for n in rows if pat.fullmatch(norm_line(n))))[:4]


def _place_gap(city_key, want):
    """这城目录里一条带这个县区名的线都没有 → 这家数据源根本没覆盖那块。

    「昆山165路」：昆山属苏州，可苏州目录 1558 条线里没有一条昆山的（车来了 1916 条也没有）。
    只说「两家都没收这条」会被群友读成插件坏了；说清是哪块没覆盖才有用。
    """
    m = re.match(r"^([\u4e00-\u9fa5]{2,4})(?=\d|[A-Za-z])", want or "")
    if not m:
        return ""
    place = m.group(1)
    if norm_city(place) == norm_city(city_key):
        return ""
    # 得是这座城的县级市/区县：「武汉鄂101」里的「武汉鄂」不是地名
    owner = DIST_BARE.get(place) or DISTRICTS.get(place) or ""
    if not owner or norm_city(owner) != norm_city(city_key):
        return ""
    ent = cities().get(city_key) or {}
    rows = [r.get("lineName") or "" for r in (_catalog(ent["mygolbs"]).get("buslines") or [])] \
        if ent.get("mygolbs") else []
    if not rows and ent.get("chelaile"):
        rows = [n for n, _ in chelaile.lines(ent["chelaile"])]
    if any(place in n for n in rows):
        return ""        # 那个县市的线就挂在父城目录里（博罗233路）
    # 「东胜区8路」「南海区8路」：区名只是行政区，目录里就是裸号 8路——号在就说明这城管着它，
    # 不能因为线名里没写「东胜区」就说这块没覆盖
    num = (want or "")[m.end():]
    if any(_tail_line(norm_line(n), norm_line(num)) or norm_line(n) == norm_line(num) for n in rows):
        return ""
    return place


def render_ghost(city_key, city_name, want):
    """两家实时都没收这条线时，回落巴士派迪的建档目录。
    「安宁12」两家都没有，可这条线真实存在——回「没这条」就是把数据源缺口说成了事实错误。"""
    try:
        cid = buspedia.region_id(city_name or "")
        if not cid:
            return None
        # 「佛256」：单字城市简称粘在号前面。只剥这种单字简称——「安宁99」剥成「99」
        # 会匹到昆明市区的 99 路，那是另一条线，宁可照实说没查到
        cands = [want, want + "路"]
        ck = norm_city(city_key)
        if len(ck) > 1 and want.startswith(ck[:1]) and not want.startswith(ck) and len(want) > 1:
            cands += [want[1:], want[1:] + "路"]
        for nm in dict.fromkeys(cands):
            hit = buspedia.find_line(cid, nm) or buspedia.find_line_fuzzy(cid, nm)
            if hit:
                l = hit[0]
                # 建档里偶尔混进 markdown 强调（昆山165路 的 term 就是「*通医线*」）：
                # 群里看到星号只会以为是格式坏了
                term = (l.get("term") or [""])[0].replace("<>", " ↔ ").replace("*", "").strip()
                if not term:
                    continue
                if not l.get("active"):
                    alt = _renamed(city_name, term)
                    if alt:
                        return (f"{l['name']} 这个号已经不跑了，同一条线现在叫 {alt}（{term}）\n"
                                + render(city_key, norm_line(alt), query(city_key, [norm_line(alt)])))
                if _exact_line(city_name, want, want):
                    # 目录里明明有这条，只是刚才实时没报上来——不能说成「两家都没收」
                    return (f"{l['name']} 这城目录里有这条线，刚才两家实时都没报来车；建档资料是 {term}"
                            "\n（首末站查得到，实时的到站时间那两家刚才没给）")
                gap = _place_gap(city_key, want)
                if gap:
                    # 县市自己那套线路压根没进这城目录（昆山的线不挂在苏州目录下）
                    return (f"{l['name']} 掌上公交和车来了都没收{gap}的线路——{gap}这块两家都没覆盖，"
                            f"只有建档资料：{term}"
                            "\n（首末站查得到，实时的到站时间那两家没有）")
                return (f"{l['name']} 掌上公交和车来了都没收这条，建档资料是 {term}"
                        + ("" if l.get("active") else "（记的是已停运）")
                        + "\n（首末站查得到，实时的到站时间那两家没有）")
    except Exception:
        return None                      # 这站挂了就当没有，老文案照走
    return None


def render(city_key, want, res, veh="", dir="", shown=""):
    if "error" in res:
        e = res["error"]
        if e.startswith("是 "):                       # 目录里有近似线名，报候选
            # 建档里有这条号（「苏州55」→ 建档案记成已停运、现在叫 555）时先说这个，
            # 别只回「是 551 吗」——那等于把改号说成打错字
            ghost = render_ghost(city_key, (cities().get(city_key) or {}).get("mygolbs"), want)
            if ghost:
                return ghost
            return f"没「{want}」这条，是 {e[2:]}"
        if e.startswith("没这条"):
            # 「614」在佛山是 614A/614B 两条：别回「没这条」，也别只报相似线名
            vs = _variants(city_key, want)
            if vs:
                return (f"{want + '路' if want.isdigit() else want} 分成 {'、'.join(vs)} 两条：\n"
                        + "\n".join(render(city_key, norm_line(v), query(city_key, [norm_line(v)]))
                                    for v in vs))
            ghost = render_ghost(city_key, (cities().get(city_key) or {}).get("mygolbs"), want)
            if ghost:
                return ghost
            # 「河池606路」：目录里它叫「宜州606路（快班）」/「（普班）」——号是对的，只是多了县名和
            # 快慢班。回「没查到 606 这条线」是假话（明明有两条）。按号挑出来让用户选，
            # 只有一条就直接答
            tail = _number_tails(city_key, want)
            if len(tail) == 1:
                one = tail[0]
                return render(city_key, norm_line(one), query(city_key, [norm_line(one)]))
            if len(tail) > 1:
                shown = "、".join(tail)
                return (f"{city_key}这边 {want + '路' if want.isdigit() else want} 有 {len(tail)} 条："
                        f"{shown}。要哪条？带上快慢班或者直接写全名我就查")
            # 光说「没查到」，模型就会自己解释成「这个数据源没有这城市的线路」（泉州 K502 就是这样）
            # 县市那套线没进父城目录时先说这个：昆山的线一条都不在苏州目录里，
            # 列一堆苏州本地的 16路/65路 只会让人以为插件匹错了
            gap = _place_gap(city_key, want)
            if gap:
                return (f"没查到「{want}」这条线——{gap}的线路掌上公交和车来了都没覆盖，"
                        f"{city_key}目录里一条{gap}的线都没有，得用{gap}本地的公交 App")
            near = _near(city_key, want)
            # 线名自带地名（博罗233）时，近似线名也在那个地名下面（博罗1路…），
            # 说「惠州这边有的是」会被读成全惠州的线，写「博罗这边」才对得上
            head = re.match(r"[^\d]{2,4}", want)
            where = (head.group(0) if head and near and all(
                n.startswith(head.group(0)) for n in near.split("、")) else city_key)
            return f"没查到「{want}」这条线" + (f"，{where}这边有的是 {near}" if near else "")
        return e                                      # 其余文案本身已经是人话
    line = res["line"]
    # 目录把两条线写在一个名字里（连云港「B1路/B1路夜」）：用户问的是哪一段就写哪一段，
    # 抬头直接甩「B1路/B1路夜 现在 6 辆」像在读目录
    if "/" in line or "／" in line:
        parts = [p.strip() for p in re.split(r"[/／]", line) if p.strip()]
        shown = next((p for p in parts if norm_line(p) == norm_line(want)), "")
        line = shown or parts[0]
    # 车来了目录里的线名是裸号（湛江「9」），群里念出来像半句话：大陆的加个「路」，
    # 香港/澳门的线号本来就不带「路」（506、B8）
    if line.isdigit() and norm_city(city_key) not in ("香港", "澳门"):
        line += "路"
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
        # 「地铁快巴4」是微循环公交，不是地铁：只有真地铁（地铁1号/轨道交通2号）且上游
        # 整城不给车机（unknown=hasReal=0）才这么说，否则会把收班说成「不上报车机」
        if res.get("unknown") and re.match(r"^(?:地铁|轨道交通)\d+号", norm_line(line)):
            # note 里已经有首末班和站数，别丢掉只回一句「能查」；头一句跟这里重复，先剥掉
            n = re.sub(r"^[^，]*实时[^，]*，?", "", res.get("note") or "")
            return (f"{line} 没有实时车辆位置，地铁不上报车机"
                    + (f"；{n}" if n else "，站表和首末班能查"))
        tail = res.get("note") or res.get("desc") or ""
        # 抬头写用户问的那个号：「河池15路」别答成「金城江15」（目录名多带了个地名，
        # 群友会以为查的是另一条线）
        head = _ask_name(want, line)
        if res.get("stale"):
            # 目录里这条线在，只是实时那一步没取到：别说成「这会儿没车」，那是另一种意思
            return f"{head} 的实时车这会儿没取到，{tail}" if tail else f"{head} 的实时车这会儿没取到"
        head = f"{head} 查不到实时车" if res.get("unknown") else f"{head} 这会儿没车"
        return head + ("，" + tail if tail else "")
    multi = sum(1 for _, b in groups if b) > 1      # 只有一个方向有车时不念空方向
    many = len(groups) > 4                          # 支线网络，空方向属于噪音
    if many:                                        # 「25路(五大队观太)」不代表别的支线，抬头去掉括号
        line = re.sub(r"[（(].*?[)）]", "", line)
    rows = [f"有，{_ask_name(want, line)} 现在 {total} 辆在路上" if not veh
            else f"{shown or veh} 这辆在 {_ask_name(want, line)} 上："]
    for label, buses in groups:
        if not buses:
            if multi and label and not many:
                rows.append(f"往{label}还没车")
            continue
        rows.append((f"往{label}：" if label else "") + "，".join(_car(*b) for b in buses))
    return "\n".join(rows)


def _number_tails(city_key, want):
    """本城目录里号对得上、只是多了地名/快慢班前缀后缀的线名。

    「河池606路」→ 宜州606路（快班）、宜州606路（普班）；「河池603路」→ 宜州603路。
    号码要是一个完整的号（两侧不粘数字），后面最多跟一个「快/普」，别把 18 当成 8。
    """
    w = norm_line(want)
    if not w:
        return []
    name = (cities().get(city_key) or {}).get("mygolbs")
    rows = (_catalog(name).get("buslines") or []) if name else []
    out = []
    for r in rows:
        ln = r.get("lineName") or ""
        n = norm_line(ln)
        if not n or n == w:
            continue
        i = n.find(w)
        while i >= 0:
            before_ok = i == 0 or not (n[i - 1].isdigit() and w[:1].isdigit())
            rest = n[i + len(w):]
            if before_ok and rest in ("", "快", "普"):
                if ln not in out:
                    out.append(ln)
                break
            i = n.find(w, i + 1)
    return out


def _ask_name(want, line):
    """没车这句的抬头按用户问的号写。

    河池目录里叫「金城江15」，群友问的是「河池15路」——抬头直接写「金城江15」会让人以为
    查的是另一条线。目录名只是多带了地名前缀时，写成「15路（金城江15）」把两边的说法都留着；
    完全不同的叫法（BRT4 ↔ 快速公交4线）就照目录写，那是同一个号的不同写法。
    """
    if not want:
        return line
    wl, ln = norm_line(want), norm_line(line or "")
    if not wl or wl == ln:
        return line
    if ln.endswith(wl) and len(ln) > len(wl):
        # 只在地名前缀上括注（金城江15）；「双誉公交C61」那种公司前缀不加，
        # 那是同一条线的运营方，括起来只是噪音。按归一化后的名字算前缀长度，
        # 「金城江K2」减「K2」正好是「金城江」，别拿原串长度去截（「路」字会错位）
        # 线名自己已经带括号说明（「宜州606路（快班）」）时也别再套一层
        if "（" not in line and "(" not in line and _is_place(ln[: len(ln) - len(wl)]):
            # 「K2」这类纯字母数字的线号，群里念的时候都带「路」（K2路、M372路）
            shown = want + "路" if re.fullmatch(r"[A-Za-z0-9]+", want) else want
            return f"{shown}（{line}）"
    return line


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


# 「从1路东线」「增城105」：号上粘着汉字的名字。本城目录里没有时说明号是别的城市的线，
# 别回「没查到「1」」再赔一堆不相干的近似线名（佛山 614 之后又踩了从1路东线，它在广州从化）
_WNAME_BAD = "坐等查看有没有这那该此本是我你要去到在问乘搭今昨每晚几哪末首多怎还现间票时和与跟及"
# 号后面的定性词只认这几种，不然「1路末班车几点」也会被当成长名字
_WNAME_MOD = ("东线", "西线", "南线", "北线", "中线", "环线", "区间", "支线", "快线", "慢线",
              "夜班", "夜线", "长线", "短线", "专线", "副线", "高峰", "大站", "直达", "接驳")


def _written_name(msg, want):
    """句子里那个号的写法（「从1路东线」「增城105」），纯「1路」返回空串。"""
    if not want:
        return ""
    m = re.search(r"(?<!\d)([\u4e00-\u9fa5]{1,4})?\s*" + re.escape(want)
                  + r"(?:路|线)?(?!\d)([\u4e00-\u9fa5]{0,4})", msg)
    if not m:
        return ""
    pre, suf = m.group(1) or "", m.group(2) or ""
    name = m.group(0).strip()
    if pre and pre[0] in _WNAME_BAD:
        name, pre = name[len(pre):], ""
    if suf and not any(suf.startswith(x) for x in _WNAME_MOD):
        name, suf = name[:len(name) - len(suf)], ""
    return name.strip() if (pre or suf) else ""


HOP_RE = re.compile(r"从?\s*([^，,。？?！!\s]{2,12}?)\s*"
                    # 「看到/说到/想到/遇到/查不到」都是动词的「到」，不是「A 站到 B 站」——闲聊被当成换乘问过
                    r"(?<![看说想做达遇收得感觉听找等拿买卖学记念料猜想跑走来回送带拉请叫问答碰见闻触意识考虑注意查不])到\s*"
                    r"([^，,。？?！!\s]{2,12})")
# 「A 咋去 B」「A 怎么去 B」：没「到」字，靠疑问词认；只有「去」一个动词的说法太松，不认
HOP2_RE = re.compile(r"(?:从\s*)?([^，,。？?！!\s]{2,12}?)\s*"
                     r"(?:咋|怎么|怎样|如何|咋样)\s*去\s*([^，,。？?！!\s]{2,12})")
# 「A 去 B 咋去」：疑问词跑到最后，HOP2 只认「A 咋去 B」这一种语序
HOP3_RE = re.compile(r"(?:从\s*)?([^，,。？?！!\s]{2,12}?)\s*去\s*"
                     r"([^，,。？?！!\s]{2,12}?)\s*(?:咋|怎么|怎样|如何)\s*(?:去|走|坐|乘)")
# 「火车站坐什么车」要把尾巴砍掉再当站名查；「我在十字街」要把脑袋砍掉
TAIL = re.compile(r"(现在|目前|当前|[坐做]哪[个条路]?|哪[辆部台]|来车|[坐做]啥|怎么坐|坐什么|怎么去|有(?:几|多少)条?(?:公交车|公交|巴士|公车)?(?:线路|线)|哪几条线路|有什么|有哪些|有哪几|多少|几路车|坐几路|怎么走|咋走|咋坐|咋去|换乘|有直达|直达|路线|的?(?:公交)?线路|的话|附近|周边|周围|(?:还)?要(?:等|坐|乘|多久|几[分站台])|还有|有没有|在?哪[里儿]|什么时候|几点到|多久到|还有多久|要多久|多久|最近|吗|呢|啊|等).*$")
# 问的是「到这一站还要几分钟」：站名没认出来时宁可反问，也别退化成整车清单
TIMEQ_RE = re.compile(r"等多久|等几分钟|几分钟|还有多久|多久到|还差多久|等几站|几分钟到")
# 「多久一班」问的是发车间隔，不是到站钟点
INTERVAL_RE = re.compile(r"多久一[班趟台]|间隔|多久一趟")
# 站牌上不写「公交站」，用户嘴里常带：「丰州公交站」其实是「丰州」
STOPSUF = re.compile(r"(公交车站|公交站|车站|站台)$")
# 「G02-513 现在在哪」「513 号车」「现在 512 车到哪」：问的是一辆具体的车，不是整条线
# 「11 路车」中间隔着「路」，不会被当成车号
VEH_RE = re.compile(r"-\s*(\d{2,5})(?!\d)|(?<![\dA-Za-z])(\d{2,5})\s*[号]?车")
# 「G02的512到汽车总站要等多久」：问的是这条线上自编号 512 那一辆，不是线号 512
VEH2_RE = re.compile(r"的\s*(\d{2,5})(?![\d号线路])")
# 完整车牌：省简称 + 字母 + 5 位（普通）或 6 位（新能源，闽A17997D）
PLATE_RE = re.compile("(" + PLATE_PAT + ")")
# 「桂K06833D 的自编号是啥」：车牌谁都有，问的是内部车号——两家都没有对照关系
# 「哪个字编号」「编号多少」也算：群里打错字少个「自」很常见，只有这一截对得上
SELFNO_RE = re.compile(r"自编号|自编|车号|车辆编号|内部编号|编号")
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
LEAD = re.compile(r"^(我?在|我?从|去|到|等|坐|往)")
# 只剥带数据源的写法：「查桥站」这种真站名不能被「查」吃掉
SRCPRE = re.compile(r"^(?:从|用)?(?:车来了|掌上公交|掌上|巴士派迪|派迪)\s*[，,、:：]?\s*"
                    r"(?:查一下|查询|查查|看一下|看看|查|看|搜一下|搜|问一下)?\s*[，,、:：]?\s*")


stops = mygolbs.stops


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
STOP_ALIAS = {"公交公司": "公共汽车公司",
              # 嘴里说「玉林汽车站」，站牌和目录里叫「汽车总站」（shtx 搜「汽车站」一个字都搜不到）
              "汽车站": "汽车总站",
              # 「玉高附中」是玉林高中初中部的口语叫法，站牌上写全称，搜「玉高」0 条
              "玉高附中": "玉林高中初中部"}


def find_stop(city_name, raw):
    """站名从长到短试，命中即止——用户嘴里不会带括号后缀，得让平台的站名去对。

    底线是 2 字（「丰州」这种站牌本名就这么短），但整段跟城市名同字的首字
    会被下面那句挡掉，所以「南宁机场」不会退成「南宁」搜出「南宁孔庙」。
    """
    # 口语叫法先换成站牌上的写法再搜（「玉高附中」→「玉林高中初中部」）；换完搜不到就按原样试
    alias = next((raw.replace(k, v) for k, v in STOP_ALIAS.items() if k in raw), None)
    if alias:
        hit = find_stop(city_name, alias)
        if hit[0]:
            return hit
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
            # 「北京密101路」剥掉线号只剩个「密」，上游子串搜会匹上「北京密码小区」
            if len(tail) < 2:
                continue
            # 「新郑市火车站」退到「新郑市」会匹上「新郑市二中」：县名本身不是站名
            if raw[n:] and norm_city(raw[:n]) in DIST_BARE:
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
            if best and len(core) >= 2 and difflib.SequenceMatcher(None, raw, best["stationName"]).ratio() >= 0.6:
                return _merge_same(rows, best), best["stationName"]
    # 「玉林八中」平台写作「玉州区第八中学」：学校简称（八中/一小）按口语搜一次，
    # 全城只有这一个才认，免得「南宁机场」这种整词被抹掉城市名后匹到不相干的站
    core = raw.replace(norm_city(city_name), "", 1)
    if len(core) >= 2 and core[-1] in "中小":
        rows = [r for r in stops(city_name, core) or [] if r.get("stationName")]
        names = {r["stationName"] for r in rows}
        if len(names) == 1 and core in next(iter(names)):
            return _merge_same(rows, rows[0]), core
    # 「玉林职院有没有公交」：站表只收「玉林职业技术学院」，群里的简称一个字都不差地对不上，
    # 补回全称再搜一次，全城只有一个才认
    for ab, full in (("职院", "职业技术学院"), ("职校", "职业技术学校"),
                     ("卫校", "卫生学校"), ("师院", "师范学院")):
        if core.endswith(ab):
            # 简称前头没剩字（「职院」整段就是简称）就拿城市名补上，否则搜出来的全城同名站不止一个
            head = core[:-len(ab)] or norm_city(city_name)
            rows = [r for r in stops(city_name, head + full) or [] if r.get("stationName")]
            names = {r["stationName"] for r in rows}
            if len(names) == 1:
                # 返回站表里的真名（「玉林职业技术学院」），不是用户写的简称——
                # 上层拿它去对站序，写「职院」一个站都对不上
                return _merge_same(rows, rows[0]), next(iter(names))
            break
    # 「税局」这种两字简称：上游的站名搜索要三个字以上才肯给结果（「税局」搜出 0 条，
    # 「税务局」才出「市税务局」），退到单字再按顺序匹一遍；站名比简称长出三字以上就不认，
    # 免得「医院」匹到「玉林市第一人民医院」
    # 「车站」「学校」这种整词都是通用字的，匹到哪个站都是瞎猜
    if len(core) == 2 and not re.fullmatch(r"[公交汽车火车车站台口路]+|学校", core):
        rows = [r for r in stops(city_name, core[:1]) or []
                if r.get("stationName") and len(r["stationName"]) <= 4
                and _subseq(core, r["stationName"])]
        names = {r["stationName"] for r in rows}
        if len(names) == 1:
            # 返站牌真名（「市税务局」），不是用户的写法「税局」——上层拿它去对线路站序
            return _merge_same(rows, _bus_row(rows)), next(iter(names))
    return None, raw


def hop(msg):
    """→ (出发站, 到达站)。只认「从X到Y」和「X咋去Y」，别的句式不猜。"""
    m = HOP_RE.search(msg)
    if m and LINE_RE.search(m.group(1)):     # 「坐1路到火车站」左边是线路号，不是站
        m = None
    if not m:
        # 「玉林火车站咋去北流万达」：没「到」字，靠疑问词+「去」认；光一个「去」太松，不认
        m = HOP2_RE.search(msg) or HOP3_RE.search(msg)
        if not m or LINE_RE.search(m.group(1)):
            return None
    a = LEAD.sub("", m.group(1).lstrip("从"))   # 「我在十字街咋去火车站」
    # 「G02 有多少辆车，以及到哪里了」：HOP_RE 按「到」切，「以及」被当成出发站，
    # 答出一句「没查到从「以及」到「」的换乘方案」——连同下面的「然后/顺便」一起挡掉
    if a.strip(" 　") in ("以及", "然后", "顺便", "还有", "并且", "同时", "接着", "再看", "再", "和", "跟", "它", "他"):
        return None
    # 「从市体育中心坐到玉林北站」：HOP_RE 按「到」切，动词「坐」粘在出发站后面
    a = re.sub(r"[坐走骑乘搭]$", "", a)
    # 「石家庄从谈固到…」：城名和「从」一起粘在出发站前面，剥掉；只认这个形状，
    # 「广州公园前」里的城名是站名的一部分（要不要跨城，交给后面认城市）
    for ck in cities():
        if a.startswith(ck + "从"):
            a = a[len(ck) + 1:]
            break
    return a, TAIL.sub("", m.group(2)).rstrip("的了")


def _generic_word(text):
    """只有称呼、没有具体站名/地名：「公交」「巴士」「公交车」不是站，别拿去搜站。

    玉林站表里真有个站叫「公交二车队」，拿「公交」去模糊匹就把这条线列出来了——
    MisakaNo 在群里回了一句「公交？」，插件答「公交二车队 车来了这边有 1 条线路：…」。
    """
    t = re.sub(r"[，,。.？?！!、~\s啊呢吧吗的了]", "", text or "")
    # 一层层剥：「公交车」「公交线路」剥一次还剩「车」/「线路」，得剥到不再变
    while True:
        r = re.sub(r"^(?:公交|巴士|地铁|轨道交通|客运|线路|车|站|台|公司|线路)", "", t)
        if r == t:
            break
        t = r
    return not t


def _cands(city_name, msg):
    """从整句里抠出可能的站名：按标点切片，逐片砍掉「我在/坐什么」。"""
    city = norm_city(city_name)
    for part in re.split(r"[，,。.？?！!、\s]+", msg):
        cand = SRCPRE.sub("", part)      # 「车来了查X」里的数据源和动词
        if LINE_RE.search(cand):
            # 「G02到高新产业园」「11路还有多久到双溪口」：线路号后面那个才是站，先切再砍
            # 「g02离玉林北站最近的公车要多久」也得认，离 也当前后景分隔
            # shortcut: 站名本身带「离」（山西离石）会被切坏，遇到了再收窄
            seps = re.split(r"到|去|往|在|经停|离", cand)
            # 「G02在玉林汽车总站要等多久才能等到」：号在前、站夹在中间，尾字「到」把最后一片切空
            cut = seps[-1] or (seps[-2] if len(seps) > 1 else "")
            if not re.sub(r"[A-Za-z]?\d{1,4}\s*[路线]", "", cut).strip():
                # 「在市政广场要等多久等到17路」：站名在线号前面，中间还夹着问法，
                # 光按「到」切会把站名切没——从号前面那截取，先把问法剥掉
                cut = ASKPAST.sub("", re.sub(r"[A-Za-z]?\d{1,4}\s*[路线].*$", "", cand))
            cand = cut
        # 站名在线路号前面（「我在玉林火车站等9路」「玉林火车站9路还有几站」）时号后面没站名，
        # 得把号本身刮掉再当站名试
        # shortcut: 不认站名里带阿拉伯数字的（「3号门」），遇到了再收窄
        cand = re.sub(r"[A-Za-z]?\d{1,4}\s*[路线]", "", cand)
        # 「玉林九中的3路等多久」：「的」粘在站名尾巴上，站表里搜「玉林九中的」一个字都没有
        cand = LEAD.sub("", TAIL.sub("", cand)).rstrip("的")
        # 「查询玉林职院附近有没有公交车」：动词粘在站名前头，「职院」就补不成全称了
        cand0 = cand                    # 剥动词之前的原样，剥完对不上时还得退回它（「那洪立交」）
        cand = JUNK_HEAD.sub("", cand) or cand
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
        for c in dict.fromkeys(alias + [cand, cand0, whole] + bare):
            # 「泉州公交 惠南工业区四期站」：切出来的「泉州公交」只剩城市名，拿去搜站会匹到
            # 「中化泉州公司」这种同名不同地的站
            core = re.sub(r"(公交)?(车?站|台)?$", "", c) or c
            if len(c) >= 2 and norm_city(c) != city and norm_city(core) != city \
                    and not LINE_RE.search(c) and not _generic_word(c):
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
            # 短真线名（夜1 / 微2 / 青金线 / 619）照抄也会被问，别只认四字以上。
            # 数字两侧要卡边界，否则「13路」里的「3路」会把整句顶掉；
            # 前面粘着字母的不算（「AirPods5」「pro3」里的数字不是线号，
            # 「北京BRT4」里的 T4 也不是——那是通州的 T4，北京 BRT4 是「快速公交4线」）
            pre = r"(?<![0-9A-Za-z])" if n[:1].isalnum() else r"(?<![0-9])"
            if (len(n) >= 3 or re.search(r"\d", n)) and re.search(
                    rf"{pre}{re.escape(n)}(?![0-9])", msg):
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
    # 「玉林汽车总站等多久才能等到9路」：「等到9路」的「9路」是线路号，不是目的地
    if LINE_RE.search(m.group(1)):
        return None
    # 「往呈贡广场方向什么时候能到」：方向后面跟的是「方向」和问法，砍掉再搜
    raw = re.sub(r"方向.*$", "", re.split(r"要|等", m.group(1))[0])
    got, _ = find_stop(city_name, TAIL.sub("", raw))
    return (got or {}).get("stationName")


def _in_line(flat, n):
    """n 出现在 flat 里、且两侧不粘着别的数字（「13」里的「3」不算「3路」）。"""
    i = flat.find(n)
    while i >= 0:
        if (i == 0 or not flat[i - 1].isdigit()) and \
                (i + len(n) >= len(flat) or not flat[i + len(n)].isdigit()) and \
                not (i and n[:1].isdigit() and flat[i - 1].isascii() and flat[i - 1].isalpha()):
            # ↑「AirPods5」「pro3」：英文单词尾巴上的数字不是线号
            return True
        i = flat.find(n, i + 1)
    return False


_METRO_PFX = re.compile(r"^(?:地铁快线|地铁|轨道交通|城市轨道|公交|BRT)")


def _digit_pat(s):
    """线名转正则：阿拉伯数字和中文数字等价认（「新乐专一路」的「一」就是 1）。"""
    cns = "零一二三四五六七八九"
    # 「②」isdecimal 也算数字，可 int() 认不了——目录里真有这种线名，会整句崩掉
    return "".join(f"[{d}{cns[int(d)]}]" if d.isascii() and d.isdigit() else re.escape(d)
                   for d in s)


def _exact_line(city_name, msg, want, city_key=""):
    """目录里有更长的真线名整段出现在句子里（「新乐专1路」）就用它，别只拿裸号去匹。"""
    flat, w = _digitize(norm_line(msg)), norm_line(want)
    w2 = _digitize(w)          # 「地铁一号」在目录里叫「地铁1号线」

    def scan(names):
        hit = []
        for nm in names:
            # 目录里「张堰一路」是中文数字，用户写「张堰1路」：两边都折成阿拉伯再比
            n = _digitize(norm_line(nm))
            # 「pro3」里的 3 不是 3 路：数字打头的线名，前面不许粘着英文字母
            def _ok(m):
                i = m.start()
                return not (i and n[:1].isdigit() and flat[i - 1].isascii() and flat[i - 1].isalpha())
            # 句子里把城市名也粘进了线名（「西安环山二号线」）：目录名是它的后缀也算。
            # 只认 head 就是这座城的名字——「安宁12」的「安宁」是线名本身（安宁K12），
            # 当成城市名剥掉会把昆明市区的 12 路答成安宁的车
            tail = w2 if n.endswith(w2) else w
            head = tail[:len(tail) - len(n)] if tail.endswith(n) and len(tail) > len(n) else ""
            # 「58路大站快车」「301路A」「613路支」：号在前、尾巴是定性词。整段出现在句子里才算，
            # 光看前缀会把「9路」当成「9路大站快车」
            front = n.startswith(w2) and len(n) > len(w2)
            # 「威远3路副线」「云和1路上城门」：比裸号长的真线名整段出现在句子里。
            # 数字两侧卡边界：「玉林99路」不能被目录里的 9 路顶掉
            whole = len(n) > len(w2) and _in_line(flat, n)
            # 「上海16号线」：目录里叫「地铁16号线」，城市名占了「地铁」的位子。
            # 剥完得是完整的一个号：「武汉鄂101路」里的「1」不是「BRT1路」的号
            bare = _METRO_PFX.sub("", n)
            if bare != n and (bare == flat or _in_line(flat, bare)):
                whole = True
            # 「（3路支路）」这种同名不同支的，靠下面按原样名择优
            if not (n == flat or whole or n.endswith(tail) or front
                    or (head and norm_city(head) == norm_city(city_name))):
                continue
            # 「新乐专一路」的「一」也是数字，得和 1 等价地认
            # 边界必须卡：`n in flat` 那种裸包含会让「16」匹进「昆山165」，
            # 于是「昆山165路」被答成「16路 这城目录里有这条线」——纯属胡认
            if _in_line(flat, n) or any(_ok(m) for m in re.finditer(_digit_pat(n), flat)) \
                    or (bare != n and _in_line(flat, bare)):
                hit.append(nm)
        return hit

    hit = scan([r["lineName"] for r in _catalog(city_name).get("buslines") or [] if r.get("lineName")])
    best = max(hit, key=lambda n: (n in (msg or ""), len(norm_line(n))), default=None)
    # 掌上没这条、或只匹到裸号（「吴川二路」）：真线名可能只在车来了目录里（吴川2路）
    cid = (cities().get(city_key or norm_city(city_name)) or {}).get("chelaile")
    if cid and (not best or norm_line(best) == w):
        try:
            # 只收比裸号更长的：「吴川9路」不能拿湛江市区的 9 路顶（上游没这条就说没这条）
            hit += [n for n in scan([n for n, _ in chelaile.lines(cid) or []])
                    if len(norm_line(n)) > len(w)]
        except Exception:
            pass
        best = max(hit, key=lambda n: (n in (msg or ""), len(norm_line(n))), default=None)
    return best


_station_seq = mygolbs.stations


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


def _dir_end(city_name, line, d):
    """目录里这个方向的 (起点, 终点)。"""
    for r in _catalog(city_name).get("buslines") or []:
        if r.get("lineName") == line and str(r.get("upperOrDown")) == str(d):
            return r.get("from") or "", r.get("to") or ""
    return "", ""


def _seq_has(city_name, line, d, stop):
    """这条线这个方向上有没有这一站：同名、包含、括号别名都算一个站。"""
    for s in _station_seq(city_name, line, d):
        n = s.get("stationName") or ""
        if n == stop or _alias(stop) & _alias(n) or (len(n) >= 2 and (n in stop or stop in n)):
            return True
    return False


def _oneway_note(city_name, at, line, d):
    """这站只停一个方向时必须说明：用户看不出「回程不经过这站」，会一直等。
    111 和直达都按方向排，反方向到底停不停这站只有 103 站序知道。"""
    oth = "2" if str(d) == "1" else "1"
    if not _seq_has(city_name, line, d, at) or _seq_has(city_name, line, oth, at):
        return ""
    f, t = _dir_end(city_name, line, d)
    return (f"{line} 在{at}只有往{t}方向停（{f} → {t}），反方向不经过这站" if f and t
            else f"{line} 在{at}只有单向停靠")


def _oneway_lines(city_name, at, lines):
    """这站在这些线上只停一个方向的 → {线: 方向}。
    CMD=115 的 ALL=1 实测不可用（9路两个方向都停却只报一个），只能拿 103 逐方向比对；
    八线程并发，冷站一次一两秒，之后走 103 的一小时缓存。"""
    def one(ln):
        ds = [d for d in ("1", "2") if _seq_has(city_name, ln, d, at)]
        return ln, (ds[0] if len(ds) == 1 else "")
    if not lines:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return {ln: d for ln, d in ex.map(one, lines) if d}


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


_ask111 = mygolbs.transfer


def render_plan(city_name, a, b):
    """掌上自己的换乘方案（CMD=111）：两次换乘也排、总时长和步行距离都算好还排了序。
    我们手算的只换一次、按「换得最早」挑，经常不是最快的那个。
    shortcut: 只取前 2 个方案，群消息再长没人看；坐标留空，按站名查就够
    """
    info = _ask111(city_name, a, b)
    ra = (find_stop(city_name, a)[0] or {}).get("stationName")
    if not info:
        rb = (find_stop(city_name, b)[0] or {}).get("stationName")
        if ra or rb:
            info = _ask111(city_name, ra or a, rb or b)
    rows = []
    first_legs = []
    for k, p in enumerate(info[:2]):
        legs = [lg for key in ("routeUpDownSimple1", "routeUpDownSimple2", "routeUpDownSimple3")
                for lg in (p.get(key) or []) if lg.get("routeName")]
        if k == 0:
            first_legs = legs
        if k == 0 and len(legs) < 2 and (legs[0].get("upStationName") or "") in (a, ra):
            return ""       # 能直达就让 render_hop 答——它带车在哪、首末班，这里排不出这些
        # 111 只数站数，会排「坐 1~2 站再换」的凑数方案：电脑城反向坐 2 站福绵线再换城际，
        # 玉林本地没人这么干。最快的那个方案（k=0）无论多短都留着。
        if k and any(1 <= abs((lg.get("downStationIndex") or 0) - (lg.get("upStationIndex") or 0)) <= 2
                     for lg in legs):
            continue
        ride = ""
        for leg in legs:
            nm = leg["routeName"]
            if nm in ride:
                continue
            up = leg.get("upStationName") or ""
            # 线名是「玉林总站-福绵新滔工业园」这种区间写法时，念出来像「要坐去福绵的车」——
            # 补上只坐几站，才看得出它只是短驳（方案2 是反向坐 2 站到换乘站）
            n = abs((leg.get("downStationIndex") or 0) - (leg.get("upStationIndex") or 0))
            seg = f"{nm}（{n}站）" if n else nm
            # 第一条腿不从用户说的站发车（上游换成了附近站），得说清去哪上车
            ride += (f"，到{up}换 {seg}" if ride else
                     f"到{up}坐 {seg}" if up and up not in (a, ra) else f"坐 {seg}")
        if not ride:
            continue
        bits = [x for x in (p.get("stationNum"),
                            p.get("totalTime") and f"约{p['totalTime']}",
                            p.get("totalWalkDistance") and f"走路{p['totalWalkDistance']}") if x]
        rows.append(ride + (f"，{'、'.join(bits)}" if bits else ""))
    # 换乘那条线若从中途站上，111 永远不给「先去首站」的方案（它只按总时长排），
    # 但很多人就是要座位、或者行李多——补一句怎么先到首站，别让用户自己问
    note = ""
    if first_legs and (first_legs[-1].get("upStationIndex") or 1) > 1:
        ln = first_legs[-1]["routeName"]
        seq = mygolbs.stations(city_name, ln, "1") or []
        head = (seq[0].get("stationName") or "") if seq else ""
        if head and head not in (a, ra, b):
            way = render_hop(norm_city(city_name), city_name, (a, head)) or ""
            picks = re.findall(r"^(.+?)（(\d+) 站）", way, re.M)[:2]
            if picks:
                note = (f"{head}是 {ln} 的首发站，想坐着去："
                        + "、".join(f"{x} {n}站" for x, n in picks) + f" 到{head}再上车")
    # 上车站只有单向停靠的也要说：换乘方案本身有方向，但人容易以为反着走同样换得回来
    ow = [n for lg in first_legs
          if (n := _oneway_note(city_name, lg.get("upStationName") or "",
                                lg["routeName"], lg.get("uod") or "1"))]
    tail = "".join(("\n" + x) for x in ([note] if note else []) + ow)
    if not rows:
        return ""
    if len(rows) == 1:
        return f"从{a}到{b}：{rows[0]}{tail}"
    return f"从{a}到{b}：\n{rows[0]}\n或者{rows[1]}{tail}"


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
    # 上车站是单边站时得说清：这几条只往这个方向停，回程不经过这站
    ow = [n for line, got in direct
          if (n := _oneway_note(city_name, a["stationName"], line, got[0]))]
    tail = f"\n{'；'.join(ow)}" if ow else ""
    if len(direct) == 1:
        line, got = direct[0]
        return f"{head}坐 {line} 直达（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got) + tail
    rows = [f"{head}能直达："]
    for line, got in direct:
        rows.append(f"{line}（{got[2] - got[1]} 站）" + _hop_line(city_name, line, *got))
    return "\n".join(rows) + tail


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


def render_at(city_key, city_name, want, msg, veh="", tally=None, at=""):
    """「我在 X 站，N 路还有几站」：按方向给最近一辆，分钟用上游对该站的预测。"""
    station_name = at or pick_stop(city_name, msg)
    # 「安宁区8路」「东胜区 8路 夜班车」：线号前面那个词是行政区名，不是站名。
    # 拿它去匹会匹到「安宁区医院」「东胜区党校」，然后答一句「8路 不经过…」——用户问的是车
    if re.split(r"\d", msg.strip(), 1)[0].strip().endswith(("区", "县")):
        return None
    # 「惠州博罗233路」：博罗是县名，可它不带区/县后缀（上和上面那条查的同一个坑）。
    # 拿「博罗」搜站会命中「博罗天虹」，然后答「233路 不经过博罗天虹」——用户问的是车
    if _place_head(msg):
        return None
    if not station_name:
        return None
    # 「G02 到高新产业园，去玉林北站」问的是在产业园上车、往玉林北站那个方向，别把终点当上车站
    dest = _dest_after(city_name, msg, station_name)
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    # 「G02的512」：掌上按车牌报车来了按自编号报，先按同一刻 GPS 把 512 对到车牌。
    # 对不上就照旧报最近一辆（不猜是哪辆，免得把别人的车说成他的）
    mine, nos = set(), []
    if veh and hit:
        try:
            mate = _selfno_mate(city_key, city_name, hit[0][1]["lineName"])[0]
            mine = {k for k, (no, _) in mate.items() if veh in str(no)}
            nos = sorted(no for no, _ in mate.values())
        except Exception:
            mine = set()
        if nos and not mine:
            return f"{hit[0][1]['lineName']} 这会儿没有 {veh} 这辆，在跑的自编号：{'、'.join(nos)}"
    got, dead, have, vmiss = {}, [], [], []      # 方向名 -> (分钟, 车牌, 站数)；没预测的进 dead
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
        have.append(d)
        # 「G02 到高新产业园，去玉林北站」问的是在产业园上车、往玉林北站那个方向，别把终点当上车站
        if dest and dest != station_name:
            g = _goes(city_name, line_name, station_name, dest)   # 只留能开到 dest 的方向
            if not g or g[0] != d:
                continue
        lab = row.get("to") or ""
        pay = _live_my(city_name, line_name, d, idx)
        eta = _etas(pay)
        if mine:
            own = [r for r in pay.get("routeOnStationRTimeInfoList") or []
                   if _plate_key(r.get("busNumber")) in mine
                   and int(r.get("busToStationCount") or -1) >= 0]
            if own:
                b = min(own, key=lambda r: float(r.get("busToStationTime") or 0) or 1e9)
                eta = (float(b.get("busToStationTime") or 0),
                       int(b.get("busToStationCount") or 0), f"{veh} {b['busNumber']}")
            else:
                # 掌上只给最近两辆报这站的预测，再往后的车按它现在停的那站往后累。
                # 站名两家写法不一样（「德兴花园」↔「德兴花园（南）」），别只认全等
                car = next((c for c in pay.get("list") or []
                            if _plate_key(c.get("busNumber")) in mine), None)
                at = (car or {}).get("stationName") or ""
                here = next((i for i, n in enumerate(names)
                             if n and (n == at or n in at or (len(n) >= 2 and at and n in at))), None)
                if car and here is not None and here < idx - 1:
                    eta = (_run(pay, here, idx - 1), idx - 1 - here,
                           f"{veh} {car['busNumber']}（现在{at}）")
                else:
                    eta = None
                    vmiss.append((lab, car, here, idx))
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
    if vmiss and len(vmiss) == len(have):
        # 问的是某一辆：它在路上但掌上没给这站预测（刚过站 / 车机没上线）。
        # 说清它在哪，别退成「这站没车报过来」——那是站着等车的人读的
        lab, car, here, idx = vmiss[0]
        if car and here is not None and here >= idx - 1:
            return f"{veh}（{car['busNumber']}）现在{car.get('stationName')}，已经过了{station_name}"
        return (f"{veh}（{car['busNumber'] if car else '车牌没报'}）"
                f"现在{car.get('stationName') if car else '没报位置'}；"
                f"到{station_name}的到站预测这站这会儿没报出来（刚发车或车机没上线）")
    if not got:
        if not hit:
            return None                  # 目录里根本没这条线，交给上层说「没查到」
        if not near:
            if tally is not None:         # 「一起等」要知道这条线压根不到这站，别当成没车
                tally[hit[0][1]["lineName"]] = None
            return f"{hit[0][1]['lineName']} 不经过{station_name}"
        # 这条线过我这站、方向全被 dest 过滤掉了：返 None 会落到整条线的清单
        # （「9路 现在 2 辆在路上，457 火车站 1.6km」）——站在站台的人读这个没用
        return f"{hit[0][1]['lineName']} 这会儿没有开往{dest or station_name}的预测（刚发车或还没发，过会儿再问）"
    line_name = hit[0][1]["lineName"]
    # 目录里这条线有两个方向、但这站只有一个方向停（单边站）：另一个方向永远不会来，
    # 不加这句的话「往X 首班还没到」会被当成再等等就有了
    ow = (_oneway_note(city_name, station_name, line_name, have[0])
          if len(hit) > 1 and len(set(have)) == 1 else "")
    ow = f"；{ow}" if ow else ""
    live = {k: v for k, v in got.items() if v}
    if tally is not None:                 # 「一起等」要按到站先后排，把这一线摊出来的数留给人
        tally[line_name] = (live, dead, station_name)
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
            return f"{line_name} 这会儿没有到{station_name}的预测（往{dirs}方向都没报过来，刚过站或还没发车）{ow}"
        span = (f"{wins[0][1] or '?'}–{wins[0][2] or '?'}" if len({(x, y) for _, x, y in wins}) == 1
                else "；".join(f"往{k} {x or '?'}–{y or '?'}" for k, x, y in wins))
        return f"{line_name} 这会儿没车报过来，{where}：{span}{ow}"

    # 「N 路离 X 站大约几分钟」：站在站台的人要的是这一句，别念「往X：桂K… 约 18:10 到」
    def one(lab, v):
        return (f"往{lab} " if lab else "") + f"大约 {v[0]} 分钟（{v[1]}，{v[2]}）"

    if len(got) == 1:                     # 只有一个方向过这站（或点了方向），不用念方向名
        return f"{line_name} 离{station_name} " + one("", live[list(live)[0]]) + ow
    # 中途站两个方向都要报：只报一边会被当成哪边都能坐；哪个方向没车也如实说
    def dead_txt(lab, x, y):
        s = _window(x, y)
        if s == "before":
            return f"往{lab} 首班 {x} 还没到"
        if s == "after":
            return f"往{lab} 末班 {y} 已经开走"
        return f"往{lab} 这会儿没车报过来"

    parts = [one(k, v) for k, v in live.items()] + [dead_txt(*d) for d in dead]
    return f"{line_name} 离{station_name}：" + "；".join(parts) + ow


def render_together(city_key, city_name, wants, msg):
    """「1路5路一起等」：一个站，几条线的车摊成一张按到站先后排的表。
    站序本来就有缓存（城+线+方向），条数不影响站点请求数；实时是上游按线路给的，合并不了。"""
    st = pick_stop(city_name, msg)
    if not st:
        return None
    tally = {}
    for w in wants:
        try:
            render_at(city_key, city_name, w, msg, tally=tally, at=st)   # 文本不要，只要它摊出来的数
        except Exception:
            continue
    rows, deads, miss = [], [], []

    def mine(line_name, w):
        # 裸号问「5路」时 _pick 会把「北流105路」也算上：号对不上就别进这张表
        nl = norm_line(line_name)
        return nl == w or (w.isdigit() and w in re.findall(r"\d+", nl))

    for line_name, v in tally.items():
        if not any(mine(line_name, w) for w in wants):
            continue
        if v is None:
            miss.append(line_name)
            continue
        live, _dead, _st = v
        for lab, item in live.items():
            rows.append((item[0], line_name, lab, item[1], item[2]))
        if not live:
            deads.append(line_name)
    if not rows and not deads:
        return None                    # 一条线都没经过这站，交给原来那段说「不经过」
    rows.sort(key=lambda r: (r[0], r[1]))
    out = [f"{st} 这会儿，按到站先后："]
    for mins, line_name, lab, plate, left in rows:
        who = f"{line_name}（往{lab}）" if lab else line_name
        out.append(f"{who} 大约 {mins} 分钟，{left}，{plate}")
    if deads:
        out.append(f"{'、'.join(dict.fromkeys(deads))} 这会儿没车报过来")
    if miss:
        out.append(f"{'、'.join(dict.fromkeys(miss))} 不经过{st}")
    lost = [w for w in dict.fromkeys(wants) if not any(mine(ln, w) for ln in tally)]
    if lost:                           # 问了几条就答几条，目录里没有的明说，别当没听见
        out.append(f"{'、'.join(w + '路' if w.isdigit() else w for w in lost)} 这城目录里没有")
    return "\n".join(out)


# 「9路站点」这种只说两个字的也要认；「到站点了没」「哪个站点」问的是实时位置，别抢
STOPS_RE = re.compile(r"所有?站点|全部站点|站(点)?列(表|出来)|站表|经过哪些站|有哪些站|停哪些站|站序|沿途|(?<![到哪个几])站点")


def render_stops(city_name, want):
    """「B19 路所有站点」要的是站表，不是实时车。两个方向站一样，只念主方向。"""
    rows = _catalog(city_name).get("buslines") or []
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
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
                        r"|(?:有哪些|哪几家|哪几个|有几家|有几个|多少家|多少个|几家|几个)"
                        r"[\u4e00-\u9fa5]{0,4}(?:公交|巴士|客运)公司"
                        r"|(?:公交|巴士|客运)公司[\u4e00-\u9fa5]{0,3}(?:有几家|有几个|几家|几个|多少家|多少个)")
# 「往X方向」在 render_price 里也得认，跟 render_times 一个口径


CITYLINES_RE = re.compile(r"(?:有哪些|哪些|几条|多少条|所有|全部)[\u4e00-\u9fa5]{0,4}(?:线路|公交线|公交(?!公司))")


def _city_wide(msg, city_name):
    """这句问的是整座城吗：把城市名、数据源和问法都抹掉，还剩地名就是在问那个地方。"""
    t = re.sub(r"用车来了|车来了|用掌上|掌上公交|掌上|查询|查一下|查下|查一查|看一下|看下|看看"
               r"|搜一下|搜搜|帮忙|帮我|麻烦|查|看", "", msg.replace(norm_city(city_name), "", 1))
    t = re.sub(r"公交线路|公交线|线路|公交车|公交|有哪些|有没有|哪些|几条|多少条|所有|全部"
               r"|这边|那里|这儿|那边|这里|这|那|该|市|县|有|的|呢|吗|啊|？|\?|\s"
               r"|城巴|九巴|屿巴|新巴|龙运|小巴|巴士", "", t)
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
        # 香港这类城市掌上的 114 返空（status=1 但没目录），车来了那边有整城线名清单
        cid = (cities().get(norm_city(city_name)) or {}).get("chelaile")
        if cid:
            try:
                for n, _i in _cached("catalog-cl", cid, CATALOG_TTL, lambda: chelaile.lines(cid)):
                    ends.setdefault(n, [])
            except Exception:
                pass
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
# 「2路到汽车总站叫我」：到站提醒（照 Trackr 的追踪，答应了就得真在后台盯着）
WATCH_RE = re.compile(r"叫我|喊我|提醒我|提醒一下|提醒下|提醒一声|跟我说一声|跟我说下|通知我|"
                      r"到站提醒|到了说|到了喊|到了叫")
WATCH_OFF_RE = re.compile(r"取消(?:掉)?(?:提醒|盯|追踪|关注)|别(?:再)?(?:提醒|盯|叫)我|"
                          r"不用(?:提醒|盯|叫)|撤(?:掉|了)?提醒|停了提醒|取消订阅")
WATCH_Q_RE = re.compile(r"我的提醒|有哪些提醒|在盯(?:什么|哪些)|提醒列表")
# 用户点名数据源：「从车来了的那个看一下数据」
SRC_RE = re.compile(r"(?P<cl>车来了)|(?P<bp>巴士派迪|派迪)|(?P<my>掌上(?:公交)?)")
# 「9路是哪家公司的」「丢东西了找谁」——只有 114 的 routeBean 带运营公司
COMPANY_RE = re.compile(r"哪家公司|哪个公司|哪家公交|所属公司|运营公司|归谁|谁开的|投诉|客服|电话|遗失|丢了|落下|忘在")


def render_company(city_name, want):
    """114.routeBean 有 company（实测几乎全城都有）和 companyMobile（只有部分城市有）。"""
    rows = [b.get("routeBean") or {} for b in mygolbs.lines(city_name, want)]
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
    co = {}
    for b in mygolbs.lines(city_name):
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
    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
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
        # 用户点的是火车站就只认火车站：问「新郑火车站」扫出「新郑汽车站」等于答错站
        kind = re.search(r"(客运站|汽车站|火车站|公交站|客运中心)", msg)
        hit = next((s for s in sn
                    if (not kind or kind.group(1) in norm_city(s))
                    and any(w in norm_city(s)
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
    """「X 站哪辆车来得最快 / 哪些线路准备到了」：每条经停线每个方向问该站的到站预测，比分钟数。
    始发站上游不给到站预测，只给 busToStationTips=等待发车——车就停在站场，那才是能上车的车。
    没有预测的线路单独点名，不能当成「没车」——末班车、刚发车都会空。"""
    to = {}
    for r in _catalog(city_name).get("buslines") or []:
        to[(r.get("lineName"), str(r.get("upperOrDown")))] = r.get("to") or ""
    hits, wait, blank = [], [], []
    for ln in routes:
        for d in ("1", "2"):
            seq = _station_seq(city_name, ln, d)
            i = next((k + 1 for k, s in enumerate(seq)
                      if (s.get("stationName") or "") == name), None)
            if not i:
                continue
            pay = _live_my(city_name, ln, d, i)
            eta = _etas(pay)
            if eta:
                hits.append((eta[0], ln, to.get((ln, d)) or "", eta[1], eta[2]))
                continue
            # 「等待发车」是始发站的写法：到站预测为空，但车就在站里/站前广场（G01 实测）
            car = next((c for c in pay.get("list") or []
                        if name and name in (c.get("stationName") or "")), None)
            tip = next((r.get("busToStationTips") for r in pay.get("routeOnStationRTimeInfoList") or []
                        if r.get("busToStationTips")), "")
            if car and tip == "等待发车":
                wait.append((ln, to.get((ln, d)) or "", car.get("stationName") or name,
                             car.get("busNumber") or ""))
            elif ln not in blank:
                blank.append(ln)
    if not hits and not wait:
        return None
    hits.sort(key=lambda h: h[0])
    out = [f"{name} 这会儿："]
    for ln, dest, st, plate in wait:      # 能直接上车的排前头
        out.append(f"{ln}" + (f" 往{dest}" if dest else "")
                   + f"：车已经在{st}等着发车" + (f"（{plate}）" if plate else ""))
    for m, ln, dest, cnt, plate in hits:
        out.append(f"{ln}" + (f" 往{dest}方向" if dest else "")
                   + f"：{plate} 约 {max(1, round(m))} 分钟（还有 {cnt} 站）")
    blank = [b for b in blank if b not in {w[0] for w in wait} | {h[1] for h in hits}]
    if blank:
        out.append(f"{'、'.join(blank)} 这会儿没有到站预测（没发车或已过末班）")
    return "\n".join(out)


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
    # 这站只停一个方向的线要单独标出来：站在反方向站台的人看不到「这站这方向没有」
    ow = _oneway_lines(city_name, name, list({n for _, rs in groups for n in rs}))

    def _item(n):
        if not ends.get(n):
            return n
        d = ow.get(n)
        f, t = _dir_end(city_name, n, d) if d else ("", "")
        if f and t:
            return f"{n} {f} → {t}（单向停靠，只往{t}方向）"
        return f"{n} {ends[n][0]}" + ("（单向停靠）" if d else "")

    # 别截 12 条：用户要的是全部，截了还得再问一轮「剩下的呢」，而那轮多半认不出站名
    out = ""
    if len(groups) == 1:
        routes = groups[0][1]
        items = [_item(n) for n in routes if ends.get(n)] or list(routes)
        out = f"{name}有 {len(items)} 条线路：" + "".join("\n" + i for i in items)
    else:
        # 同一片站台被拆成几条记录（汽车总站 / （二医院）/ （大北路）），说清哪路车在哪个站台等
        seen, blocks = set(), []
        for gname, routes in sorted(groups, key=lambda g: -len(g[1])):
            items = [_item(n) for n in routes if n not in seen and ends.get(n)]
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
BEST_RE = re.compile(r"最快|最少|哪[辆部个条]?先|先到|等得最少|等的最少|准备到了|快到了|快到站|来车")
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
SCAN_BUDGET = 6.0     # 临时找一辆车时一次最多扫这么久，到点就报「只翻了前 N 个方向」，下次接着扫
SCAN_WORKERS = 32     # 全城找车是"问一辆车在哪"，并发给高些：昆明 680 个方向 24 并发 18.6 秒
IDX_TTL = 300         # 全城车号索引攒在缓存里，5 分钟内再问接着用、接着往后扫
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


# ── 车辆身份：两家给的号格式各城都不一样，别把「车牌」「自编号」绑到某个源上 ──
# 实测（同一时刻真接口）：玉林 掌上=车牌、车来了=513 这种自编号；河池 掌上=车牌、车来了=000105；
# 武汉 掌上=8A9E8AA8 这种机器号；石家庄 掌上=7376、郑州 掌上=15479（纯数字自编号）；
# 南宁 两家都是车牌；湛江 只有车来了、报的也是车牌。所以判定只能看号本身的形状，不看是谁给的。
_PLATE_GC = re.compile(r"[京津沪渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新]")
KIND_CN = {"plate": "车牌", "selfno": "自编号", "code": "车号"}


def _id_key(s):
    """去掉分隔符、统一大写：「桂k12627d」「桂K·12627D」都成一个键。"""
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def _id_kind(s):
    """这个号是什么样式：plate 车牌 / selfno 纯数字自编号 / code 其它机器号。"""
    raw = re.sub(r"[\s·・]", "", (s or "")).upper()
    k = _id_key(raw)
    if not k:
        return ""
    if _PLATE_GC.search(raw) or re.fullmatch(r"[A-Z]{1,3}\d{4,6}[A-Z]?", k):
        return "plate"
    if k.isdigit():
        return "selfno"
    return "code"


def _id_eq(a, b):
    """两个号是不是同一辆车（跨格式）：桂K12627D ≈ K12627D ≈ 12627D；000105 ≈ 105。"""
    ka, kb = _id_key(a), _id_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ca, cb = _plate_core(a), _plate_core(b)
    if len(ca) >= 5 and ca == cb:
        return True
    # 「12627 那辆车」只写了车牌里数字那截
    if len(ca) >= 5 and len(cb) >= 5 and (ca.startswith(cb) or cb.startswith(ca)):
        return True
    if ka.isdigit() and kb.isdigit():
        return ka.lstrip("0") == kb.lstrip("0")
    return False


def _line_ids(city_key, city_name, line):
    """这条线上两家各自报的车号和坐标：{源: {号: (lat, lng, 站名, 方向)}}。不看号是什么格式。"""
    out, my = {}, {}
    for d in ("1", "2"):
        try:
            for c in _live_my(city_name, line, d).get("list") or []:
                lat, lng = float(c.get("bus_lat") or 0), float(c.get("bus_lng") or 0)
                no = str(c.get("busNumber") or "")
                if no and lat and lng:
                    my.setdefault(no, (lat, lng, c.get("stationName") or "", d))
        except Exception:
            pass
    if my:
        out["mygolbs"] = my
    cid = (cities().get(city_key) or {}).get("chelaile")
    cl = {}
    if cid:
        try:
            for nm, lid in chelaile.lines(cid):
                if norm_line(nm) != norm_line(line):
                    continue
                d = _cached("live-cl", (cid, lid), LIVE_TTL, lambda i=lid: chelaile.live(cid, i))
                pos = d.get("pos") or {}
                for b in d.get("buses") or []:
                    p = pos.get(str(b[0]))
                    if p:
                        cl.setdefault(str(b[0]), (p["lat"], p["lng"],
                                                  (b[1] if len(b) > 1 else ""), d.get("to") or ""))
        except Exception:
            pass
    if cl:
        out["chelaile"] = cl
    return out


def _pair_ids(a, b, max_m=1500):
    """两份 {号: (lat, lng, …)} 按同一刻 GPS 一对一配：{a 号: (b 号, 米)}。与号格式无关。"""
    pairs = sorted((m, ka, kb) for ka, va in a.items() for kb, vb in b.items()
                   if (m := _dist_m({"lat": va[0], "lng": va[1]},
                                    {"lat": vb[0], "lng": vb[1]})) is not None)
    # shortcut: 两家 GPS 刷新节奏不同，同一辆车常差 500 米上下，1.5km 是实测上限
    used_a, used_b, mate = set(), set(), {}
    for m, ka, kb in pairs:
        if m > max_m:
            break
        if ka in used_a or kb in used_b:
            continue
        used_a.add(ka), used_b.add(kb)
        mate[ka] = (kb, m)
    return mate


def _plate_index(city_name, cap=PLATE_MAX, budget=SCAN_BUDGET, start=0):
    """全城逐线翻一遍，车号 → (线路, 方向, 站位)。两家都没按车号查的接口，只能这样。

    返回 (索引, 扫到第几个方向, 全城几个方向)，扫不完时前两个不等。
    cap 默认扫够 PLATE_MAX；临时找一辆车时传小一点。start 是上次扫到的位置，
    大城一次翻不完，下一次接着往后翻（结果攒在 _scan_cache 里）。
    """
    rows = _catalog(city_name).get("buslines") or []
    dirs = {}
    for r in rows:
        d = str(r.get("upperOrDown") or "")
        if d in ("1", "2"):
            dirs.setdefault(r["lineName"], []).append(d)
    jobs = [(n, d) for n, ds in dirs.items() for d in ds]
    total = len(jobs)
    if total > cap:
        # shortcut: 上千条线的城市扫不满，先扫按号排的主线再补其余的，扫不全别报成「全城都没有」
        trunk = sorted((n for n in dirs if n.endswith("路") and n[:-1].isdigit()),
                       key=lambda n: int(n[:-1]))
        names = trunk + [n for n in dirs if n not in set(trunk)]
        jobs = [(n, d) for n in names for d in dirs[n]][:cap]
    idx, core = {}, {}
    done, t0 = start, time.time()
    jobs = jobs[start:start + cap]

    def fetch(j):
        return _live_my(city_name, j[0], j[1]).get("list") or []

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for i in range(0, len(jobs), 40):
            chunk = jobs[i:i + 40]
            for (name, d), cs in zip(chunk, pool.map(fetch, chunk)):
                for c in cs:
                    b = _plate_key(c.get("busNumber"))
                    if not b:
                        continue
                    hit = (name, d, c.get("stationName") or "在途")
                    idx[b] = hit
                    k = _plate_core(b)
                    if k != b:      # 有些城的号只在数字那截一样；撞了就作废，别把两辆车认成一辆
                        core[k] = None if k in core and core[k] != hit else hit
            done += len(chunk)
            if time.time() - t0 > budget:
                break           # 大城按时间收口，扫了多少照实说
    for k, hit in core.items():
        if hit and k not in idx:
            idx[k] = hit
    return idx, done, total


def render_plate(city_name, plate, dir_msg=""):
    """「闽A17997D 在哪」：扫全城找这一辆，报它在哪条线、哪个方向、到了哪站。"""
    try:
        idx, scanned, total = _cached("plate-idx", (city_name, PLATE_MAX), PLATE_TTL,
                                      lambda: _plate_index(city_name))
    except Exception:
        return f"{city_name}这边扫车号的时候上游没响应，过会儿再问一次"
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


def _line_label(name):
    """车来了目录里的线名是裸号（「26」），念出来像半句话：加个「路」（港澳的号本来不带路）。"""
    name = (name or "").strip()
    return name + "路" if name.isdigit() else name


SELFNO_MAX = 800      # 车来了一侧最多翻这么多方向（昆明 680 个方向要能翻完）
CITY_SCAN_MAX = 1200  # 掌上一侧同上


def _selfno_index(city_id, cap=SELFNO_MAX, start=0, budget=SCAN_BUDGET):
    """翻车来了实时：号（归一化）→ [(线名, 方向终点, 到站)]，从 start 起翻 cap 个方向。

    车来了没有按车号查的接口，问「530 现在跑哪条线」只能这样扫。
    返回 (索引, 扫到第几个方向, 全城几个方向)；扫不完时下一次接着往后翻。
    这里不判断号是车牌还是自编号——各城不一样，统一按归一化键入索引。
    """
    rows = _cached("catalog-cl", city_id, CATALOG_TTL, lambda: chelaile.lines(city_id))
    total = len(rows)
    jobs = rows[start:start + cap]
    idx = {}

    def one(row):
        nm, lid = row
        try:
            d = _cached("live-cl", (city_id, lid), LIVE_TTL, lambda: chelaile.live(city_id, lid))
        except Exception:
            return None
        return nm, d.get("to") or "", d.get("buses") or []

    done, t0 = start, time.time()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for i in range(0, len(jobs), 40):
            chunk = jobs[i:i + 40]
            for got in pool.map(one, chunk):
                done += 1
                if not got:
                    continue
                nm, to, bs = got
                for b in bs:
                    idx.setdefault(_id_key(str(b[0])), []).append(
                        (nm, to, b[1] if len(b) > 1 else ""))
            if time.time() - t0 > budget:
                break
    return idx, done, total


def _scan_cached(kind, key, fn):
    """攒着扫：同一个城 5 分钟内接着上次的位置往后翻，翻完就再也不扫。

    大城（昆明 680 个方向）一次翻不完，得把上次的成果留着，再问一次接着翻——
    不然每次都是「只翻了前 80 个方向」，永远查不到。
    """
    ck = (kind, key)
    hit = _cache.get(ck)
    data = hit[1] if hit and time.time() - hit[0] < IDX_TTL else None
    idx, pos, total = {}, 0, 0
    if data:
        idx = dict(data["idx"])
        if data.get("lists"):
            idx = {k: list(v) for k, v in idx.items()}
        pos, total = data["pos"], data["total"]
        if pos >= total:
            return idx, pos, total
    try:
        part, pos2, total2 = fn(pos)
    except Exception:
        return idx, pos, total
    lists = bool(part) and all(isinstance(v, list) for v in part.values())
    for k, v in part.items():
        if k in idx and isinstance(idx[k], list):
            idx[k] = idx[k] + list(v)
        else:
            idx[k] = list(v) if isinstance(v, list) else v
    _cache[ck] = (time.time(), {"idx": idx, "pos": pos2, "total": total2, "lists": lists})
    return idx, pos2, total2


def _selfno_scan(city_id):
    return _scan_cached("selfno-idx", city_id,
                        lambda pos: _selfno_index(city_id, SELFNO_MAX, pos))


def _plate_scan(city_name):
    return _scan_cached("plate-idx", (city_name, CITY_SCAN_MAX),
                        lambda pos: _plate_index(city_name, CITY_SCAN_MAX, SCAN_BUDGET, pos))


def _line_kinds(city_name, line):
    """这条线掌上报的号是什么形状（车牌 / 纯数字自编号 / 机器号）——按实际数据判断，不写死。"""
    if not city_name or not line:
        return None
    try:
        ids = [str(c.get("busNumber")) for d in ("1", "2")
               for c in (_live_my(city_name, line, d).get("list") or [])]
    except Exception:
        return None
    return {_id_kind(i) for i in ids if i} or None


def _source_kinds(city_key, city_name, cid):
    """采样：两家各自报的号长什么样（各看十几条线就够）。

    返回 {源: {"kinds": 形状集合, "ids": 样例号, "no_real": 这城是否整城不给车辆位置}}。
    昆明这种城掌上整城不给车机（hasReal=0）、车来了给 5 位自编号（04387），
    拿一个 4 位数去问时就不必把掌上 797 个方向、车来了 680 个方向全翻一遍。
    """
    ck = ("id-kinds", city_key)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < IDX_TTL:
        return hit[1]
    out = {}

    def grab(fn, jobs, n=40):
        ids, flags = [], []
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                for got in pool.map(fn, jobs):
                    ids += got[0] or []
                    flags += got[1] or []
                    if len(ids) >= n or len(flags) >= 3:
                        break
        except Exception:
            pass
        return {"kinds": {_id_kind(i) for i in ids if i}, "ids": ids[:4],
                "no_real": bool(flags) and all(f == "0" for f in flags)}

    def my_one(j):
        p = _live_my(city_name, j[0], j[1])
        return ([str(c.get("busNumber")) for c in (p.get("list") or [])],
                [str(p.get("hasReal"))] if "hasReal" in p else [])

    rows = [r for r in (_catalog(city_name).get("buslines") or [])
            if str(r.get("upperOrDown")) in ("1", "2")][:16]
    out["mygolbs"] = grab(my_one, [(r["lineName"], str(r["upperOrDown"])) for r in rows])

    if cid:
        def cl_one(j):
            d = _cached("live-cl", (cid, j[1]), LIVE_TTL, lambda: chelaile.live(cid, j[1]))
            return ([str(b[0]) for b in (d.get("buses") or [])], [])
        try:
            crow = _cached("catalog-cl", cid, CATALOG_TTL, lambda: chelaile.lines(cid))[:16]
        except Exception:
            crow = []
        out["chelaile"] = grab(cl_one, crow)
    _cache[ck] = (time.time(), out)
    return out


def _id_hint(samples, tok):
    """号对不上时给一句形状提示：「这城车号是 5 位数字（像 04387），4449 位数不一样」。"""
    tok = str(tok)
    if not tok.isdigit():
        return ""
    got = [str(i) for ids in samples.values() for i in ids if str(i).isdigit()]
    if got and all(len(i) != len(tok) for i in got):
        return (u"（这城车号是 %d 位数字，像 %s，%s 位数不一样）"
                % (len(got[0]), u"、".join(got[:2]), tok))
    return ""


def _city_hits(city_key, city_name, tok, line="", my_kinds=None):
    """全城找这个号：掌上、车来了都翻，返回 (命中 [(源, 线名, 终点, 站名)], 扫描说明, 形状提示)。

    两家给的号格式各城不同（有的城掌上是车牌车来了是自编号，有的反过来，有的两家都是车牌），
    所以按「形状无关」的规则查：归一化键相等、车牌数字段相等、纯数字去前导零相等。
    先翻「号最可能是这一种」的那家，命中就停；形状明显对不上的那家直接跳过（采样判断）。
    """
    kind = _id_kind(tok)
    order = ["mygolbs", "chelaile"] if kind == "plate" else ["chelaile", "mygolbs"]
    cid = (cities().get(city_key) or {}).get("chelaile")
    samples, hint = {}, ""
    kinds_my = my_kinds if my_kinds else _line_kinds(city_name, line)
    if not line:      # 没写线路号：先采样看两家报什么形状，再决定翻哪家、值不值得翻
        sk = _source_kinds(city_key, city_name, cid)
        samples = {src: v["ids"] for src, v in sk.items()}
        hint = _id_hint(samples, tok)
        # 位数就对不上（问 4449、这城车号都是 5 位）就别白扫几百个方向了：直接说清
        if hint and kind == "selfno":
            return [], "", hint
        for src in list(order):
            e = sk.get(src, {})
            # 掌上整城不给车机（昆明 hasReal=0），翻它也是白翻
            if e.get("no_real"):
                order.remove(src)
            elif e.get("kinds") and kind and kind not in e["kinds"]:
                order.remove(src)
    elif kinds_my and kind and kind not in kinds_my:
        order = [x for x in order if x != "mygolbs"]
    hits, spans = [], []
    key, core = _id_key(tok), _plate_core(tok)
    ends = {r["lineName"]: (r.get("from") or "", r.get("to") or "")
            for r in _catalog(city_name).get("buslines") or []}

    for src in order:
        if src == "mygolbs":
            idx, scanned, total = _plate_scan(city_name)
        else:
            idx, scanned, total = _selfno_scan(cid) if cid else ({}, 0, 0)
        if total:
            spans.append((scanned, total))
        found = []
        if src == "mygolbs":
            for k in (key, core):
                got = idx.get(k) if k else None
                if got:
                    name, d, st = got
                    a, b = ends.get(name, ("", ""))
                    found.append(("掌上公交", name, b if str(d) == "1" else a, st))
                    break
        else:
            for k in (key, core):
                for nm, to, st in (idx.get(k) or []) if k else []:
                    found.append(("车来了", nm, to, st))
                if found:
                    break
            if not found:
                # 数字号宽松认：000105 ≈ 105，别因为前导零错过
                for k, rows in idx.items():
                    if _id_eq(k, tok):
                        found += [("车来了", nm, to, st) for nm, to, st in rows]
                        break
        found = [h for h in found if norm_line(h[1]) != norm_line(line)]
        if found:
            return found, _scan_span(spans), hint
    return [], _scan_span(spans), hint


def _scan_span(spans):
    if not spans:
        return ""
    scan, tot = max(spans, key=lambda p: p[1])
    return (u"全城 %d 个方向都翻了" % tot if scan >= tot
            else u"只翻了前 %d 个方向（全城 %d 个）" % (scan, tot))


def render_veh_city(city_key, city_name, veh, line="", my_kinds=None):
    """这条线上没这辆车时，去全城其它线路找它（两家都翻，号是什么格式都认）。

    「530呢」：530 是 G02 号段里的一辆，这会儿可能调去别的线了、也可能停场。
    只回一句「G02 没跑 530」等于把活儿推回给用户——他要的就是这辆车现在在哪。
    """
    if not veh:
        return ""
    hits, span, hint = _city_hits(city_key, city_name, str(veh), line, my_kinds)
    if hits:
        bits = u"、".join(_line_label(nm) + (u"（往%s，刚过%s）" % (to, st.strip()) if to and st else u"")
                         for _src, nm, to, st in hits[:3])
        return u"不过它这会儿在其它线上：%s" % bits
    more = u"，再问一次我接着往后扫" if span and u"只翻了前" in span else u""
    lead = (span + u"，") if span else u""
    return u"%s这会儿没看到 %s 在路上——多半停场或者收车了%s%s" % (lead, veh, hint, more)


def _selfno_mate(city_key, city_name, name):
    """一条线上，两家各自报的车号按同一刻 GPS 一对一配上（与号是车牌还是自编号无关）。

    返回 ({掌上号 key: (车来了号, 距离米)}, {掌上号 key: 掌上那辆车})。
    玉林两家是「车牌 ↔ 513」，河池是「车牌 ↔ 000105」，武汉两家都是机器号——
    格式各城不同，这里只按坐标配，不看形状。
    """
    rows = []
    for d in ("1", "2"):
        rows += [c for c in (_live_my(city_name, name, d).get("list") or [])
                 if float(c.get("bus_lat") or 0) and float(c.get("bus_lng") or 0)]
    mine = {str(c["busNumber"]): (float(c["bus_lat"]), float(c["bus_lng"]), c.get("stationName") or "", d)
            for c in rows}
    ids = _line_ids(city_key, city_name, name)
    mate = _pair_ids(mine, ids.get("chelaile") or {})
    return ({_plate_key(k): v for k, v in mate.items()},
            {_plate_key(c["busNumber"]): c for c in rows})


def _selfno_of(own, other):
    """两边哪个像车牌，另一个就是自编号。深圳反过来：掌上给 BS05070D，车来了给粤B05070D。
    两边都是车牌（车来了也有直接挂车牌的）就没有自编号可报，返空串。"""
    if PLATE_RE.fullmatch(other or ""):
        return own if not PLATE_RE.fullmatch(own or "") else ""
    return other or ""


# 车牌 ↔ 自编号 是这辆车的身份，不是这一刻的位置：对上一次就存下来。
# 收班之后全城实时里扫不到这辆车，再问也还能答，标明是哪天对上的（车调走了就会过时）。
SELFNO_DB = HERE / "selfno.json"


def _selfno_remember(pairs, line):
    try:
        db = json.loads(SELFNO_DB.read_text("utf-8"))
    except Exception:
        db = {}
    now = time.strftime("%m-%d %H:%M")
    for p, no in pairs.items():
        if not no:
            continue
        db[_plate_key(p)] = [no, line, now]
        if _plate_core(p) != _plate_key(p):
            db[_plate_core(p)] = [no, line, now]
    try:
        SELFNO_DB.write_text(json.dumps(db, ensure_ascii=False), "utf-8")
    except Exception:
        pass            # 存不下只是这次没记住，这一句照答


def _selfno_recall(plate):
    try:
        db = json.loads(SELFNO_DB.read_text("utf-8"))
    except Exception:
        return None
    return db.get(_plate_key(plate)) or db.get(_plate_core(plate))


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
    seen = {}
    for k, c in cars.items():
        got = mate.get(k)
        own = c.get("busNumber")
        sn = _selfno_of(own, got[0]) if got else ""
        if sn:
            seen[own] = sn
        # 一句话常同时问「几辆 / 到哪了 / 自编号」，位置顺手带上，别只答其中两件
        at = _car(own, c.get("stationName") or "", c.get("busToStationNiheDistance"),
                  c.get("statusType")).rstrip()
        if sn:
            out.append(f"{at} → {sn}")
        elif got and _id_eq(own, got[0]):
            out.append(f"{at} → 两家给的是同一个号")
        else:
            miss += 1
            out.append(f"{at} → 没配上（车来了那边给它挂的也是车牌）" if got
                       else f"{at} → 没配上（车来了那条线没报这辆）")
    if miss:
        out.append(f"没配上的 {miss} 辆多半是车来了那条线没报这辆车，隔一分钟再问会变")
    _selfno_remember(seen, name)
    return "\n".join(out)


# 「9路用什么车」「配了几辆车」：两家的实时接口都没有车型/配车字段，问到这层才去查建档资料
FLEET_RE = re.compile(r"用什么车|用的什么车|什么车型|什么车跑|什么车在跑|配了几辆|配了几台|配几辆|配车")
# 「有什么配车，现在线上几辆车」是一句话问两件事：配车只答了前一半，用户还得再问一次。
# 「配了几辆车」问的是配车本身，不能算实时，所以前缀只认这几个
LIVE_Q = re.compile(r"(?:现在|线上|当前|这会儿|实时|在跑|在路上)[^，。,]{0,8}(?:几辆|几台|多少辆)|"
                    r"(?:几辆|几台)[^，。,]{0,6}(?:在跑|在路上|在线)")


def _bp_line(city_name, want):
    """巴士派迪建档：返回 (目录真名, uid, 起讫, 公司, 车型列表)；这站没建这条线返 None。

    按地级市建档，县级市（北流、容县）没有自己的 region，落回父城；要用掌上那套带「市」
    的名字——建档按 region 字符串筛，剥成「玉林」一条都对不上。"""
    tail = "" if norm_city(city_name) in ("香港", "澳门") or want.endswith(("线", "路")) else "路"
    region = (cities().get(_district_city(norm_city(city_name))) or {}).get("mygolbs") or city_name
    cid = buspedia.region_id(region)
    if not cid:
        return None
    # 县内线路在目录里带县名（「北流105路」）：裸号查不到建档，得用目录里的真名
    real = [o.get("lineName") for _, o in _pick_city_line(
        [(r["lineName"], r) for r in (_catalog(city_name).get("buslines") or [])], want, city_name)]
    for line in dict.fromkeys([x for x in real if x] + [want + tail]):
        hit = buspedia.find_line(cid, line)
        if hit:
            hit.sort(key=lambda l: not l.get("active"))     # active 标记不可靠（13路标成停运其实在跑），只用来排序
            uid = hit[0]["uid"]
            return (line, uid, hit[0].get("term") or [], buspedia.comp_name(uid),
                    [nm for nm, _ in buspedia.models(uid)])
    return None


def render_fleet(city_key, city_name, want):
    """配车资料只在巴士派迪的建档数据里。线路→型号是众包标注，一半的线填了；
    没填就说没填，别拿同公司别的线顶。数量是整家公司这个型号的保有量，不全是这条线的。"""
    try:
        got = _bp_line(city_name, want)
    except Exception:
        return "巴士派迪那边没响应，过会儿再问一次"     # 别默默落到实时，那答的不是配车
    if not got:
        return f"巴士派迪建档里没有 {want + '路' if want.isdigit() else want} 这条线的车型（那站是人工填的，不是每城每线都有）"
    line, _uid, _term, _comp, parts = got
    if not parts:
        return f"{line} 的车型这站没填"
    # 只报车型不报数量：/model 的保有量是整家公司在用这个型号的车
    # （南宁704 数出来 986 辆，一小时一趟的线也挂着一百多辆），当成这条线的配车数就是错的
    return (f"{line} 建档了 {len(parts)} 种车型：" + "、".join(parts)
            + f"\n（巴士派迪只记车型，不记每条线配几辆——要看这会儿跑着几辆，问「{line}现在几辆」）")


# 「LCK6116EVGA2 在玉林配属哪条线路」：型号代号不是线号（尾数 2 会被当成 2 路）
MODEL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4}\d{3,}[A-Za-z0-9]{0,8})(?![A-Za-z0-9])")
MODEL_Q = re.compile(r"配属|配给|配哪|哪条线|哪些线路|哪条路|跑哪条|用在哪|上了哪些")


def render_model(city_name, code):
    """型号 → 哪几条线在用：建档里只有「线 → 车型」，反过来得一城逐线翻（buspedia.model_lines）。"""
    region = (cities().get(_district_city(norm_city(city_name))) or {}).get("mygolbs") or city_name
    try:
        cid = buspedia.region_id(region)
        rows = buspedia.model_lines(cid, code) if cid else []
    except Exception:
        return f"巴士派迪那边没响应，过会儿再问一次"
    if not rows:
        return (f"巴士派迪建档里，{region}这边没有哪条线路登记了 {code} 这个型号"
                f"（建档是人工填的，也可能这型号压根没在这儿跑）")
    out = [f"{region} 建档里用 {code} 的线路 {len(rows)} 条："]
    for name, names in rows:
        out.append(f"  {name}（{names[0]}）")
    out.append("（建档资料，不是这会儿在跑的；要看实时就报线路名问）")
    return "\n".join(out)


def render_bp(city_name, want):
    """「用巴士派迪查 D276」：点名了建档站，就只给建档那份——起讫、公司、车型，不给实时。"""
    try:
        got = _bp_line(city_name, want)
    except Exception:
        return f"巴士派迪那边没响应，过会儿再问一次"
    if not got:
        # 建档是人工的，全国只建了一部分线和城，说清是「没建」而不是「没这条线」
        return (f"巴士派迪没建「{want}」这条线（那站是人工建档，不是每座城每条线都有）；"
                f"要看实时就问「{want}现在有车吗」")
    line, _uid, term, comp, parts = got
    out = [f"巴士派迪建档 · {line}"]
    for t in term:
        out.append("  " + re.sub(r"^\[(?:上行|下行)\]", "", t))
    if comp:
        out.append(f"  运营公司：{comp}")
    out.append("  车型：" + ("、".join(parts) if parts else "这站没填"))
    return "\n".join(out)


def render_selfno(city_key, city_name, token):
    """「XX 的自编号是啥 / 这辆车的另一个号是啥」：用户给的可能是车牌，也可能是自编号。

    两家给的号格式各城不一样（玉林掌上=车牌、车来了=513；河池车来了=000105；武汉两家都是
    8A9E8AA8 这种机器号；南宁两家都是车牌），所以先全城定位它在哪条线，再按同一刻 GPS
    把另一家给的那个号配出来，并说清哪个是车牌、哪个是自编号。
    """
    token = (token or "").strip()
    if not token:
        return ""
    kind = KIND_CN.get(_id_kind(token), "车号")
    hits, span, hint = _city_hits(city_key, city_name, token)
    if not hits:
        old = _selfno_recall(token)
        if old:
            return (f"{token} 这会儿不在路上，上次 {old[2]} 在 {old[1]} 上对上的另一个号是 {old[0]}"
                    f"（车要是调了线，这条就旧了）")
        more = "，再问一次我接着往后扫" if span and "只翻了前" in span else ""
        lead = (span + "，") if span else ""
        return f"{lead}没看到 {token} 这辆车{hint}{more}"
    src, name, _to, st = hits[0]
    ids = _line_ids(city_key, city_name, _line_real(city_name, name))
    mine_src = "mygolbs" if src == "掌上公交" else "chelaile"
    mine = ids.get(mine_src) or {}
    other_src = "chelaile" if mine_src == "mygolbs" else "mygolbs"
    other = ids.get(other_src) or {}
    own = next((k for k in mine if _id_eq(k, token)), "")
    mate = _pair_ids(mine, other)
    got = mate.get(own)
    where = f"在 {_line_label(name)} 上" + (f"，刚过 {st.strip()}" if st else "")
    if src == "车来了" and not (ids.get("mygolbs")):
        # 这城掌上那条线没报车，没有另一家可对
        return f"{token}（{kind}）{where}；这城掌上没报这条线的车，另一个号对不上"
    if got:
        other_id, dist = got
        o_kind = KIND_CN.get(_id_kind(other_id), "车号")
        if _id_eq(own, other_id):
            return f"{token}（{kind}）{where}；另一家给的是同一个号" \
                   if o_kind == kind else f"{token}（{kind}）{where}；另一家也叫 {other_id}"
        return (f"{token}（{kind}）{where}；另一家给它的号是 {other_id}（{o_kind}）"
                f"，两家 GPS 差 {dist:.0f} 米")
    return f"{token}（{kind}）{where}；这条线另一家没报这辆车，暂时对不上"


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


def render_watch(key, city_key, city_name, want, msg):
    """「2路到汽车总站叫我」：把这条线盯起来，剩两站时在群里喊一声。"""
    line = want if not want.isdigit() else want + "路"
    # 「叫我」「2路到」都不是站名的一部分，先抹掉再找站，不然会搜出「汽车总站叫我」这种
    s = WATCH_RE.sub(" ", msg)
    s = re.sub(r"[A-Za-z]{0,2}\d{1,4}\s*[路线]?", " ", s)
    stop = pick_stop(city_name, s) or pick_stop(city_name, msg)
    if not stop:
        return f"{line} 到哪一站？说个站名我盯着（「{line}到汽车总站叫我」）"
    seqs = {}
    # _station_seq 要的是目录里的真线名（「2路」），归一化后的「2」它认不出
    rows = (_catalog(city_name).get("buslines") or [])
    lname = next((r.get("lineName") for r in rows
                  if norm_line(r.get("lineName") or "") == norm_line(want)), want)
    for d in ("1", "2"):
        try:
            names = [s.get("stationName") or "" for s in _station_seq(city_name, lname, d)]
        except Exception:
            names = []
        if names:
            seqs[d] = names
    if not seqs:
        return (f"{line} 的站序只有掌上有，这条线那边没给，盯不了它到哪站；"
                f"问「{line}还有几分钟」倒能给你实时的")
    if not any(stop in ns for ns in seqs.values()):
        return f"{line} 不到 {stop}，说个这条线经过的站"
    return watch.add(key, city_key, city_name, want, stop, seqs)


def _one(city_key, name, num, prefix, msg, src, explicit, last, key=""):
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
    # 县级市线路落到父城查（新郑2路 → 郑州市目录）：裸号「2」是父城自己的 2 路，退回去就是拿同名线顶替。
    # 写的就是父城名（「乌鲁木齐BRT2」）时不算县级市，别拼成「乌鲁木齐BRT2」这种目录里没有的名字
    if prefix and norm_city(prefix) != city_key \
            and norm_city(_district_city(prefix) or "") == city_key:
        wants = [prefix + num]
    # 有前缀要纠前缀截短；「环山二号线」这种自带汉字的线名也要纠（前缀是空的）
    # 数字线号同样要纠：「58路大站快车」和「58路」同城并存，只拿裸号会答成普通线。
    # _exact_line 要求整段真线名出现在句子里，裸号问法不受影响
    # BRT 的叫法先补全（「BRT4」↔「快速公交4线」），不然只能落到同城的别的 T4/快4 上
    if name:
        add = [a for w in list(wants) for a in brt_alias(w) if a not in wants]
        # 别名得排在裸号前面：厦门「快速公交1线」先试「快1」，试不到才轮到「1」（那是另一条线）
        wants = ([wants[0]] + add + wants[1:]) if add else wants
        cat = _catalog(name)
        # 网络挂了才不敢兜底：目录取不到时裸号会答成另一条线（「新乐专1路」→ 1 路）；
        # 业务上的「这城没目录」照旧往下走
        if cat.get("error") and "detail" in cat:
            return cat.get("msg") or ""
        # 前缀被 PREFIX_RE 截短过（「新乐专1路」→「庄新乐专」），目录里有真线名就用真的
        # 点名单（「城巴1」）时不能纠：香港掌上目录里只有一个 1 路（九巴的），纠过去就答错公司
        # 「博罗233路」也不能纠：句子里那个「233路」是惠城—惠阳的线，博罗没有 233，
        # 纠过去就把同城的另一条线当成博罗的车报（跟县级市那条分支一个道理）
        county_ns = bool(prefix) and norm_city(prefix) != city_key and _is_place(prefix) and any(
            (r.get("lineName") or "").startswith(prefix) for r in cat.get("buslines") or [])
        # 句子里写的是「快速公交1线」/「BRT1」时别拿目录里的「1路」来纠：那是同城的另一条线，
        # 该纠的方向是「快1」，交给上面的别名
        brt_words = bool(re.search(r"快速公交|BRT", msg, re.I)) and bool(add)
        full = None if (op or county_ns or brt_words) else _exact_line(name, msg, num, city_key)
        if full:
            wants = [norm_line(full)]      # wants 里存的向来是归一化后的号
        if county_ns:
            # 目录里这个县的线路自成一套（博罗1路、博罗222路…）：只认带县名的写法，
            # 裸号是同城的另一条线，不能拿来顶
            wants = [w for w in wants if w.startswith(prefix)] or [prefix + num]
    last["line"] = wants[0]
    # 「2路到汽车总站叫我」：答应了就得真在后台盯着，别只给一句实时就完
    if key and name and WATCH_RE.search(msg):
        return render_watch(key, city_key, name, wants[0], msg)
    if name:
        for w in wants:
            # 「用巴士派迪查D276」：点名了建档站，只给建档那份（起讫/公司/车型），不掺实时
            if src == "buspedia":
                return render_bp(name, w)
            # 「9路用什么车」：车型/配车只有建档资料有，先问它，答不上再走实时
            if FLEET_RE.search(msg):
                out = render_fleet(city_key, name, w)
                if out and LIVE_Q.search(msg):       # 顺带问了实时，一并给
                    live = render(city_key, w, query(city_key, [w]))
                    if "在路上" in live:
                        out += "\n" + live.replace("有，", "", 1)   # 单独发才需要「有，」
            # 「G02 这几辆的自编号」：整条线一次列完，别只答一辆
            elif SELFNO_RE.search(msg) and not PLATE_RE.search(msg):
                _vt = _veh_tokens(msg)
                out = render_selfno(city_key, name, _vt[0]) if _vt else render_line_selfno(city_key, name, w)
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
                _v2 = VEH2_RE.search(msg)
                out = render_at(city_key, name, w, msg, _v2.group(1) if _v2 else "")
                # 「玉林北站要等多久才能等到G02」：站名整段在线号前面时 parse 把它当前缀取走了，
                # 取站那一步就从整句里抠不出干净的站名（抠出来的是「玉林北站要等多久才能等」），
                # 于是反口问「是哪一站等」。前缀真在这城站表里就拿它当站名再问一遍
                if not out and prefix and norm_city(prefix) != city_key \
                        and find_stop(name, prefix)[0]:
                    out = render_at(city_key, name, w, msg,
                                    _v2.group(1) if _v2 else "", at=prefix)
                # 「玉林九中的3路等多久」站名没认出来：站在站台问钟点的人读「每辆车离它旁边那站多远」没用
                if not out and TIMEQ_RE.search(msg) and not INTERVAL_RE.search(msg):
                    return f"{w + '路' if w.isdigit() else w} 是哪一站等？说个站名我给几分钟"
                # 「多久一班」「发车间隔」：上游两家都没有这个字段，别装作答了——给实时，再说清缺什么
                if out and re.search(r"多久一[班趟台]|几[分]钟一[班趟台]|间隔|一趟多久|多久一趟", msg):
                    out += "\n（发车间隔两家都不给，只能看实时的这几辆）"
            if out:
                return out
    vm = VEH_RE.search(msg)
    veh = next((g for g in vm.groups() if g), "") if vm else ""
    _toks = _veh_tokens(msg)          # 句子里那个车号（「530呢」的 530、8A9E8AA8…）
    tok = _toks[0] if _toks else ""
    inherited_veh = False
    res = query(city_key, wants, src, veh)
    if not veh and not res.get("error"):
        # 句子里夹着车号（「现在513去哪里了」）：拿实时清单里的车号对一遍，对上哪辆就只报哪辆。
        # 513 是自编号，手上这份可能是车牌——_veh_from 会按 GPS 把两家的号配一次
        veh = _veh_from(res, msg, city_key, name, wants[0])
        if not veh and tok and (re.search(r"车|辆|号|到哪|在哪|哪了|去哪", msg) or _vehicle_talk(msg)):
            # 号对不上本线任何一辆也先当这辆答：「…没跑 530 这辆」+ 去全城找。
            # 摊开整条线等于把「530 在哪」这个问题推回给用户
            veh = tok
        if not veh:
            # 「现在呢」「到哪了」：上一句问的就是某一辆车，接着问的还是那辆（半小时内）
            if (last.get("veh") and last.get("line") == wants[0]
                    and time.time() - (last.get("veh_t") or 0) < 1800
                    and len(msg) <= 12 and _FOLLOW_VEH.search(msg)
                    and any(_veh(b, last["veh"]) for _, bs in res.get("groups") or [] for b in bs)):
                veh, inherited_veh = last["veh"], True
    # 用户是用自编号问的、清单里报的是车牌：抬头把两个号都写上，别让他以为答错了车
    shown = f"{tok}（{veh}）" if veh and tok and tok != str(veh) else ""
    if not shown and inherited_veh:
        shown = last.get("veh_shown") or ""      # 追问「现在呢」时接着写「513（桂K…）」
    if veh:
        last["veh"], last["veh_t"] = veh, time.time()
        # 车号是分源记的（车来了报自编号 514、掌上报车牌桂K12623D）：追问得接着用它那一家的源，
        # 换一家这个号对不上任何一辆，用户问的那辆就丢了
        last["veh_src"] = "chelaile" if res.get("source") == LABEL["chelaile"] else "mygolbs"
        last["veh_shown"] = shown
    else:
        for k in ("veh", "veh_src", "veh_shown"):
            last.pop(k, None)
    txt = render(city_key, wants[0], res, veh, _dir(msg), shown)
    # 「530呢」：这条线没这辆车时去全城其它线路找一遍再答，别只丢一句「没跑这辆」
    if veh and f"没跑 {veh}" in txt:
        _kinds = ({_id_kind(str(b[0])) for _, bs in res.get("groups") or [] for b in bs}
                  if str(res.get("source") or "").startswith(LABEL["mygolbs"]) else None)
        other = render_veh_city(city_key, name, veh, wants[0], _kinds)
        if other:
            txt += f"\n{other}"
    # 点名车来了、可这条线只有掌上有：别让掌上的车冒充车来了的
    if src and res.get("source") == LABEL["mygolbs" if src == "chelaile" else "chelaile"]:
        txt += f"\n（{LABEL[src]}那边没这条，这份是{res['source']}的）"
    # 「G01多久一班」没有站名，走不到上面那条；上游两家都没有间隔字段，给实时并说清缺什么
    if re.search(r"多久一[班趟台]|几[分]钟一[班趟台]|间隔|多久一趟", msg) and "发车间隔" not in txt:
        txt += "\n（发车间隔两家都不给，只能看实时的这几辆）"
    # 句子里写的是「从1路东线」这种号上粘着汉字的名字，本城目录里匹到的却是「1路」（或压根没这条）：
    # 那是别的城市的同号车，别拿本城的 1 路顶掉，也别回「没查到「1」」；这名字自己看不出城市，问
    w = _written_name(msg, wants[0]) if city_key not in msg else ""
    if w and (txt.startswith("没查到") or norm_line(w) != norm_line(str(res.get("line") or ""))):
        # 「博罗233路」：博罗是惠州的下辖县，问题不在「这是哪座城市的线」，
        # 而是博罗自己没有 233 路（惠州 233 路跑的是惠城—惠阳那边）
        host = _district_city(w)
        if host and norm_city(host) == city_key:
            own = [r["lineName"] for r in (_catalog(name or "") or {}).get("buslines") or []
                   if (r.get("lineName") or "").startswith(w.rstrip("路" + "线"))]
            return (f"{w.rstrip('路') if w.endswith('路') else w} 在{norm_city(host)}没有这条线"
                    + (f"；{norm_city(host)}{w[:2]}这边有的是 {'、'.join(dict.fromkeys(own))}" if own[:4] else "")
                    + ("" if own else f"（{norm_city(city_key)}目录里是另一条同号的线，别混了）"))
        return (f"「{w}」{norm_city(city_key)}这边没这条，这是哪座城市的线？"
                f"带上城市名我再查（像「广州{w}」）")
    # 没明写城市就按默认城市查，查不到时别让用户以为「这线路不存在」
    if txt.startswith("没查到") and city_key not in msg:
        txt += f"\n（你没写城市，我按{norm_city(city_key)}查的，别的城市前面带上城市名）"
    return txt


def answer(msg, default_city, ctx="", key=""):
    """ctx 是引用/上文里的文字，只用来认城市——追问「那 39 路往哪开」靠它。"""
    if CODE_RE.search(msg):      # 群里贴的脚本不查公交，交给模型
        return ""
    if _chatty_code(AT_RE.sub(" ", msg)):
        # 「最早是拿玉林那个 h5 起步的」：h5 不是线号。工具被模型调进来时也别答
        # 「没查到「玉林那个H5」这条线」，返空让模型正常接话
        return ""
    if _about_someone(AT_RE.sub(" ", msg)):   # 只是跟别人说话（bus_query 也被模型调过）
        return ""
    # 「公交？」「巴士」这种只有称呼的一句，是对话里的反问，不是在问某条线/某个站。
    # 玉林站表里有个「公交二车队」，以前会答「公交二车队 车来了这边有 1 条线路：3 红十字会医院 ↔ 公交二车队」
    if _generic_word(msg):
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
    src = ("chelaile" if sm.group("cl") else "buspedia" if sm.group("bp") else "mygolbs") if sm else None
    if src:
        last["src"] = src
    # 「用掌上公交查G02路」：「掌上公交查」不是城市名，别让前缀截出「上公交」当城
    msg = SRCPRE.sub("", msg)
    # 提醒的撤单和查看不需要线路号，先接走（答应了盯车，就得让它停得掉）
    if key and WATCH_OFF_RE.search(msg):
        return watch.stop(key, msg)
    if key and WATCH_Q_RE.search(msg):
        return watch.listing(key)
    # 「LCK6116EVGA2 在玉林配属哪条线路」：型号代号当线号会匹到 2 路，先按型号问法接走
    mm = MODEL_RE.search(msg)
    # 型号代号后面还得挂字母（LCK6116EVGA2 / ZK6821NG5）：D276 这种纯「字母+数字」是线号不是型号
    if mm and MODEL_Q.search(msg) and re.search(r"[A-Za-z]{2}", re.sub(r"^[A-Za-z]{2,4}\d{3,}", "", mm.group(1))):
        cy, _ex = detect_city(msg, "", default_city)
        return render_model(cy or norm_city(default_city), mm.group(1))
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
    # 「现在513去哪里了」「现在呢」：没写线号，问的是上一句那条线上某辆车现在到哪。
    # 以前这里不认，落到站名那条路，最后返空——群里一声不吭，模型就自己编。
    if not line and last.get("line") and _vehicle_talk(msg):
        line = (last["line"], "")
        # 上一句点名了源（「用车来了查 g02」）就接着用它：两家的车号体系不一样，
        # 换成掌上以后 514 这个自编号对不上任何一辆，用户问的那辆就丢了
        if not src and (last.get("veh_src") or last.get("src")):
            src = last.get("veh_src") or last["src"]
    # 「从车来了的那个看一下数据」没写线路号：说的就是上一条问的那路车。
    # 但「用车来了查化州-湛江线」也带「查」，那是点名的另一条文字线名（parse 认不出带「-」的），
    # 当成追问上一条就会答成上一轮的 506——只有指着上一条说（这个/那条）或整句只剩追问词时才续
    # 源名本身也要剥：「用车来了查」剥完是空的，就是「换个源再问一遍上一条」
    _bare = re.sub(r"[看查询数据接口一下怎么样呢吗的了，。？?！!\s]", "",
                   re.sub(r"用车来了|车来了|掌上公交|掌上|巴士派迪|派迪", "", SRCPRE.sub("", raw)))
    inherited = False
    if src and not line and last.get("line") and re.search(r"看|查|数据|接口", raw) and (
            re.search(r"这个|那个|这条|那条|该线|这路|那路", raw) or len(_bare) <= 3):
        line = (last["line"], "")
        inherited = True
    prefix = line[1] if line else ""
    # 「泉州公交11路」「泉州公交K502」：前缀截到的是「泉州公交」，去掉「公交」才是城市名，
    # 留着就会拼成「泉州公交K502」这种不存在的线
    # 「快速公交1线」的「公交」是线名的一部分（厦门叫「快1路」、北京叫「快速公交1线」），
    # 剥掉就剩「快速」，拼成「快速1」——厦门没有这条，答成同城的 1 路
    prefix = re.sub(r"(?<!快速)公交$", "", prefix)
    # 「惠州博罗233路」：前缀里粘着城市名（惠州 + 博罗县），线名里只写县名。
    # 不剥掉会拼成「惠州博罗233」命中不了，还会把「博罗」当站名，答成「233路 不经过博罗天虹」
    for _ck in sorted(cities(), key=len, reverse=True):
        _rest = prefix[len(_ck):]
        if prefix.startswith(_ck) and len(_rest) >= 2 and (
                _rest in DIST_BARE or _rest in DISTRICTS or _district_city(_rest)):
            prefix = _rest
            break
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
        # 追问「用巴士派迪查」自己没写城市时，上一句答的站名会被当成县级市：
        # 「往安吉客运站」里的安吉成了浙江安吉县，答出「安吉这边有的是 207路」。
        # 城市名在 ctx 里只是更长一段话的前半截（安吉客运站 / 玉林北站）就不算明写
        if explicit and city_key not in msg and re.search(
                re.escape(city_key) + r"[东南西北中]?(?:客运|车站|站|公交|地铁|机场|路|线)", ctx):
            back = last.get("city") if last.get("city") in cities() else norm_city(default_city)
            city_key, explicit = (back if back in cities() else None), False
    base = norm_city(default_city)
    # 「316路山南张庄接驳车」：「山南张庄」是石家庄的站名，西藏山南市被当成城市，
    # 答出「山南这边有的是 6路、1路」。城市名只是更长一段话的前半截、而且那座城压根没这条线时不算明写
    if explicit and line and re.search(re.escape(city_key) + r"[\u4e00-\u9fa5]", msg) \
            and not re.search(re.escape(city_key) + r"(?:路|线|公交|巴士|城际|城乡|市郊|市域|客运|车站|地铁)", msg):
        try:
            if not mygolbs.lines((cities().get(city_key) or {}).get("mygolbs") or city_key, line[0]):
                city_key, explicit = (base if base in cities() else None), False
        except Exception:
            pass
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
            # 县级市（新郑、巩义）在掌上不是一个城市，它的线路挂在父城目录里、线名自带县名前缀
            # （「新郑12路」全在郑州市那 1283 条里）。落到父城查，查不到也让「没查到 + 同前缀的线」讲清楚
            parent = _district_city(prefix) if line else None
            pk = norm_city(parent or "")
            if pk and pk in cities():
                city_key, explicit = pk, True        # 用户写了地名，别再补「你没写城市，我按X查的」
            elif line and bn and find_stop(bn, prefix)[0]:
                city_key = base
            elif looks_like_ask(msg):
                if not _covered(prefix):
                    # 别写「查不了」：main.py 的 FALSE_CLAIM 会把这句当假话整句吞掉，用户啥也收不到
                    # 前缀不是地名（「玉高附中」这种口语站名）时说人家整座城没数据是撒谎，点名是哪个站没找到
                    if not re.search(r"[市县区州旗盟]$", prefix):
                        # 「金龙湾来8路没有。」：站名对了、城市不对。别只说「站表里没有」——
                        # 这站可能在别的城（金龙湾在河池8路线上），让用户补城市
                        return (f"{bn or base}站表里没查到「{prefix}」这个站，"
                                f"说个城市名我再查（像「河池{prefix}」）")
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
            CITYLINES_RE.search(msg) or (explicit and "公交线路" in msg)
            # 「用车来了查湛江线路」「香港城巴全部线路」：没写「有哪些/全部」也是在要整城清单，
            # 不接这句就落到最后返回空串，群里一声不吭
            or (explicit and re.search(r"线路", msg))) and _city_wide(msg, name):
        out = render_city_lines(name)
        if out:
            return out
    # 「玉林公交公司电话是多少」：没带线路号，问的是整座城的公司，别回空交给模型（它会编）
    if not line and CITY_CO_RE.search(msg) and not (SELFNO_RE.search(msg) and re.search(r"\d{3,}", msg)):
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
    # 「玉林G02 桂K12623D 到哪了」「石家庄43路 7376 到哪了」：HOP_RE 按「到」切成
    # 「从X到哪」，答成换乘方案。带车牌/车号问「到哪」问的是那辆车的位置，不是换乘。
    # 车号形状各城不同（车牌、7376、8A9E8AA8），所以按 _veh_tokens 判，不只看车牌
    if pm or (line and _veh_tokens(msg)) or _vehicle_talk(msg):
        got = None
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
    # 「这三辆车的自编号分别是多少」：没写线路号时沿用上一条问过的线，整条线一次列完。
    # 但句子里点了具体一辆（「514 的自编号是啥」）就只答那一辆，别把整条线摊开
    if not line and name and SELFNO_RE.search(msg) and not PLATE_RE.search(msg):
        _vt = _veh_tokens(msg)
        want = last.get("line") or ""
        if _vt and want:
            return render_selfno(city_key, name, _vt[0])
        out = render_line_selfno(city_key, name, want) if want else None
        if not out:
            # 光给一句号（「济宁公交自编号230021」）没有线路名：扫全城太慢，问一句比一声不吭强
            num = re.search(r"\d{3,}", msg)
            if num:
                return f"自编号 {num.group()} 是哪条线上的？报个线路名（比如 9路）我就能查出这辆车跑到哪了"
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
            # 「我在汽车总站等1路和G02」：一起等，一个站一张表按到站先后排，别各说一段
            # 问的是站表（「1路9路站表」）就别排队了，站表有它自己的答案
            if name and not STOPS_RE.search(msg) and (TIMEQ_RE.search(msg) or re.search(r"等", msg)):
                tog = render_together(city_key, name,
                                      [n for n, _pre in multi], msg)   # 前缀常是站名尾巴（「火车站等1」），不用
                if tog:
                    last["stop"] = pick_stop(name, msg) or last.get("stop")
                    return tog
            outs = [t for t in (_one(city_key, name, n, pre, msg, src, explicit, last, key)
                                for n, pre in multi) if t]
            if outs:
                return "\n\n".join(outs)
        # 前缀是线路名的一部分（容县11路），明写城市也不该丢——explicit 只管去哪个目录找
        return _one(city_key, name, line[0], prefix, msg, src, explicit, last, key)
    if name:
        # 「玉林397号车在哪」：没写线路号，只有车号——全城找这一辆，报它在哪条线上。
        # 以前这里落到站名那条路，最后返空，模型就自己编「玉林没有 397 这条线」
        if _veh_tokens(msg) and re.search(r"车|辆|号", msg):
            out = render_selfno(city_key, name, _veh_tokens(msg)[0])
            if out:
                return out
        # 「还运营吗」「哪条线在跑」没有站名，别拿去当站名搜（哈尔滨 → 哈尔滨站）
        pl = re.search(r"车牌|号牌", msg)
        # 「你们那还有多少车是纯发动机的」不是问哪条线在跑，是问车辆本身（车型/动力）——
        # 这个数据两家实时接口都没有，扫全城只会回一句「玉林市这会儿有 20 条线在跑」
        if pl or (SWEEP_RE.search(msg) and not re.search(r"站|几路|哪路", msg)
                  and not re.search(r"发动机|柴油|汽油|纯电|新能源|车型|配车|座位|空调", msg)):
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
            raw_place = TAIL.sub("", JUNK_HEAD.sub("", STOPASK_RE.sub("", msg))).strip()
            # 原名先试：「玉林火车站有几路车」剥掉城市名剩「火车站」，站表里反而认不出
            for place in dict.fromkeys([raw_place, raw_place.replace(norm_city(name), "", 1).strip()]):
                if len(place) < 2:
                    continue
                got = render_stop(city_key, name, place)
                if got:
                    last["stop"] = pick_stop(name, place) or last.get("stop")
                    return got
            bare = raw_place.replace(norm_city(name), "", 1).strip()
            return f"{norm_city(name)}站表里没查到「{bare if len(bare) >= 3 else raw_place}」这个站"
        # 站名猜不出是哪座城的：问一句，别回空让模型自己圆话（「返回的是玉林全市正在跑的线路」）。
        # 「有一条线路查不到实时信息我就不写出来了（）」是吐槽，反问就抢话了
        # 还得真的是公交问句：「这是抓的档案站吗」只有一个「站」字，
        # 反问成「「这是抓的档案站」这个站没查到」就把闲聊抢了
        if not explicit and BUS_RE.search(msg) and re.search(r"站|线路|几路", msg) \
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
    # 「用车来了查」只点了源名、没线名，上一条也没留下线：反问一句。
    # 返空的话模型会自己编一份「车来了这边只有 3 条」（07:19 那次就是）
    if src and not line and not _bare and re.search(r"查|看|用|一下", raw):
        return "查哪条线？报个线名，我换个源再查"
    return "上游这会儿没响应，过会儿再问" if (BUS_RE.search(msg) and _up_fail[0] >= t0) else ""
