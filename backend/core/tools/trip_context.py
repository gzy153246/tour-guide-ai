"""行程实时上下文采集：转场时长 / 天气 / 景点开放状态。

定位：行程规划链路的工具（与 map_guide / rag_engine 等并列在 core/tools/ 下）。

与 core/tools/map_guide.py 的分工：
    map_guide  面向「单条问答」——一次查一个点/一条路线，结果写 last_map 旁路交给 LLM
    本模块     面向「整份行程」——一次批量取回几十个景点对的转场矩阵、多天天气、开放时间，
               带独立磁盘缓存与总预算控制。两者取数维度不同，故不互相 import，
               但 itinerary.build_trip_map() 仍会复用 map_guide 的地理编码。

三条降级底线：
    1. 未配 AMAP_API_KEY / 接口报错 / 超时 → 退回经验估算或返回 None，绝不把异常抛给调用方
    2. 一次规划的实时查询有总预算（默认 8 秒），超时就放弃剩余查询
    3. 所有外部结果落盘缓存，第二次起不再消耗配额

能查到什么 / 查不到什么（诚实边界）：
    转场时长   可查，准实时（高德路径规划，含路况）
    天气       可查，仅覆盖「今天起 4 天」（高德预报上限），第 5 天起为 None
    开放时间   可查景区公布的常规开放时间（biz_ext.opentime2），含淡旺季/日期区间多段表述
    临时闭馆   查不到——高德无闭馆公告接口，只能靠官方渠道，代码里如实标注为「未知」
"""

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---- 路径：core/tools/trip_context.py → tools → core → backend → 项目根 ----
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
_DATA_DIR = os.path.join(_PROJECT_DIR, "data")
_TRIP_DIR = os.path.join(_DATA_DIR, "trip")          # 行程侧数据与缓存的分组目录

ROUTE_CACHE_PATH = os.path.join(_TRIP_DIR, "route_cache.json")
WEATHER_CACHE_PATH = os.path.join(_TRIP_DIR, "weather_cache.json")
POI_CACHE_PATH = os.path.join(_TRIP_DIR, "poi_cache.json")
HOLIDAY_PATH = os.path.join(_TRIP_DIR, "holidays.json")
# 人工校正表：高德数据与本地标注冲突时，以此为准（见文件内说明）
OVERRIDE_PATH = os.path.join(_TRIP_DIR, "attraction_overrides.json")

AMAP_BASE = os.getenv("AMAP_BASE", "https://restapi.amap.com/v3")

# 总开关：置 0 则完全退回纯静态骨架，不发任何外部请求
ENABLED = os.getenv("TRIP_REALTIME", "1").strip().lower() not in ("0", "false", "no", "off")
# 一次规划的实时查询总预算（秒）
BUDGET_SEC = float(os.getenv("TRIP_CTX_BUDGET", "8"))
HTTP_TIMEOUT = float(os.getenv("TRIP_CTX_HTTP_TIMEOUT", "6"))
# 高德 Web 服务对并发敏感（CUQPS 超限会被拒），默认保守并发 + 最小请求间隔
MAX_WORKERS = int(os.getenv("TRIP_CTX_WORKERS", "4"))
MIN_INTERVAL = float(os.getenv("TRIP_CTX_MIN_INTERVAL", "0.25"))
# 天气缓存有效期（秒）：预报一天一变，没必要每次都查
WEATHER_TTL = int(os.getenv("TRIP_CTX_WEATHER_TTL", "3600"))
# 转场缓存有效期（秒）：0 表示永久有效（景点坐标固定，路线基本稳定）
ROUTE_TTL = int(os.getenv("TRIP_CTX_ROUTE_TTL", "0"))

# 降级用的经验速度（km/h）与固定开销（分钟）：等车/换乘、找车位、进出站
_FALLBACK_SPEED = {"transit": 22.0, "driving": 28.0, "walking": 4.5}
_FALLBACK_OVERHEAD = {"transit": 12, "driving": 8, "walking": 0}

# 天气分级：good 适宜室外 / mild 一般 / bad 建议转室内
_BAD_WEATHER = ("雨", "雪", "雷", "冰雹", "冻")
_GOOD_WEATHER = ("晴", "多云")

_cache_lock = threading.Lock()
_route_cache: Dict[str, dict] = {}
_weather_cache: Dict[str, dict] = {}
_poi_cache: Dict[str, dict] = {}
_holiday_cache: Optional[dict] = None
_loaded = False


