"""高德地图工具：地点搜索与路线规划，产出统一 MapPayload 供前端渲染。

后端 REST 直连高德 Web 服务（v3，免 SDK，需 .env 配 AMAP_API_KEY，平台类型选「Web 服务」）。
坐标全程 GCJ-02，与前端高德瓦片同源，无偏移。

MapPayload 结构（前端 static/index.html 的 renderMap 依赖此契约）：
    type: poi | route
    title: 标题
    center: [lng, lat]
    bbox: [[minLng, minLat], [maxLng, maxLat]]   前端 fitBounds 自适应视野
    markers: [{name, lng, lat, desc, order}]
    path: [[lng, lat], ...]                      route 模式才有
    fallback_url: 高德 URI 深链，免 key，API 失效时保底

旁路与 rag_engine.last_sources 同模式：文本给 LLM，payload 经 last_map() 给接口层。
"""

import json
import os
from typing import List, Optional
from urllib.parse import quote

import requests
from langchain_core.tools import tool

AMAP_BASE = "https://restapi.amap.com/v3"
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# 层级：core/tools/map_guide.py -> tools -> core -> backend -> 项目根
_BACKEND_DIR = os.path.dirname(os.path.dirname(_TOOLS_DIR))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
CACHE_PATH = os.path.join(_PROJECT_DIR, "data", "amap_geo_cache.json")
TIMEOUT = 15

_geo_cache = {}
_last_map = None


# ============ 高德 REST ============

def _api(path: str, **params) -> dict:
    key = os.getenv("AMAP_API_KEY")
    if not key:
        raise RuntimeError("未配置 AMAP_API_KEY")
    params["key"] = key
    resp = requests.get(f"{AMAP_BASE}/{path}", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("status")) != "1":
        raise RuntimeError(f"高德接口返回失败: {data.get('info', 'unknown')}")
    return data


def _resolve(name: str, city: str = "") -> dict:
    """地名 → {lng, lat, poi_id, address, pname}，带本地缓存（坐标基本不变）"""
    cache_key = f"{name}|{city or ''}"
    if cache_key in _load_cache():
        return _geo_cache[cache_key]
    params = {"keywords": name, "offset": 1, "page": 1}
    if city:
        params["city"] = city
    pois = _api("place/text", **params).get("pois") or []
    if not pois:
        raise ValueError(f"未找到地点「{name}」")
    poi = pois[0]
    lng, lat = poi["location"].split(",")
    entry = {
        "lng": float(lng), "lat": float(lat),
        "poi_id": poi.get("id", ""),
        "address": poi.get("address") or "",
        "pname": poi.get("pname") or "",
    }
    _geo_cache[cache_key] = entry
    _save_cache()
    return entry


def _poi_extra(poi_id: str) -> str:
    """开放时间/参考花费等深度信息；该字段部分账号权限下拿不到，失败返回空串"""
    if not poi_id:
        return ""
    try:
        pois = _api("place/detail", id=poi_id).get("pois") or []
        if not pois:
            return ""
        p = pois[0]
        bits = []
        opentime = p.get("opentime") or (p.get("biz_ext") or {}).get("opentime")
        if opentime:
            bits.append(f"开放时间：{opentime}")
        cost = (p.get("biz_ext") or {}).get("cost")
        if cost:
            bits.append(f"参考花费：{cost}")
        return "；".join(bits)
    except Exception:
        return ""


# ============ 地名缓存持久化 ============

def _load_cache() -> dict:
    if not _geo_cache and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                _geo_cache.update(json.load(f))
        except Exception:
            pass
    return _geo_cache


def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_geo_cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"⚠️ 地理编码缓存写入失败: {e}")


# ============ MapPayload ============

def _finish(ptype: str, title: str, markers: List[dict],
            path: Optional[List[list]] = None, fallback_url: str = "") -> dict:
    """组装 MapPayload 并存入旁路"""
    global _last_map
    lons = [m["lng"] for m in markers]
    lats = [m["lat"] for m in markers]
    if path:
        lons += [p[0] for p in path]
        lats += [p[1] for p in path]
    anchor = markers[-1] if markers else {"lng": 116.397, "lat": 39.909}
    _last_map = {
        "type": ptype,
        "title": title,
        "center": [anchor["lng"], anchor["lat"]],
        "bbox": [[min(lons), min(lats)], [max(lons), max(lats)]] if lons else None,
        "markers": markers,
        "path": path,
        "fallback_url": fallback_url,
    }
    return _last_map


def last_map() -> Optional[dict]:
    """旁路出口：最近一次 MapPayload，供接口层随响应返回前端"""
    return _last_map


