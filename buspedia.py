"""巴士派迪（buspedia.top）的配车资料：某条线用什么车型、这个型号一共几辆、自编号区间。

这站是人工建档的静态资料库，不是实时车况——它有的正是两家实时接口都没有的东西
（车型、配车数、自编号段）。返回给用户时别说成「这会儿在跑的」，只能说车队登记。

接口要点：响应体是裸 deflate（没写 Content-Encoding），要自己 zlib 解压。
纯标准库，不 import 本插件任何模块。
"""
import json
import re
import time
import urllib.request
import zlib

BASE = "https://api.buspedia.top/"
# 短 UA 会被 nginx 直接掐断连接（RemoteDisconnected），必须像浏览器的完整一串
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*", "Referer": "https://buspedia.top/",
        "Accept-Language": "zh-CN,zh;q=0.9"}
TTL = 6 * 3600
SUFFIX = re.compile(r"(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|地区|盟|市|区|县)$")
_cache = {}


def _api(path, ttl=TTL):
    now = time.time()
    hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(BASE + path, headers=HEAD)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    try:
        raw = zlib.decompress(raw)
    except zlib.error:
        pass                     # 小响应有时不压，直接就是 JSON
    data = json.loads(raw)
    _cache[path] = (now, data)
    return data


def cities():
    """地区名 → id。接口按省份分组返回二维列表。"""
    return {r["name"]: r["id"] for grp in _api("region") for r in grp}


def region_id(city_name):
    """掌上的城市名（「香港特别行政区」）和这边的写法（「香港」）不一定一样，剥尾缀再比一次。"""
    idx = cities()
    if city_name in idx:
        return idx[city_name]
    core = SUFFIX.sub("", city_name or "")
    for k, v in idx.items():
        if SUFFIX.sub("", k) == core:
            return v
    return None


def find_line(city_id, name):
    """全市线路目录里按线名找；同名的可能有几条（两家公司各开一条），全返回。"""
    out = []
    for g in _api("region/%s/lines" % city_id):
        for b in g.get("buckets") or []:
            for l in b.get("lines") or []:
                if l.get("name") == name:
                    out.append(l)
    return out


def models(uid):
    """这条线登记的常用车型：[(型号名, 车型 id)]。空 = 这站建了线路但没填型号。"""
    d = _api("line/" + uid).get("line") or {}
    return [(m["name"], (m.get("params") or {}).get("id")) for m in d.get("常用车型") or []]


def comp_name(uid):
    """这条线归哪家公司（线路页登记的，没填就是空）。"""
    d = _api("line/" + uid).get("line") or {}
    return ((d.get("公司") or [{}])[0]).get("name") or ""


def fleet(mid, comp="", region=""):
    """型号的保有量：[(年份, 台数, 自编号区间)]。机型页是全国的，
    每个 block 记的是一家公司在一个地区的批次，不筛就会把全国同型号的车加在一起。"""
    out = []
    for blk in _api("model/" + mid).get("data") or []:
        if comp and ((blk.get("comp") or {}).get("name") or comp) != comp:
            continue
        if region and (blk.get("region") or region) != region:
            continue
        for row in (blk.get("data") or {}).get("生产年份") or []:
            for b in row.get("batches") or []:
                out.append((row.get("value"), b.get("count") or 0, (b.get("no") or [""])[0]))
    return out


def comps(city_id):
    """城市的运营公司：[(公司 id, 公司名)]。"""
    d = _api("region/%s" % city_id)
    names = d.get("cmap") or {}
    return [(c["id"], (names.get(c["id"]) or {}).get("name") or c["id"]) for c in d.get("comps") or []]