# ============================ 缓存持久化 ============================

_SCHEMA = 1  # 解析逻辑变了就 +1，旧缓存自动作废，避免拿过期结构当新数据


def _load_all() -> None:
    """首次访问时把三份缓存读进内存；读不到或版本不符就当空缓存，不影响任何功能"""
    global _loaded
    if _loaded:
        return
    with _cache_lock:
        if _loaded:
            return
        for path, bag in ((ROUTE_CACHE_PATH, _route_cache),
                           (WEATHER_CACHE_PATH, _weather_cache),
                           (POI_CACHE_PATH, _poi_cache)):
            name = os.path.basename(path)
            try:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and raw.get("_schema") == _SCHEMA:
                    bag.update(raw.get("data") or {})
                else:
                    print(f"ℹ️  缓存 {name} 版本不匹配（schema {_SCHEMA}），本次忽略并重建")
            except Exception as e:
                print(f"⚠️  行程上下文缓存读取失败（{name}）: {e}")
        _loaded = True


def _save(path: str, bag: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _cache_lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_schema": _SCHEMA, "data": bag}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"⚠️  行程上下文缓存写入失败（{os.path.basename(path)}）: {e}")


# ============================ 高德 REST ============================

_req_lock = threading.Lock()
_last_req = [0.0]


def _amap(path: str, _retry: int = 0, **params) -> dict:
    key = os.getenv("AMAP_API_KEY")
    if not key:
        raise RuntimeError("未配置 AMAP_API_KEY")
    params["key"] = key
    with _req_lock:  # 全局串行节流，避免触发高德 QPS 限制
        wait = MIN_INTERVAL - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
    resp = requests.get(f"{AMAP_BASE}/{path}", params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("status")) != "1":
        info = str(data.get("info") or "")
        # QPS 超限是限流不是真失败，退避重试一次即可，别把整趟行程拖成估算
        if "CUQPS" in info.upper() and _retry < 2:
            time.sleep(1.0 * (_retry + 1))
            return _amap(path, _retry=_retry + 1, **params)
        raise RuntimeError(f"高德返回失败: {info or 'unknown'}")
    return data


def _haversine_km(a: dict, b: dict) -> float:
    """两点球面距离（km），用于降级估算"""
    try:
        lat1, lng1 = math.radians(a["lat"]), math.radians(a["lng"])
        lat2, lng2 = math.radians(b["lat"]), math.radians(b["lng"])
    except (KeyError, TypeError):
        return 0.0
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _estimate_minutes(a: dict, b: dict, mode: str) -> int:
    """经验估算：直线距离 × 绕路系数 ÷ 速度 + 固定开销"""
    km = _haversine_km(a, b) * 1.35  # 1.35 是路网相对直线的经验绕路系数
    speed = _FALLBACK_SPEED.get(mode, 22.0)
    return int(round(km / speed * 60 + _FALLBACK_OVERHEAD.get(mode, 10)))


# ============================ 转场时长 ============================

def _route_key(a: dict, b: dict, mode: str) -> str:
    return f"{mode}|{a['lng']:.5f},{a['lat']:.5f}|{b['lng']:.5f},{b['lat']:.5f}"


def _fetch_transfer(a: dict, b: dict, mode: str) -> dict:
    """调高德路径规划拿真实耗时；返回 {'minutes','distance_m','source'}"""
    o, d = f"{a['lng']},{a['lat']}", f"{b['lng']},{b['lat']}"
    if mode == "transit":
        data = _amap("direction/transit/integrated", origin=o, destination=d,
                     city=os.getenv("TRIP_CITY", "北京"), strategy=0)
        route = data.get("route") or {}
        transits = route.get("transits") or []
        if not transits:
            raise ValueError("无公交换乘方案")
        best = transits[0]
        minutes = int(best.get("duration") or 0) // 60
        distance = int(route.get("distance") or 0)
    else:
        api = "direction/driving" if mode == "driving" else "direction/walking"
        data = _amap(api, origin=o, destination=d)
        path0 = ((data.get("route") or {}).get("paths") or [{}])[0]
        minutes = int(path0.get("duration") or 0) // 60
        distance = int(path0.get("distance") or 0)
    if minutes <= 0:
        raise ValueError("高德未返回有效耗时")
    return {"minutes": minutes, "distance_m": distance, "source": "amap"}


def transfer(a: dict, b: dict, mode: str = "transit") -> dict:
    """两地点之间的转场时长。

    :return: {'minutes': int, 'distance_m': int, 'source': 'amap' | 'estimate'}
    任何失败都会退回经验估算，调用方永远拿得到一个可用值。
    """
    if not (a.get("lat") and a.get("lng") and b.get("lat") and b.get("lng")):
        return {"minutes": 20, "distance_m": 0, "source": "estimate"}
    if not ENABLED:
        return {"minutes": _estimate_minutes(a, b, mode), "distance_m": 0, "source": "estimate"}

    _load_all()
    key = _route_key(a, b, mode)
    hit = _route_cache.get(key)
    if hit and (ROUTE_TTL <= 0 or time.time() - hit.get("_ts", 0) < ROUTE_TTL):
        return {"minutes": hit["minutes"], "distance_m": hit.get("distance_m", 0),
                "source": hit.get("source", "amap")}
    try:
        info = _fetch_transfer(a, b, mode)
        _route_cache[key] = {**info, "_ts": time.time()}
        _save(ROUTE_CACHE_PATH, _route_cache)
        return info
    except Exception as e:
        print(f"⚠️  转场时长查询失败，改用估算: {e}")
        return {"minutes": _estimate_minutes(a, b, mode), "distance_m": 0, "source": "estimate"}


# ============================ 天气 ============================

def _weather_grade(text: str) -> str:
    t = text or ""
    if any(w in t for w in _BAD_WEATHER):
        return "bad"
    if any(w in t for w in _GOOD_WEATHER):
        return "good"
    return "mild"


def weather(city: Optional[str] = None) -> Dict[str, dict]:
    """查询城市天气预报。

    :return: {'2026-10-21': {'day': '晴', 'night': '多云', 'grade': 'good',
                             'temp': '18/9℃'}, ...}
    高德预报只覆盖「今天起 4 天」，超出的日期不会出现在结果里（调用方按 None 处理）。
    """
    if not ENABLED:
        return {}
    _load_all()
    city = city or os.getenv("TRIP_CITY", "北京")
    hit = _weather_cache.get(city)
    if hit and time.time() - hit.get("_ts", 0) < WEATHER_TTL:
        return {k: v for k, v in hit.items() if k != "_ts"}
    try:
        data = _amap("weather/weatherInfo", city=city, extensions="all")
        out: Dict[str, dict] = {}
        # 注意层级：forecasts 是城市（通常 1 条），真正的逐日预报在 forecasts[].casts[]
        for f in data.get("forecasts") or []:
            for c in f.get("casts") or []:
                date = c.get("date")
                if not date:
                    continue
                day = c.get("dayweather") or ""
                out[str(date)] = {
                    "day": day,
                    "night": c.get("nightweather") or "",
                    "grade": _weather_grade(day),
                    "temp": f"{c.get('daytemp', '')}/{c.get('nighttemp', '')}℃",
                }
        _weather_cache[city] = {**out, "_ts": time.time()}
        _save(WEATHER_CACHE_PATH, _weather_cache)
        return out
    except Exception as e:
        print(f"⚠️  天气查询失败，本次行程不做天气适配: {e}")
        return {}


# ============================ 景点开放状态 ============================

_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WINDOW_CN = {"dawn": "清晨", "morning": "上午", "noon": "中午",
              "afternoon": "下午", "evening": "晚间", "whole": "全天"}
# 「周一全天关闭」「周二闭馆」这类表述；[^\s，。；]{0,8}? 限制跨度，避免误吃「周二至周日开放」
_CLOSED_RE = re.compile(r"周([一二三四五六日天])[^\s，。；]{0,8}?(?:全天关闭|关闭|闭馆|不开放|休息)")


def _closed_days(text: str) -> List[int]:
    """从开放时间文本里解析固定闭馆日，解析不出返回空列表（≠ 不闭馆，只是不知道）"""
    return sorted({_WEEKDAY_MAP[m] for m in _CLOSED_RE.findall(text or "")
                   if m in _WEEKDAY_MAP})


def _closed_words(text: str) -> bool:
    """开放时间文本里出现整体停业的信号（不是每周固定闭馆，而是暂时不开放）"""
    t = text or ""
    return any(w in t for w in ("暂停开放", "暂不开放", "停止开放", "闭园整改"))


# ============================ 开放时间的条件段解析（淡旺季 / 日期区间） ============================
#
# 景区公布的开放时间常常是多段条件表述，直接取第一个时间会取错季节。实测样本：
#   旺季4月1日至10月31日 周二至周日 08:30-17:00 …；淡季11月1日至次年3月31日 … 08:30-16:30
#   11月1日至次年5月31日周二至周日09:00-17:00；6月1日至10月31日周二至周日09:00-17:30
#   淡季:06:30-20:00,旺季:06:00-21:00
#   暑期7月6号-8月30号全天开放 09:00-19:00；周二全天关闭…周一、周三至周日:09:00-16:30
# 所以按「时间区间 + 它前面那句条件描述」配对，再按出行日期挑出生效的那一段。

# 时间区间：兼容全角冒号、en dash、波浪号
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
# 月日区间：4月1日至10月31日 / 11月1日-次年3月31日 / 4月至10月（缺日按月初月末近似）
_MD_RANGE_RE = re.compile(
    r"(?:次年)?(\d{1,2})\s*月\s*(?:(\d{1,2})\s*[日号]\s*)?"
    r"(?:至|到|-|~|—)\s*(?:次年)?(\d{1,2})\s*月\s*(?:(\d{1,2})\s*[日号]\s*)?")
# 斜杠日期：07/01-08/31
_SLASH_RANGE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*(?:-|~|至)\s*(\d{1,2})\s*/\s*(\d{1,2})")
_SEASON_RE = re.compile(r"(旺季|淡季|暑期)")

# 北京景区惯例（跨年区间用 r1 > r2 表示）：旺季 4-10 月，淡季 11-3 月，暑期 7-8 月
_SEASON_RANGE = {
    "旺季": ((4, 1), (10, 31)),
    "淡季": ((11, 1), (3, 31)),
    "暑期": ((7, 1), (8, 31)),
}


def _in_md_range(md: Tuple[int, int], r1: Tuple[int, int], r2: Tuple[int, int]) -> bool:
    """判断 (月,日) 是否落在区间内，支持跨年（如 11月1日 → 次年3月31日）"""
    if r1 <= r2:
        return r1 <= md <= r2
    return md >= r1 or md <= r2


def parse_open_spans(text: str) -> List[dict]:
    """把开放时间原文拆成带条件的时段列表。

    :return: [{'start': 分钟, 'end': 分钟, 'range': ((m,d),(m,d))|None,
               'season': '旺季'|'淡季'|'暑期'|None, 'cond': 原文条件片段}]
    """
    spans: List[dict] = []
    for m in re.finditer(r"([^；;。]{0,40}?)" + _TIME_RE.pattern, text or ""):
        cond = m.group(1) or ""
        h1, mi1, h2, mi2 = (int(m.group(i)) for i in (2, 3, 4, 5))
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and mi1 < 60 and mi2 < 60):
            continue
        start, end = h1 * 60 + mi1, h2 * 60 + mi2
        if end < start:
            end += 1440                      # 跨夜营业（如 18:00-02:00）
        if end - start < 30:
            continue                         # 太短的当作噪音（多是「最晚入馆」时刻）
        rng = None
        md = _MD_RANGE_RE.search(cond)
        if md:
            rng = ((int(md.group(1)), int(md.group(2) or 1)),
                   (int(md.group(3)), int(md.group(4) or 31)))
        else:
            sl = _SLASH_RANGE_RE.search(cond)
            if sl:
                rng = ((int(sl.group(1)), int(sl.group(2))),
                       (int(sl.group(3)), int(sl.group(4))))
        season = _SEASON_RE.search(cond)
        spans.append({
            "start": start, "end": end, "range": rng,
            "season": season.group(1) if season else None,
            "cond": cond.strip(),
        })
    return spans