def _human_dur(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "未知时长"
    h, m = sec // 3600, sec % 3600 // 60
    return (f"{h}小时{m}分" if h else f"{m}分钟") if sec else "未知时长"


def _human_dist(meter) -> str:
    try:
        return f"{int(meter) / 1000:.1f}公里"
    except (TypeError, ValueError):
        return "未知距离"


def _parse_polyline(text: str) -> List[list]:
    pts = []
    for pair in (text or "").split(";"):
        if "," in pair:
            lng, lat = pair.split(",", 1)
            try:
                pts.append([float(lng), float(lat)])
            except ValueError:
                continue
    return pts


# ============ 工具一：地点搜索 ============

@tool("map_poi_search")
def map_poi_search(name: str, city: str = "") -> str:
    """查询地点在地图上的位置与详情（地址、开放时间、门票），地图将展示在前端。
    用户询问某景点/餐厅/设施在哪里、开放时间、门票价格时调用。
    name: 地点名称，如 天坛公园; city: 所在城市如 北京，跨城市有重名时必填"""
    try:
        loc = _resolve(name, city)
    except Exception as e:
        return f"【地图查询失败：{e}】请如实告知用户暂时无法在地图上定位该地点，不要编造坐标。"
    detail = _poi_extra(loc["poi_id"])
    _finish("poi", name,
            [{"name": name, "lng": loc["lng"], "lat": loc["lat"],
              "desc": loc["address"] or loc["pname"], "order": None}],
            fallback_url=f"https://uri.amap.com/marker?position={loc['lng']},{loc['lat']}&name={quote(name)}")
    lines = [f"「{name}」已在地图上标记（{loc['pname'] or city or '高德'} {loc['address']}）。"]
    if detail:
        lines.append(detail)
    lines.append("地图已在前端展示，无需复述坐标，请基于以上信息自然讲解。")
    return "\n".join(lines)


# ============ 工具二：路线规划 ============

_MODE_NAME = {"transit": "公交/地铁", "driving": "驾车", "walking": "步行"}
_MODE_URI = {"transit": "bus", "driving": "car", "walking": "walk"}


def _route_lines(mode: str, origin: dict, dest: dict, city: str):
    """返回 (摘要行, path 坐标)"""
    o, d = f"{origin['lng']},{origin['lat']}", f"{dest['lng']},{dest['lat']}"
    if mode == "transit":
        route = _api("direction/transit/integrated", origin=o, destination=d,
                     city=city or origin["pname"] or "北京", strategy=0).get("route") or {}
        transits = route.get("transits") or []
        if not transits:
            raise ValueError("未找到公交换乘方案")
        best = transits[0]
        pts, seg = [], []
        for s in best.get("segments") or []:
            for st in (s.get("walking") or {}).get("steps") or []:
                pts += _parse_polyline(st.get("polyline", ""))
            for ln in ((s.get("bus") or {}).get("buslines") or []):
                seg.append(ln.get("name", ""))
                pts += _parse_polyline(ln.get("polyline", ""))
        lines = [f"公交/地铁：约{_human_dur(best.get('duration'))}，全程{_human_dist(route.get('distance'))}"]
        if seg:
            lines.append("乘车：乘 " + " → 换乘 ".join(seg))
        return lines, pts
    if mode == "driving":
        data = _api("direction/driving", origin=o, destination=d, strategy=0)
        path0 = ((data.get("route") or {}).get("paths") or [{}])[0]
        pts = []
        for st in path0.get("steps") or []:
            pts += _parse_polyline(st.get("polyline", ""))
        return [f"驾车：约{_human_dur(path0.get('duration'))}，{_human_dist(path0.get('distance'))}"], pts
    if mode == "walking":
        data = _api("direction/walking", origin=o, destination=d)
        path0 = ((data.get("route") or {}).get("paths") or [{}])[0]
        pts = []
        for st in path0.get("steps") or []:
            pts += _parse_polyline(st.get("polyline", ""))
        return [f"步行：约{_human_dur(path0.get('duration'))}，{_human_dist(path0.get('distance'))}"], pts
    raise ValueError(f"不支持的出行方式: {mode}")


@tool("map_route_plan")
def map_route_plan(from_place: str, to_place: str, mode: str = "transit", city: str = "") -> str:
    """规划两地之间的出行路线并在前端展示地图，返回耗时、距离与换乘信息。
    用户询问怎么去、如何到达、交通路线、行程怎么安排时调用。
    from_place: 起点名称; to_place: 终点名称; mode: transit/driving/walking 之一，默认 transit;
    city: 所在城市（公交换乘建议填起点城市，如 北京）"""
    mode = mode if mode in _MODE_NAME else "transit"
    try:
        origin = _resolve(from_place, city)
        dest = _resolve(to_place, city)
        lines, path = _route_lines(mode, origin, dest, city)
    except Exception as e:
        return (f"【路线规划失败：{e}】请如实告知用户暂时无法查询该路线，"
                f"建议用地图App自行查询，不要编造耗时与票价。")
    fallback = (f"https://uri.amap.com/navigation?from={origin['lng']},{origin['lat']},{quote(from_place)}"
                f"&to={dest['lng']},{dest['lat']},{quote(to_place)}&mode={_MODE_URI[mode]}"
                f"&policy=1&src=tour-guide-ai&coordinate=gaode&callnative=0")
    _finish("route", f"{from_place} → {to_place}",
            [{"name": f"起点·{from_place}", "lng": origin["lng"], "lat": origin["lat"], "desc": "", "order": 0},
             {"name": f"终点·{to_place}", "lng": dest["lng"], "lat": dest["lat"], "desc": "", "order": 1}],
            path=path, fallback_url=fallback)
    lines.append(f"「{_MODE_NAME[mode]}」路线地图已在前端展示，请自然讲解，不要逐字复述坐标。")
    return "\n".join(lines)


MAP_TOOLS = [map_poi_search, map_route_plan]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(map_poi_search.invoke({"name": "天坛公园", "city": "北京"}))
    print(json.dumps(last_map(), ensure_ascii=False, indent=1))
