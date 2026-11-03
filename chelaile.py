"""车来了 H5：目录 + 实时车辆。协议细节见 docs/车来了API.md（本仓姊妹项目）。

两个坑：
  src 必须能路由到该城，跨城统一用 app，填别城渠道会静默返「等待发车」。
  lineId 与 cityId 必须配对，错配直接 status=0002。
"""

import base64
import hashlib
import json
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://web.chelaile.net.cn/api/"
UA = "Mozilla/5.0 (Linux; Android 13) Mobile MicroMessenger/8.0"
REFERER = "https://web.chelaile.net.cn/customer_ch5/"
KEY = b"422556651C7F7B2B5C266EED06068230"
SALT = "qwihrnbtmj"
SRC = "app"


def _get(handler, params):
    url = BASE + handler + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    return urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")


def _unwrap(text):
    body = json.loads(text.split("**YGKJ", 1)[1].rsplit("YGKJ##", 1)[0])
    jr = body["jsonr"]
    if jr.get("status") != "00":
        raise RuntimeError(jr.get("errmsg") or jr.get("status"))
    return jr["data"]


def _params(city_id, **extra):
    # shortcut: v/vc/sign 是客户端版本占位，上游当前不校验；版本升级导致失败时改这里
    p = {"s": "h5", "v": "9.1.2", "vc": 1, "sign": 1, "userId": "", "h5Id": "",
         "cityId": city_id, "src": SRC}
    p.update(extra)
    return p


def lines(city_id):
    """[(lineName, lineId)]，一条线一次（目录里是方向级记录，这里去重）。"""
    data = _unwrap(_get("bus/cityLineList", _params(city_id)))
    seen = {}
    for row in data["allLines"]["all"]:
        seen.setdefault(row["lineName"], row["lineId"])
    return list(seen.items())


def _decrypt(blob):
    dec = Cipher(algorithms.AES(KEY), modes.ECB()).decryptor()
    raw = dec.update(base64.b64decode(blob)) + dec.finalize()
    return json.loads(raw[: -raw[-1]].decode("utf-8"))


def live(city_id, line_id):
    """实时车辆 → {"state": int, "buses": [(车号, 下一站, 距离米)]}"""
    payload = {"lineId": line_id}
    t = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sign = hashlib.md5((t[1:-1].replace(":", "=").replace(",", "&") + SALT).encode()).hexdigest()
    body = _unwrap(_get("bus/line!encryptedLineDetail.action",
                        _params(city_id, lineId=line_id, cryptoSign=sign)))
    d = _decrypt(body["encryptResult"])
    stops = d.get("stations") or []
    buses = []
    for b in d.get("buses") or []:
        order = b.get("order")
        # 车来了的 order 是 1 基站序，超出站表时就不报站名，只给距离
        station = stops[order - 1]["sn"] if isinstance(order, int) and 0 < order <= len(stops) else "途中"
        # 香港这类车队没有车牌，busId 形如 9_20261103203011，取前段当车号
        plate = (b.get("licence") or "").split("_")[0] or str(b.get("busId") or "?").split("_")[0]
        buses.append((plate, station,
                      b.get("distanceToWaitStn")))
    return {"state": d["line"].get("state"), "buses": buses}