def open_span_for_date(text: str, month: int, day: int) -> Optional[Tuple[int, int]]:
    """按出行日期挑出生效的开放区间，返回 (开始分钟, 结束分钟)；解析不出返回 None。

    优先级：日期区间命中的段 > 淡旺季命中 > 无条件通用段。
    一个景点在同一天命中多段时取并集的边界（偏宽松，宁可少砍也不错砍）。
    """
    spans = parse_open_spans(text)
    if not spans:
        return None
    md = (month, day)
    dated, seasonal, generic = [], [], []
    for s in spans:
        if s["range"]:
            if _in_md_range(md, *s["range"]):
                dated.append(s)
        elif s["season"]:
            if _in_md_range(md, *_SEASON_RANGE[s["season"]]):
                seasonal.append(s)
        else:
            generic.append(s)
    pool = dated or seasonal or generic or spans
    return (min(s["start"] for s in pool), max(s["end"] for s in pool))


# ============================ 闭馆口径：人工校正表 ============================
#
# 高德的 opentime2 与本地 attractions.json 的 closed_weekday 实测有 8 处冲突，
# 且两边各有错（高德有过时数据，本地表覆盖不全）。联网查证后的结论写进校正表，
# 优先级高于两者，避免每次规划都重新查一次网络。

_override_cache: Dict[str, Any] = {"mtime": 0.0, "data": {}}


