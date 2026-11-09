import io


def load(p):
    s = io.open(p, encoding='utf-8', newline='').read()
    return s.replace('\r\n', '\n')


def save(p, s):
    io.open(p, 'w', encoding='utf-8', newline='').write(s.replace('\n', '\r\n'))


# 1) mygolbs：上游回的错别按一小时缓存
p = 'mygolbs.py'
s = load(p)
old = '''    val = fn()
    # 网络抖动别进缓存：catalog 一挂就是一小时查不到车，全是这条缓存留的
    if isinstance(val, dict) and val.get("error") and "detail" in val:
        return val'''
new = '''    val = fn()
    # 网络抖动别进缓存：catalog 一挂就是一小时查不到车，全是这条缓存留的
    if isinstance(val, dict) and val.get("error") and "detail" in val:
        return val
    # 上游自己回的错（status 非 1，比如「当前城市暂无法提供公交查询」）以前照 ttl 存一小时：
    # 武汉那次目录偏了一下，后续一小时内所有线路都走了「目录里没这条」的路
    if isinstance(val, dict) and val.get("error"):
        keep = min(ttl, ERR_TTL)
        cache[(kind, key)] = (time.time() - (ttl - keep), val)
        return val'''
assert s.count(old) == 1, 'mygolbs cached'
s = s.replace(old, new, 1)

old2 = '''CACHE_MAX = 2048'''
new2 = '''CACHE_MAX = 2048
# 出错响应在缓存里最多留这么久（秒）：宁可多问一次上游，也别让一次抖动顶一小时
ERR_TTL = 30'''
assert s.count(old2) == 1, 'mygolbs ERRT'
s = s.replace(old2, new2, 1)
save(p, s)

# 2) query：目录缺这条线时，用关键词搜索（CMD=114）再问一次上游
p = 'query.py'
s = load(p)
old3 = '''    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    if not hit:'''
new3 = '''    hit = _pick_city_line([(r["lineName"], r) for r in rows], want, city_name)
    if not hit:
        # 目录（CMD=119）偶尔给的是残缺/过期的一份：只凭它就说「没这条」会把数据源缺口说成
        # 事实错误（武汉 325 路那次就这么被答成「两家实时都没报来车」）。
        # 关键词搜索（CMD=114）按 KEYWORD 直接给行，带 to/upperOrDown，能当目录用
        try:
            alt = [r for r in mygolbs.lines(city_name, want) or [] if r.get("lineName")]
        except Exception:
            alt = []
        if alt:
            hit = _pick_city_line([(r["lineName"], r) for r in alt], want, city_name)
            if hit:
                rows = alt'''
assert s.count(old3) == 1, 'query _mygolbs'
s = s.replace(old3, new3, 1)
save(p, s)
print('patched')
