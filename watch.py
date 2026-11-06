"""到站提醒：「2路到汽车总站叫我」——后台盯着这条线，快到站了在群里喊一声。

口径照 Trackr（安卓「追客」）的 ArrivalRules / TrackingEngine：
- 只认「车正在开往的那一站」的站序：比目标靠前就是还有几站，正好是目标就是到站，
  靠后就是已经开过去（这趟没赶上）。方向靠站序表自己认，不用问用户往哪边坐。
- 问数据的节奏按剩余站数走：0~1 站 10 秒，2~4 站 20 秒，更远 45 秒；
  连续失败 ×2 退避，最多 120 秒；一次提醒最多盯 3 小时。
- 同一个车牌只喊一次「还有 N 站」，换了辆车才再喊。
纯标准库；查数据那步用 _q() 现拿 query，免得 query 和本模块循环 import。
"""

import json
import os
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch.json")
MAX_SECONDS = 3 * 3600        # 一次提醒最多盯 3 小时
NEAR_STOPS = 2                # 剩 2 站开始喊
TICK = 5                      # 主循环多久看一次有没有到点的


def _q():
    try:
        from . import query
    except ImportError:
        import query
    return query


def _rows():
    try:
        with open(DB, encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save(rows):
    try:
        with open(DB, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    except Exception:
        pass                  # 写不进去就当这条没记下，别把整轮提醒带崩


def _flag(want):
    return want if not want.isdigit() else want + "路"


def _idx(names, stn):
    """站名在站序里的位置：先认全等，再认包含（两家写法差一两个字）。"""
    for i, n in enumerate(names):
        if n == stn:
            return i
    for i, n in enumerate(names):
        if len(n) >= 2 and (stn in n or n in stn):
            return i
    return None


def _left(seqs, stn, stop):
    """车离目标站还剩几站；这个方向不经过（或对不上）就返回 None。"""
    for names in seqs.values():
        a, b = _idx(names, stn), _idx(names, stop)
        if a is not None and b is not None:
            return b - a
    return None


def _interval(left, fails=0):
    if fails:
        return min((10 if left is not None and left <= 1 else
                    20 if left is not None and left <= 4 else 45) << min(fails, 4), 120)
    if left is not None and left <= 1:
        return 10
    if left is not None and left <= 4:
        return 20
    return 45


def add(key, city_key, city_name, line, stop, seqs, by=""):
    rows = [r for r in _rows() if not (r["umo"] == key and r["line"] == line and r["stop"] == stop)]
    rows.append({"id": os.urandom(4).hex(), "umo": key, "key": city_key, "city": city_name,
                 "line": line, "stop": stop, "seqs": seqs, "by": by,
                 "created": int(time.time()), "next": 0, "fails": 0, "hit": []})
    _save(rows)
    till = time.strftime("%H:%M", time.localtime(time.time() + MAX_SECONDS))
    return (f"记下了：{_flag(line)} 往 {stop}，剩 {NEAR_STOPS} 站的时候喊你一声"
            f"（最多盯到 {till}，说「取消提醒」就不盯了）")


def stop(key, msg=""):
    rows = _rows()
    keep = [r for r in rows if r["umo"] != key]
    m = _q().parse(msg)
    for r in rows:
        if r["umo"] == key and m and m[0] not in r["line"]:
            keep.append(r)              # 点了具体某条线，只撤那条
    removed = len(rows) - len(keep)
    _save(keep)
    return f"撤了 {removed} 条提醒" if removed else "这会儿没在盯什么"


def listing(key):
    rows = [r for r in _rows() if r["umo"] == key]
    if not rows:
        return "这会儿没在盯什么。说「2路到汽车总站叫我」就开始盯"
    return "正在盯：\n" + "\n".join(
        f"{_flag(r['line'])} 往 {r['stop']}，{time.strftime('%H:%M', time.localtime(r['created']))} 开始"
        for r in rows)


def _check(row):
    """查一次实时，返回 (要说的话 or "", 还留不留, 下次几秒后)。"""
    left, plate, stn, dist = None, "", "", 0
    try:
        res = _q().query(row["key"], [row["line"]])
    except Exception:
        return "", True, _interval(None, row.get("fails", 0) + 1)
    for _, buses in res.get("groups") or []:
        for b in buses:
            plate0, stn0 = b[0], b[1]
            dist0 = b[2] if len(b) > 2 else 0
            l0 = _left(row["seqs"], stn0, row["stop"])
            if l0 is None:
                continue
            if left is None or l0 < left:
                left, plate, stn, dist = l0, plate0, stn0, dist0
    if left is None:
        return "", True, _interval(None, row.get("fails", 0))       # 这线这会儿没报车，接着盯
    km = ""
    try:
        d = float(dist or 0)
        km = f"（{d / 1000:.1f}km）" if d >= 1000 else (f"（{round(d)}m）" if d > 0 else "")
    except (TypeError, ValueError):
        pass
    line = _flag(row["line"])
    if left == 0:
        return f"{line} 到 {row['stop']} 了：{plate} 正在进站，快上车", False, 0
    if left < 0:
        # 这一趟都开过去了（还有车在后面就轮不到这条分支），说清楚是没赶上，别装成到站
        return (f"{line} 刚开过 {row['stop']}，这趟没赶上（{plate} 现在在 {stn}）；"
                f"要等下一班就说一句「{line}到{row['stop']}叫我」"), False, 0
    if left <= NEAR_STOPS and plate not in row.get("hit", []):
        row.setdefault("hit", []).append(plate)
        return f"{line} 还有 {left} 站到 {row['stop']}：{plate} 现在在 {stn}{km}", True, _interval(left)
    return "", True, _interval(left)


def due():
    """把到点的提醒都查一遍，返回要发的话。全部同步，调用方丢线程里跑。"""
    rows, out = _rows(), []
    now = int(time.time())
    keep = []
    for row in rows:
        if now - row["created"] > MAX_SECONDS:
            out.append((row, f"{_flag(row['line'])} 这条盯了 3 小时还没等到，先撤了"))
            continue
        if row.get("next", 0) > now:
            keep.append(row)
            continue
        text, alive, delay = _check(row)
        if text:
            out.append((row, text))
        if alive:
            row["next"] = int(time.time()) + delay
            keep.append(row)
    if len(keep) != len(rows) or out:
        _save(keep)
    return out