def overrides() -> dict:
    """读取 data/trip/attraction_overrides.json（按 mtime 自动失效，改完即生效）

    格式：
      {"景点名": {"closed_weekday": [0], "note": "…", "source": "…", "verified": "2026-10-21"}}
    """
    try:
        mtime = os.path.getmtime(OVERRIDE_PATH)
    except OSError:
        return {}
    if _override_cache["data"] and _override_cache["mtime"] == mtime:
        return _override_cache["data"]
    try:
        with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        print(f"⚠️  校正表读取失败（{os.path.basename(OVERRIDE_PATH)}）: {e}")
        data = {}
    _override_cache["mtime"] = mtime
    _override_cache["data"] = data
    return data


def closed_days(name: str, status: Optional[dict] = None,
                static_closed: Optional[List[int]] = None) -> List[int]:
    """合并三套闭馆口径。

    优先级：人工校正表 >（本地标注 ∪ 高德解析）。
    没有校正值时取并集——宁可少排一个真闭馆的替代品，也不冒「排了却进不去」的风险。
    """
    ov = overrides().get(name) or {}
    if isinstance(ov.get("closed_weekday"), list):
        return sorted({int(x) for x in ov["closed_weekday"] if 0 <= int(x) <= 6})
    api = set((status or {}).get("closed_days") or [])
    st = set(int(x) for x in (static_closed or []))
    return sorted(api | st)


def attraction_status(name: str, city: Optional[str] = None) -> dict:
    """查询景点的常规开放时间。

    高德把开放时间放在 biz_ext.opentime2（不是 opentime，实测 opentime 恒为 null），
    文本里常直接写明「周一全天关闭」，正好可以解析成固定闭馆日。

    :return: {'opentime': str, 'closed_days': [int], 'source': 'amap' | 'unknown'}
             opentime 为空表示查不到，调用方应如实提示而非臆断。
    """
    empty = {"opentime": "", "closed_days": [], "source": "unknown"}
    if not ENABLED:
        return {**empty, "source": "disabled"}
    _load_all()
    city = city or os.getenv("TRIP_CITY", "北京")
    key = f"{name}|{city}"
    if key in _poi_cache:
        return _poi_cache[key]
    result = dict(empty)
    try:
        pois = _amap("place/text", keywords=name, city=city, offset=1, page=1).get("pois") or []
        if not pois:
            _poi_cache[key] = result
            _save(POI_CACHE_PATH, _poi_cache)
            return result
        biz = pois[0].get("biz_ext") or {}
        opentime = (biz.get("opentime2") or pois[0].get("opentime")
                    or biz.get("opentime") or "")
        result = {
            "opentime": opentime,
            "closed_days": _closed_days(opentime),
            "source": "amap" if opentime else "unknown",
        }
    except Exception as e:
        print(f"⚠️  景点开放信息查询失败（{name}）: {e}")
    _poi_cache[key] = result
    _save(POI_CACHE_PATH, _poi_cache)
    return result


def holidays() -> dict:
    """法定节假日表（可选）。

    数据放 data/holidays.json，格式 {"2026-10-01": "国庆节"}。
    不存在就是空表——宁可不识别，也不硬编码一份会过期的日历。
    命中节假日时，博物馆「周一闭馆」这类规则不适用。
    """
    global _holiday_cache
    if _holiday_cache is None:
        try:
            if os.path.exists(HOLIDAY_PATH):
                with open(HOLIDAY_PATH, "r", encoding="utf-8") as f:
                    _holiday_cache = json.load(f)
            else:
                _holiday_cache = {}
        except Exception as e:
            print(f"⚠️  节假日表读取失败: {e}")
            _holiday_cache = {}
    return _holiday_cache


# ============================ 一次规划的共享上下文 ============================

class TripContext:
    """一次行程规划内的实时数据容器：共享缓存、并发取数、总预算兜底。

    plan_trip 里这样用：
        ctx = TripContext(transport="bus")
        info = ctx.transfer(prev_item, next_item)     # 单个
        ctx.batch_status(["故宫博物院", "颐和园"])     # 批量并发
        ctx.weather_for_date("2026-10-22")
        ctx.notes                                     # 采集过程中的降级说明
    """

    _MODE_MAP = {"walk": "walking", "bus": "transit", "car": "driving"}

    def __init__(self, transport: str = "bus", city: Optional[str] = None,
                 budget: Optional[float] = None):
        self.mode = self._MODE_MAP.get(transport, "transit")
        self.city = city or os.getenv("TRIP_CITY", "北京")
        self.budget = BUDGET_SEC if budget is None else budget
        self.deadline = time.time() + self.budget
        self.notes: List[str] = []
        self.enabled = ENABLED
        self._weather: Optional[Dict[str, dict]] = None
        self._weather_loaded = False
        self.stats = {"amap_calls": 0, "estimated": 0, "timeout": False}

    # ---- 预算 ----
    def _left(self) -> float:
        return self.deadline - time.time()

    def _spend(self, fn, *args) -> Any:
        """在剩余预算内执行；预算耗尽就放弃，不阻塞规划"""
        if self._left() <= 0:
            self.stats["timeout"] = True
            return None
        try:
            return fn(*args)
        except Exception as e:
            print(f"⚠️  实时上下文采集异常: {e}")
            return None

    # ---- 转场 ----
    def transfer(self, a: dict, b: dict) -> dict:
        def job():
            info = transfer(a, b, self.mode)
            if info["source"] == "amap":
                self.stats["amap_calls"] += 1
            else:
                self.stats["estimated"] += 1
            return info
        got = self._spend(job)
        if got is None:
            est = {"minutes": _estimate_minutes(a, b, self.mode),
                   "distance_m": 0, "source": "estimate"}
            self.stats["estimated"] += 1
            return est
        return got

    def batch_transfer(self, pairs: List[Tuple[Any, dict, dict]]) -> Dict[Any, dict]:
        """并发取多段转场时长。pairs = [(key, a, b), ...]，返回 {key: info}"""
        out: Dict[Any, dict] = {}
        if not pairs:
            return out
        todo = [(k, a, b) for k, a, b in pairs]
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as ex:
            futures = {ex.submit(self.transfer, a, b): k for k, a, b in todo}
            for fut, key in futures.items():
                left = self._left()
                if left <= 0:
                    self.stats["timeout"] = True
                    break
                try:
                    out[key] = fut.result(timeout=max(0.1, left))
                except Exception:
                    self.stats["timeout"] = True
        return out

    # ---- 天气 ----
    def weather_for_date(self, date_str: str) -> Optional[dict]:
        if not self._weather_loaded:
            self._weather = self._spend(weather, self.city) or {}
            self._weather_loaded = True
        return (self._weather or {}).get(date_str)

    def weather_all(self) -> Dict[str, dict]:
        if not self._weather_loaded:
            self._weather = self._spend(weather, self.city) or {}
            self._weather_loaded = True
        return self._weather or {}

    # ---- 开放状态 ----
    def status(self, name: str, weekday: Optional[int] = None,
               static_closed: Optional[List[int]] = None,
               month: Optional[int] = None, day: Optional[int] = None) -> dict:
        """查开放时间，并结合目标星期 / 出行日期给出当天是否开放与生效的开放区间。

        open 三态：True 当天开放 / False 当天不开放 / None 查不到（如实标注，不臆断）
        span      : 按出行日期从原文里挑出的生效区间 (开始分钟, 结束分钟)，
                    会区分淡旺季与日期区间；挑不出来为 None（不做硬约束）
        """
        got = self._spend(attraction_status, name, self.city) or {}
        opentime = got.get("opentime", "")
        cd = closed_days(name, got, static_closed)
        open_flag: Optional[bool] = None
        if _closed_words(opentime):
            open_flag = False          # 文本明说暂停/停止开放
        elif cd and weekday is not None:
            open_flag = weekday not in cd
        span = open_span_for_date(opentime, month, day) if (
            opentime and month and day) else None
        return {"open": open_flag, "opentime": opentime, "closed_days": cd,
                "span": span, "source": got.get("source", "unknown")}

    def batch_status(self, names: List[str], weekday: Optional[int] = None,
                     static_map: Optional[Dict[str, List[int]]] = None,
                     month: Optional[int] = None,
                     day: Optional[int] = None) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        uniq = [n for n in dict.fromkeys(names) if n]
        if not uniq:
            return out
        smap = static_map or {}
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(uniq))) as ex:
            futures = {ex.submit(self.status, n, weekday, smap.get(n), month, day): n
                       for n in uniq}
            for fut, name in futures.items():
                left = self._left()
                if left <= 0:
                    self.stats["timeout"] = True
                    break
                try:
                    out[name] = fut.result(timeout=max(0.1, left))
                except Exception:
                    out[name] = {"open": None, "opentime": "", "closed_days": [],
                                 "span": None, "source": "unknown"}
        return out

    # ---- 节假日 ----
    def is_holiday(self, date_str: str) -> bool:
        return date_str in holidays()

    def finish(self) -> List[str]:
        """收尾：把采集过程中的降级情况整理成用户可见的说明"""
        if not self.enabled:
            return ["实时路况与天气适配已关闭（TRIP_REALTIME=0），当前为静态推荐。"]
        if self.stats["timeout"]:
            self.notes.append(
                f"实时信息查询超出 {self.budget:.0f} 秒预算，未覆盖到的路段已按经验时长估算。")
        if self.stats["estimated"] and not self.stats["amap_calls"]:
            self.notes.append("未能获取实时路况，转场时间按经验估算，实际请以地图 App 为准。")
        return self.notes


# ============================ 供 Agent 调用的工具包装 ============================
#
# 上面的 TripContext 是「批量采集器」，面向整份行程（一次取几十个景点对）。
# 规划 Agent 需要的是「问一个点、答一个数」的细粒度工具，所以这里再包一层。
# langchain 是可选依赖：未安装时下面三个仍是普通函数，不影响行程规划主流程。

_ATTR_PATH = os.path.join(_PROJECT_DIR, "data", "attractions.json")
_attr_cache: Dict[str, Any] = {"mtime": 0.0, "index": {}}


def _attr_index() -> Dict[str, dict]:
    """景点名 -> 景点条目（含 lat/lng），按文件 mtime 自动失效"""
    try:
        mtime = os.path.getmtime(_ATTR_PATH)
    except OSError:
        return {}
    if _attr_cache["index"] and _attr_cache["mtime"] == mtime:
        return _attr_cache["index"]
    try:
        with open(_ATTR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        idx = {a["name"]: a for a in data.get("attractions", []) if a.get("name")}
    except Exception as e:
        print(f"⚠️  景点库读取失败（{os.path.basename(_ATTR_PATH)}）: {e}")
        idx = {}
    _attr_cache["mtime"] = mtime
    _attr_cache["index"] = idx
    return idx


def query_transfer_time(origin: str, destination: str, transport: str = "bus") -> str:
    """查询北京两个景点之间的真实通行时长与距离。

    行程排期要用真实转场时间，不能假设景点之间瞬移。
    transport 取 walk（步行）/ bus（公交地铁，默认）/ car（自驾）。
    """
    idx = _attr_index()
    a, b = idx.get(origin), idx.get(destination)
    if not a or not b:
        miss = [n for n, v in ((origin, a), (destination, b)) if not v]
        return (f"景点库中没有 {'、'.join(miss)}，无法计算通行时间。"
                f"请改用 data/attractions.json 中已有的景点名。")
    mode = {"walk": "walking", "bus": "transit", "car": "driving"}.get(transport, "transit")
    info = transfer(a, b, mode)
    km = f"{info.get('distance_m', 0) / 1000:.1f}" if info.get("distance_m") else "未知"
    src = "高德实时路径规划" if info.get("source") == "amap" else "经验估算（未取到实时路况）"
    return f"{origin} → {destination}：约 {info.get('minutes')} 分钟，{km} 公里（{src}）。"


def query_weather(date: str = "", city: str = "") -> str:
    """查询北京的天气预报，返回指定日期的天气、气温与适宜度。

    date 用 YYYY-MM-DD；留空则返回未来几天的全部预报。
    注意：高德预报只覆盖「今天起 4 天」，更远的日期查不到，不要猜测。
    """
    city = city or os.getenv("TRIP_CITY", "北京")
    try:
        wmap = weather(city) or {}
    except Exception as e:
        return f"天气查询失败：{e}"
    if not wmap:
        return "未能获取天气预报，请按常规天气准备行程。"
    if date:
        w = wmap.get(date)
        if not w:
            return (f"查不到 {date} 的预报（高德只提供今天起 4 天），"
                    f"可选日期：{'、'.join(sorted(wmap)[:4])}。")
        return f"{date} {city}：{w.get('day')}（夜间 {w.get('night')}），{w.get('temp')}，适宜度 {w.get('grade')}。"
    lines = [f"{d}：{w.get('day')} {w.get('temp')}" for d, w in sorted(wmap.items())]
    return f"{city}天气预报：\n" + "\n".join(lines)


def query_open_hours(attraction: str, date: str = "") -> str:
    """查询北京某景点的开放时间与固定闭馆日。

    date 用 YYYY-MM-DD；给出日期时会按淡旺季/日期区间解析出当天实际生效的营业区间。
    临时闭馆公告查不到（高德无此接口），涉及出行请提示用户以官方渠道为准。
    """
    if not ENABLED:
        return "实时信息已关闭（TRIP_REALTIME=0），无法查询开放时间。"
    st = attraction_status(attraction, os.getenv("TRIP_CITY", "北京"))
    if not st.get("opentime"):
        return f"未查到「{attraction}」的开放时间，请建议用户咨询官方渠道。"
    closed = st.get("closed_days") or []
    cd = "、".join(_WEEKDAY_NAMES[i] for i in closed if 0 <= i < 7) or "无固定闭馆日"
    out = f"「{attraction}」开放时间：{st['opentime']}\n固定闭馆：{cd}"
    if date and len(date) >= 10:
        try:
            span = open_span_for_date(st["opentime"], int(date[5:7]), int(date[8:10]))
        except ValueError:
            span = None
        if span:
            hm = lambda x: f"{x // 60 % 24:02d}:{x % 60:02d}"
            out += f"\n{date} 生效区间：{hm(span[0])}-{hm(span[1])}"
    return out


def query_attraction(name: str) -> str:
    """查询景点档案：区域、适合时段、建议游览时长、票价、是否室内、固定闭馆日。

    判断「这个点能不能放进某个时段」「换点该换成谁」之前先查它，
    不要凭印象猜——档案来自 data/attractions.json，是排班的唯一事实来源。
    """
    idx = _attr_index()
    hit = idx.get(name)
    if not hit:
        near = [n for n in idx if name and (name in n or n in name)][:5]
        tip = f"，你是否想查 {'、'.join(near)}" if near else "，请改用库中的标准名"
        return f"景点库中没有「{name}」{tip}。"
    win = "、".join(_WINDOW_CN.get(w, w) for w in (hit.get("windows") or [])) or "未标注"
    closed = "、".join(_WEEKDAY_NAMES[i] for i in (hit.get("closed_weekday") or [])
                      if 0 <= i < 7) or "无固定闭馆日"
    tags = "、".join(hit.get("tags") or []) or "无"
    return (f"「{hit['name']}」：位于{hit.get('area', '未知')}片区，适合{win}，"
            f"建议 {hit.get('duration') or 0} 小时，门票 {hit.get('ticket') or 0} 元，"
            f"{'室内' if hit.get('indoor') else '室外'}，{'整日项目' if hit.get('whole_day') else '常规景点'}；"
            f"固定闭馆：{closed}；标签：{tags}。")


try:
    from langchain_core.tools import tool as _lc_tool
    query_transfer_time = _lc_tool(query_transfer_time)
    query_weather = _lc_tool(query_weather)
    query_open_hours = _lc_tool(query_open_hours)
    query_attraction = _lc_tool(query_attraction)
except Exception:  # 没装 langchain 时保持为普通函数，主流程不受影响
    pass


# 供 Agent 注册： TRIP_CONTEXT_TOOLS 与 TRIP_TOOLS / MAP_TOOLS 同级
TRIP_CONTEXT_TOOLS = [query_transfer_time, query_weather, query_open_hours, query_attraction]
