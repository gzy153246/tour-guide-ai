"""行程编排引擎（确定性骨架 + LLM 文案润色）
职责边界：
  - 这里只做「生成行程」这一件事，不认识 FastAPI，可 `python core/trip/itinerary.py` 单跑
  - 候选景点来自 data/attractions.json，改动该文件无需重启之外的操作（按 mtime 自动失效）
  - 地图点位是惰性产物：build_trip_map() 只在前端要看图时才调用
设计取舍：
  - 需求先交给规划 Agent（core/trip/agent.py）理解成结构化约束，本文件只消费约束不读原话
  - 骨架（选哪些点、放哪天、什么时段、多少钱）全部由规则算出，稳定、可复现、毫秒级
  - 生成后再过一遍 _apply_real_world()：按真实转场时长重排时间轴、按天气换室内场馆、
    核查开放状态。该步依赖外部接口，有落盘缓存 + 并发 + 总预算三重兜底，失败退回骨架
  - 实时上下文全部由 core/tools/trip_context.py 提供，本文件不直接碰高德 API
  - LLM 只润色 desc / tip 文案，且失败不破坏结构（polished=False 时前端照常渲染）
  - 宁可少排并如实提示，也不编造未知景点/价格
对外：
    plan_trip(params: dict) -> dict
    build_trip_map(plan: dict, day_key: str) -> dict
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 保证以任意工作目录启动时都能导入 core 包
# 层数：core/trip/itinerary.py -> trip -> core -> backend -> 项目根
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)                                # 项目根，data 在这里
if _BACKEND_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _BACKEND_DIR)

from core.prompts.trip import TRIP_POLISH_PROMPT  # noqa: E402
from core.prompts.shared import extract_json  # noqa: E402
from core.tools.trip_context import TripContext, open_span_for_date  # noqa: E402

ATTR_PATH = os.path.join(_PROJECT_DIR, "data", "attractions.json")

FIXED_DEST = "北京市"
MAX_DAYS = 5
MAX_PEOPLE = 20

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 每个时段的可用时间格：morning/afternoon 各能放 2 个景点
WINDOW_TIME = {
    "morning": ["08:30 - 10:30", "10:40 - 12:10"],
    "noon": ["12:20 - 13:20"],
    "afternoon": ["13:30 - 15:20", "15:40 - 17:30"],
    "evening": ["18:30 - 20:30"],
}
WINDOW_NAME = {"dawn": "清晨", "morning": "上午", "noon": "中午", "afternoon": "下午",
               "evening": "晚间", "whole": "全天"}

# 需求解析出来的时间提示词 → 中文说法（用于备注文案）
_HINT_CN = {"dawn": "清晨", "morning": "上午", "noon": "中午",
            "afternoon": "下午", "evening": "傍晚/夜间"}

# 北京日出时刻锚点（年内第几天 -> 日出分钟数），用于「看升旗 / 看日出」这类需求。
# 线性插值即可，误差几分钟对行程安排无影响，不额外消耗 API。
_SUNRISE_ANCHORS = [(15, 440), (46, 412), (74, 372), (105, 329), (135, 297), (166, 285),
                    (196, 302), (227, 328), (258, 357), (288, 386), (319, 415), (349, 436)]

# 每日景点容量（不含餐饮）
PACE_CONF = {
    "relax": {"name": "轻松", "cap": {"morning": 1, "afternoon": 1, "evening": 0},
              "note": "每天 2 个主景点，留足休息与拍照时间"},
    "normal": {"name": "适中", "cap": {"morning": 1, "afternoon": 1, "evening": 1},
               "note": "每天 3 个主景点，行程饱满但不赶"},
    "tight": {"name": "紧凑", "cap": {"morning": 2, "afternoon": 2, "evening": 1},
              "note": "每天最多 5 个主景点，适合体力充沛的打卡型选手"},
}

TRANS_CONF = {
    "walk": {"name": "徒步为主", "tip": "全程步行最能感受城市肌理，请穿舒适的鞋并预留转场时间，地铁+步行混搭更省力。"},
    "bus": {"name": "公共交通", "tip": "热门景区建议地铁直达，高峰期比打车快且省心；远郊线路可提前查好旅游专线。"},
    "car": {"name": "自驾", "tip": "自驾需提前查好停车场与尾号限行；城区核心景区建议换乘地铁，避免堵在二环。"},
}

DIET_ADVICE = {
    "清真": "餐饮已优先安排清真餐厅，进店前仍建议确认清真标识。",
    "素食": "素食餐厅选择有限，本行程已优先标注素食用餐地，其余可询问店内素食套餐。",
    "清淡少辣": "点餐时可主动说明少油少辣，京菜偏咸，多要一份清水涮着吃。",
    "无辣不欢": "簋街、麻辣类餐厅等位高峰在 18:30 后，建议错峰或提前线上取号。",
    "海鲜过敏": "海鲜过敏请避开自助海鲜与小摊贝类，随身携带抗过敏药。",
    "不吃牛肉": "点菜前确认锅底与配菜成分，北京涮肉、卤煮多为牛羊肉。",
    "低糖低盐": "老字号小吃普遍偏甜偏咸，建议少尝多样，正餐选择清炒类。",
}

INTEREST_NAMES = {"history": "历史人文", "nature": "自然风光", "photo": "摄影打卡",
                  "food": "美食寻味", "family": "亲子休闲"}

BUDGET_TIERS = [
    (300, "经济", "青旅 / 连锁快捷酒店（150-300 元/晚）"),
    (700, "舒适", "市区四星酒店（400-700 元/晚）"),
    (float("inf"), "高端", "五星或精品酒店（1000 元以上/晚）"),
]

# ============================ 候选目录加载 ============================

_catalog_cache: Dict[str, Any] = {"mtime": 0.0, "data": None}


def load_catalog() -> Tuple[List[dict], List[dict], dict]:
    """返回 (景点池, 餐饮池, 元数据)；按文件 mtime 自动失效，无需重启"""
    try:
        mtime = os.path.getmtime(ATTR_PATH)
    except OSError as e:
        raise RuntimeError(f"候选目录不可读：{ATTR_PATH}（{e}）")
    if _catalog_cache["data"] and _catalog_cache["mtime"] == mtime:
        data = _catalog_cache["data"]
    else:
        with open(ATTR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _catalog_cache["mtime"] = mtime
        _catalog_cache["data"] = data
    spots = [a for a in data.get("attractions", []) if a.get("kind", "spot") == "spot"]
    foods = [a for a in data.get("attractions", []) if a.get("kind") == "food"]
    meta = {"city": data.get("city", FIXED_DEST), "areas": data.get("areas", {})}
    return spots, foods, meta


# ============================ 参数与打分 ============================

def _score(item: dict, interests: List[str]) -> float:
    tags = set(item.get("tags") or [])
    s = 0.0
    for i in interests:
        if i in tags:
            s += 6
    if "classic" in tags:
        s += 3
    s += 4 - int(item.get("rank", 3) or 3)
    return s


def _open_on(item: dict, weekday: int) -> bool:
    return weekday not in (item.get("closed_weekday") or [])


def _date_list(start: datetime.date, days: int) -> List[datetime.date]:
    return [start + timedelta(days=i) for i in range(days)]


def normalize_params(params: dict) -> dict:
    raw_date = str(params.get("date") or "").strip()
    try:
        start = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("出发日期格式不正确，应为 YYYY-MM-DD")

    days = int(params.get("days") or 2)
    if days < 1 or days > MAX_DAYS:
        raise ValueError(f"游玩天数需在 1 - {MAX_DAYS} 天之间")

    people = int(params.get("people") or 2)
    if people < 1 or people > MAX_PEOPLE:
        raise ValueError(f"出行人数需在 1 - {MAX_PEOPLE} 人之间")

    budget = float(params.get("budgetAmount") or 0)
    if budget < 0:
        raise ValueError("预算不能为负数")

    pace = str(params.get("pace") or "normal")
    if pace not in PACE_CONF:
        pace = "normal"
    transport = str(params.get("transport") or "bus")
    if transport not in TRANS_CONF:
        transport = "bus"

    interests = [k for k in (params.get("interests") or []) if k in INTEREST_NAMES]
    if not interests:
        interests = ["history"]

    diet = [d for d in (params.get("diet") or []) if d and d != "不忌口"]
    return {
        "origin": str(params.get("origin") or "北京市区").strip() or "北京市区",
        "start": start, "days": days, "people": people,
        "budget": budget, "pace": pace, "transport": transport,
        "interests": interests, "diet": diet,
        "extra": str(params.get("extra") or "").strip(),
        "polish": bool(params.get("polish", True)),
    }


# ============================ 骨架编排 ============================

def _pick_day_spots(pool: List[dict], used: set, weekday: int,
                    need: int, prefer_area: str = "") -> List[dict]:
    """挑出某天的景点：先按区域聚拢（少跑冤枉路），再按兴趣分排序"""
    avail = [a for a in pool if a["name"] not in used and _open_on(a, weekday)]
    if not avail or need <= 0:
        return []

    area_weight: Dict[str, float] = {}
    for a in avail:
        area_weight[a["area"]] = area_weight.get(a["area"], 0.0) + a["_score"] + 1.0
    anchor = prefer_area if prefer_area in area_weight else max(area_weight, key=area_weight.get)

    avail.sort(key=lambda a: (0 if a["area"] == anchor else 1, -a["_score"], a["name"]))
    return avail[:need]


def _pick_meal(foods: List[dict], area: str, meal_count: Dict[str, int],
               diet: List[str], window: str) -> Optional[dict]:
    """挑一顿饭：同区域优先，能匹配忌口优先，尽量不重复"""
    cands = [f for f in foods if window in (f.get("windows") or [])]
    if not cands:
        cands = foods
    if not cands:
        return None

    def rank(f: dict) -> tuple:
        hit = len([d for d in diet if d in (f.get("diet_tags") or [])])
        return (0 if f["area"] == area else 1, -hit, meal_count.get(f["name"], 0), f["name"])

    cands.sort(key=rank)
    return cands[0]


def _slot(item: dict, slot_key: str, time_text: str, desc: str = "", tip: str = "",
         must: bool = False) -> dict:
    return {
        "slot": slot_key,
        "slotName": WINDOW_NAME.get(slot_key, slot_key),
        "time": time_text,
        "name": item["name"],
        "kind": item.get("kind", "spot"),
        "must": must,
        "desc": desc or item.get("desc", ""),
        "tip": tip or item.get("tip", ""),
        "duration": item.get("duration", 0),
        "ticket": item.get("ticket", 0) or 0,
        "area": item.get("area", ""),
        # 地图去重键：全天项目分上下两段时共用同一个原始名称
        "mapKey": item.get("mapKey", item["name"]),
    }


def build_plan_days(req: dict, spots: List[dict], foods: List[dict],
                    extra_req: Optional[dict] = None) -> Tuple[List[dict], Dict[str, int], List[str]]:
    """返回 (planDays, 餐饮计数, 提示语)

    extra_req 来自规划 Agent（core/trip/agent.py），降级时来自 core/trip/agentkit/tool_extra.py：
      must  = [{"name": 库内标准名, "time_hint": "dawn|morning|...", "reason": str}]
      avoid = [{"name": 库内标准名, "reason": str}]
    没有额外需求时传 None，行为与此前完全一致。
    """
    cap = PACE_CONF[req["pace"]]["cap"]
    per_day = cap["morning"] + cap["afternoon"] + cap["evening"]
    dates = _date_list(req["start"], req["days"])
    weekdays = [d.weekday() for d in dates]

    # 用户点名不去的景点，连替补池都不进
    avoid = {str(a.get("name") or "") for a in (extra_req or {}).get("avoid") or []}
    base = [a for a in spots if a["name"] not in avoid]

    scored = []
    for a in base:
        item = dict(a)
        item["_score"] = _score(a, req["interests"])
        scored.append(item)
    scored.sort(key=lambda a: (-a["_score"], a["name"]))
    whole_items = [a for a in scored if a.get("whole_day") and a["_score"] >= 7.0]

    # 用户点名要去的景点：dawn 类走附加的清晨档，其余强制占掉正常的当日名额
    must_all = [dict(m) for m in (extra_req or {}).get("must") or []]
    by_name = {a["name"]: a for a in scored}
    must_dawn: List[dict] = []      # 清晨/日出类，单独一格，不占常规容量
    must_queue: List[dict] = []     # 其余必去，按天补进 picked
    for m in must_all:
        item = by_name.get(m.get("name") or "")
        if not item:
            continue
        m["_item"] = item
        if m.get("time_hint") == "dawn":
            must_dawn.append(m)
        else:
            must_queue.append(m)

    used: set = set()
    meal_count: Dict[str, int] = {}
    notes: List[str] = []

    # 主题乐园占用一整天。用户点名的优先占掉一天（不然会被塞进两小时的时段格），
    # 否则只在行程够长或明确亲子时才给，避免"沾标签就吞掉一天"
    whole_day_index = -1
    whole_item = None
    whole_must = False
    must_whole = next((m for m in must_queue if m["_item"].get("whole_day")
                       and m["_item"]["name"] not in used), None)
    if must_whole:
        whole_item, whole_must = must_whole["_item"], True
        must_queue.remove(must_whole)
    elif whole_items and (req["days"] >= 4 or "family" in req["interests"]):
        pick = next((a for a in whole_items if any(_open_on(a, w) for w in weekdays)), None)
        if pick:
            whole_item = pick
    if whole_item:
        used.add(whole_item["name"])
        whole_day_index = req["days"] - 1

    plan_days: List[dict] = []
    for i, day_date in enumerate(dates):
        wd = weekdays[i]
        if i == whole_day_index and whole_item:
            day_area = whole_item["area"]
            plan_days.append(_whole_day_block(i, day_date, whole_item, foods, meal_count,
                                             req, must=whole_must))
            plan_days[-1]["area"] = day_area
            continue

        # ① 清晨档（升旗 / 日出）：附加位，不占用当天常规名额，当天开放即可排
        dawn_slots: List[Tuple[dict, str]] = []
        for m in list(must_dawn):
            item = m["_item"]
            if item["name"] in used:
                must_dawn.remove(m)
                continue
            if not _open_on(item, wd):
                continue                       # 当天闭馆，顺延到下一天再试
            dawn_slots.append((item, m.get("reason", "")))
            must_dawn.remove(m)
            used.add(item["name"])

        # ② 其余必去景点优先占位，剩下的名额照常按兴趣/区域补齐
        locked: Dict[str, str] = {}
        must_picked: List[dict] = []
        for m in list(must_queue):
            item = m["_item"]
            if item["name"] in used:
                must_queue.remove(m)
                continue
            if not _open_on(item, wd):
                continue
            must_picked.append(item)
            locked[item["name"]] = m.get("time_hint") or ""
            must_queue.remove(m)
            used.add(item["name"])

        need = max(0, per_day - len(must_picked))
        rest = _pick_day_spots(scored, used, wd, need,
                               prefer_area="core" if i == 0 else "")
        rest = _ensure_window_fit(scored, rest, need, cap, used, locked=set(locked))
        picked = must_picked + rest
        for p in picked:
            used.add(p["name"])
        if len(picked) < per_day:
            notes.append(f"{day_date.strftime('%m-%d')} 当天可选的匹配景点较少，已减少安排，建议放宽兴趣标签或增加天数。")

        area = picked[0]["area"] if picked else "core"
        day = {
            "day": i + 1,
            "date": day_date.strftime("%Y-%m-%d"),
            "weekday": wd,
            "weekdayName": WEEKDAY_NAMES[wd],
            "area": area,
            "slots": [],
            "title": "",
        }
        day["slots"] = _layout_slots(picked, foods, cap, area, meal_count, req,
                                     dawn=dawn_slots, locked=locked, day_date=day_date)
        day["title"] = _day_title(i, req["days"], area, req["origin"], None)
        plan_days.append(day)

    # 排不进来的必去景点（全程闭馆等）如实告知，绝不假装已安排
    for m in must_dawn + must_queue:
        notes.append(f"您点名要去的「{m['name']}」在所选日期内无法安排（可能每日闭馆或行程天数不足），"
                     f"建议调整出发日期或增加天数。")
    if avoid:
        names = "、".join(sorted(avoid)[:5])
        notes.append(f"已按您的要求避开：{names}。")

    return plan_days, meal_count, notes


def _ensure_window_fit(pool: List[dict], picked: List[dict], need: int, cap: dict,
                       used: set, locked: Optional[set] = None) -> List[dict]:
    """挑中的景点必须撑得起当天要排的时段；缺哪个窗口就往候选池后面换一个能撑的进来

    否则会出现「晚间 18:30 去天坛」这种傍晚已闭园的安排。
    注意：替补必须从「此前几天都没用过」的候选里找，否则会造成跨天重复。
    locked 里的名字是用户点名要去的，不许被换成别的景点。
    """
    locked = locked or set()
    picked = list(picked)
    for window, k in (("morning", cap["morning"]), ("evening", cap["evening"])):
        if k <= 0 or not picked:
            continue
        if any(window in (a.get("windows") or []) for a in picked):
            continue
        victim = next((a for a in picked
                      if a["name"] not in locked
                      and window not in (a.get("windows") or [])), None)
        if victim is None:
            continue
        backup = next((a for a in pool
                       if a not in picked and a["name"] not in used
                       and window in (a.get("windows") or [])), None)
        if backup:
            picked[picked.index(victim)] = backup
    return picked


def _layout_slots(picked: List[dict], foods: List[dict], cap: dict, area: str,
                  meal_count: Dict[str, int], req: dict,
                  dawn: Optional[List[Tuple[dict, str]]] = None,
                  locked: Optional[Dict[str, str]] = None,
                  day_date=None) -> List[dict]:
    """把当天挑好的景点落位到 dawn → morning → noon → afternoon → evening

    dawn 只在用户明确提了「看升旗 / 看日出」这类需求时才存在，
    没有需求就不生成这一格（时间由需求语义决定，不是固定档位）。
    locked 是 {景点名: time_hint}，用户点名要去的景点会被锁到对应时段。
    """
    locked = locked or {}
    remaining = list(picked)
    slots: List[dict] = []

    # 清晨档：日出时刻按日期算，提前 40 分钟到场
    if dawn and day_date is not None:
        t = _dawn_range(day_date)
        for item, _reason in dawn:
            slots.append(_slot(item, "dawn", t, must=True))

    def take(window: str, count: int) -> List[Tuple[dict, str]]:
        """按窗口偏好取景点。

        - 优先取本身在该窗口开放的景点；
        - 用户点名要去的（locked）直接锁进它对应的时段；
        - 上午/下午只在「晚间另有专属候选」时才不吃掉早晚皆宜的景点，
          否则天安门这类 morning+evening 的景点会被一律推到晚上；
        - 晚间排不出合适的就不排，那一段留给晚餐或自由活动。
        """
        out: List[Tuple[dict, str]] = []
        for idx in range(count):
            times = WINDOW_TIME[window]
            t = times[min(idx, len(times) - 1)]
            forced = next((a for a in remaining
                           if locked.get(a["name"]) == window
                           and (window in (a.get("windows") or [])
                                or not a.get("windows"))), None)
            if forced is not None:
                remaining.remove(forced)
                out.append((forced, t))
                continue
            cands = [a for a in remaining if window in (a.get("windows") or [])]
            if not cands:
                if window == "evening":
                    break  # 没有适合晚间的安排，宁缺勿滥
                cands = list(remaining)
            elif window in ("morning", "afternoon") and len(cands) > 1:
                # 只有当「只能放晚间」的景点另有其人时，才把早晚皆宜的留给晚间
                pure_evening = [a for a in remaining
                                if "evening" in (a.get("windows") or [])
                                and "morning" not in (a.get("windows") or [])
                                and "afternoon" not in (a.get("windows") or [])]
                if pure_evening:
                    others = [a for a in cands if "evening" not in (a.get("windows") or [])]
                    if others:
                        cands = others
            if not cands:
                break
            choice = cands[0]
            remaining.remove(choice)
            out.append((choice, t))
        return out

    if not remaining:
        pass
    for item, t in take("morning", cap["morning"]):
        slots.append(_slot(item, "morning", t, must=item["name"] in locked))

    meal = _pick_meal(foods, area, meal_count, req["diet"], "noon")
    if meal:
        meal_count[meal["name"]] = meal_count.get(meal["name"], 0) + 1
        slots.append(_slot(meal, "noon", WINDOW_TIME["noon"][0]))

    for item, t in take("afternoon", cap["afternoon"]):
        slots.append(_slot(item, "afternoon", t, must=item["name"] in locked))

    evening_spot_filled = False
    for item, t in take("evening", cap["evening"]):
        slots.append(_slot(item, "evening", t, must=item["name"] in locked))
        evening_spot_filled = True

    if not evening_spot_filled:
        dinner = _pick_meal(foods, area, meal_count, req["diet"], "evening")
        if dinner:
            meal_count[dinner["name"]] = meal_count.get(dinner["name"], 0) + 1
            slots.append(_slot(dinner, "evening", WINDOW_TIME["evening"][0]))

    return slots


def _whole_day_block(idx: int, day_date, item: dict, foods: List[dict],
                     meal_count: Dict[str, int], req: dict, must: bool = False) -> dict:
    """整日项目（主题乐园）：上午入园 + 园区午餐 + 下午续玩"""
    slots = [_slot(item, "morning", "09:00 - 12:30",
                   item.get("desc", ""), item.get("tip", ""), must=must)]
    meal = _pick_meal(foods, item["area"], meal_count, req["diet"], "noon")
    if meal:
        meal_count[meal["name"]] = meal_count.get(meal["name"], 0) + 1
        slots.append(_slot(meal, "noon", WINDOW_TIME["noon"][0]))
    afternoon = _slot(
        item, "afternoon", "13:30 - 17:30",
        f"继续游览{item['name']}的其余园区，留意官方 App 上的演出与花车时刻。",
        "热门项目可用单次通道，闭园前两小时排队最快", must=must)
    afternoon["name"] = f"{item['name']} · 午后与演出"
    afternoon["mapKey"] = item["name"]
    slots.append(afternoon)
    return {
        "day": idx + 1,
        "date": day_date.strftime("%Y-%m-%d"),
        "weekday": day_date.weekday(),
        "weekdayName": WEEKDAY_NAMES[day_date.weekday()],
        "area": item["area"],
        "title": f"整日 · {item['name']}",
        "slots": slots,
    }


def _day_title(i: int, total: int, area: str, origin: str, meta: Optional[dict]) -> str:
    if i == 0:
        return f"启程 · 由 {origin} 出发"
    if i == total - 1 and total > 1:
        return "收官 · 精华补漏与返程"
    return f"深入 · 第 {i + 1} 天主线"


# ============================ 实时修正（转场 / 天气 / 开放状态） ============================
# 这一层是「骨架之后的校准」，只做三件事：
#   1. 按真实转场时长推进时间轴，排不下 21:00 的景点直接砍掉——宁可少排，不给走不完的行程
#   2. 恶劣天气把室外景点换成室内
#   3. 标注查到的开放时间；明确不开放的换点，查不到的如实标为未知
# 底线：任何一步失败都不改骨架、不抛异常，最多往 tips.notes 里加一句说明。

_DAY_END_MIN = 21 * 60  # 一天最晚排到 21:00


def _sunrise_minutes(day_date) -> int:
    """按日期估算北京日出时刻（分钟数），在 12 个锚点之间线性插值"""
    try:
        doy = int(day_date.strftime("%j"))
    except Exception:
        return 360
    pts = _SUNRISE_ANCHORS
    for i, (a_doy, a_min) in enumerate(pts):
        b_doy, b_min = pts[(i + 1) % len(pts)]
        if a_doy <= b_doy:
            if a_doy <= doy <= b_doy:
                if b_doy == a_doy:
                    return a_min
                return round(a_min + (b_min - a_min) * (doy - a_doy) / (b_doy - a_doy))
        elif doy >= a_doy or doy <= b_doy:      # 跨年那一段（12 月 → 次年 1 月）
            span = (365 - a_doy) + b_doy
            if span <= 0:
                return a_min
            off = (doy - a_doy) if doy >= a_doy else (365 - a_doy) + doy
            return round(a_min + (b_min - a_min) * off / span)
    return pts[0][1]


def _dawn_range(day_date) -> str:
    """看升旗 / 看日出这类需求的时间格：提前 40 分钟到场，仪式后留 30 分钟"""
    m = _sunrise_minutes(day_date)
    return _fmt_range(max(0, m - 40), m + 30)


def _parse_range(text: str) -> Optional[Tuple[int, int]]:
    """"08:30 - 10:30" → (510, 630)；解析不出就返回 None，交给调用方跳过"""
    try:
        parts = str(text).split("-")
        if len(parts) != 2:
            return None

        def to_min(p: str) -> int:
            h, m = p.strip().split(":")
            return int(h) * 60 + int(m)

        return to_min(parts[0]), to_min(parts[1])
    except Exception:
        return None


def _fmt_range(start: int, end: int) -> str:
    return f"{start // 60:02d}:{start % 60:02d} - {end // 60:02d}:{end % 60:02d}"


def _find_substitute(pool: List[dict], slot: dict, weekday: int, used: set,
                     require_indoor: Optional[bool] = None,
                     closed_map: Optional[Dict[str, List[int]]] = None) -> Optional[dict]:
    """从候选池找一个能顶替当前 slot 的景点：同时段、当天开放、没被占用

    closed_map 提供时用它的闭馆日（校正表 > 本地标注 ∪ 高德），
    否则退回景点自带的 closed_weekday——后者只覆盖了 11/42，容易挑出同样闭馆的替补。
    """
    window = slot.get("slot")
    for c in pool:
        if c.get("kind") != "spot" or c["name"] in used:
            continue
        closed = (closed_map.get(c["name"]) if closed_map
                  else (c.get("closed_weekday") or []))
        if weekday in (closed or []):
            continue
        if require_indoor is not None and bool(c.get("indoor")) != require_indoor:
            continue
        if window and window != "whole" and window not in (c.get("windows") or []):
            continue
        return c
    return None


def _swap_slot(slot: dict, new_item: dict, used: set, reason: str) -> None:
    """原地替换一个 slot 的景点内容（保持时段与坐标键一致）"""
    used.discard(slot.get("mapKey") or slot["name"])
    slot["name"] = new_item["name"]
    slot["mapKey"] = new_item["name"]
    slot["desc"] = new_item.get("desc", "")
    slot["tip"] = new_item.get("tip", "")
    slot["duration"] = new_item.get("duration", 0)
    slot["ticket"] = new_item.get("ticket", 0) or 0
    slot["area"] = new_item.get("area", "")
    slot["swapped"] = reason
    used.add(new_item["name"])


def _retime_day(day: dict, transfers: Dict[tuple, dict]) -> List[str]:
    """按真实转场时长推进一天的时间轴；撑不过 21:00 的景点直接删除

    transfers 的键是 (上一站名字, 本站名字)，所以中途删站也不会错位。
    :return: 被删除景点的说明列表
    """
    notes: List[str] = []
    for _ in range(2):  # 删站后后续基准会变，最多重排两轮
        slots = day["slots"]
        cursor_end: Optional[int] = None
        prev_name: Optional[str] = None
        drop_idx: Optional[int] = None
        for i, s in enumerate(slots):
            key = s.get("mapKey") or s.get("name", "")
            rng = _parse_range(s.get("time", ""))
            if rng is None:
                cursor_end = None
                prev_name = key
                continue
            st, en = rng
            if cursor_end is None or prev_name is None:
                cursor_end = en
                prev_name = key
                continue
            tf = transfers.get((prev_name, key)) or {}
            mins = int(tf.get("minutes") or 20)
            s["transfer"] = {"minutes": mins, "distance_m": tf.get("distance_m", 0),
                             "source": tf.get("source", "estimate")}
            need_start = cursor_end + mins
            if need_start <= st:
                cursor_end = en
                prev_name = key
                continue
            new_end = en + (need_start - st)
            # 用户点名要去的站点不砍（宁可后面挤一点），其余排不下就砍
            if new_end > _DAY_END_MIN and s.get("kind") != "food" and not s.get("must"):
                drop_idx = i  # 排不下就砍景点，餐饮不砍
                break
            s["time"] = _fmt_range(need_start, new_end)
            s["adjusted"] = True
            cursor_end = new_end
            prev_name = key
        if drop_idx is None:
            break
        dropped = slots.pop(drop_idx)
        notes.append(f"{day.get('date', '')} 「{dropped['name']}」与上一站转场过久，"
                     f"当天排不下，已去掉这一站。")
    return notes


def _fit_open_hours(day: dict, status_map: Dict[str, dict], md: Tuple[int, int],
                    spots: List[dict], used: set, weekday: int) -> List[str]:
    """把每站塞进它当天的真实开放区间（按出行日期区分淡旺季）。

    三种情况分别处理，都不硬截断游览时长：
      - 早于开门：整站顺延到开门时刻（后续站点由下一次重排吸收）
      - 晚于关门或压根不重叠：换一个当天开放且时段兼容的景点
      - 换不到：删掉这一站并如实说明，绝不排一个进不去的点
    """
    notes: List[str] = []
    if not md[0]:
        return notes
    month, day_no = md
    date_str = day.get("date", "")

    for s in list(day["slots"]):
        if s.get("kind") != "spot":
            continue
        key = s.get("mapKey") or s["name"]
        st = status_map.get(key) or {}
        if not st.get("opentime"):
            continue
        span = open_span_for_date(st["opentime"], month, day_no)
        if not span:
            continue
        o_start, o_end = span
        rng = _parse_range(s.get("time", ""))
        if not rng:
            continue
        st_min, en_min = rng
        need = max(30, int((s.get("duration") or 0) * 60) or (en_min - st_min))

        if st_min >= o_end:
            # 时段整体晚于关门：今天已经没机会安排了，只能换点或砍掉
            swap_or_drop = True
            new_start = st_min
        else:
            # 其余情况（早于开门、跨越关门）都先试着在时间轴内挪一挪
            new_start = max(st_min, o_start)
            swap_or_drop = new_start + need > o_end

        if swap_or_drop:
            sub = _find_substitute(spots, s, weekday, used,
                                   closed_map={k: (v.get("closed_days") or [])
                                               for k, v in status_map.items()})
            if sub:
                _swap_slot(s, sub, used, "open_hours")
                notes.append(f"{date_str} 「{key}」开放时间为 {st['opentime'][:24]}，"
                             f"该时段不营业，已替换为「{sub['name']}」。")
            else:
                day["slots"].remove(s)
                used.discard(key)
                notes.append(f"{date_str} 「{key}」开放时间为 {st['opentime'][:24]}，"
                             f"当天排不进营业时段，已去掉这一站。")
        elif new_start != st_min:
            s["time"] = _fmt_range(new_start, new_start + need)
            s["adjusted"] = True
            s["open_adjusted"] = True      # 与「按路况顺延」区分，前端分别提示
            verb = "顺延到开门后" if st_min < o_start else "提前到闭馆前"
            notes.append(f"{date_str} 「{key}」营业时间为 "
                         f"{o_start // 60:02d}:{o_start % 60:02d}-{o_end // 60 % 24:02d}:{o_end % 60:02d}，"
                         f"已把该站{verb}。")
    return notes


def _apply_real_world(plan: dict, req: dict, spots: List[dict], foods: List[dict]) -> None:
    """骨架生成后的一次性校准：天气换点 → 闭馆核查 → 转场重排 → 开放时段对齐

    纯增量逻辑，plan_trip 里整体 try 包住：出问题就退回原骨架，不会让行程页白屏。
    """
    ctx = TripContext(transport=req.get("transport", "bus"))
    weather_map = ctx.weather_all()
    index: Dict[str, dict] = {}
    for it in list(spots) + list(foods):
        index[it["name"]] = it

    used: set = set()
    for d in plan["planDays"]:
        for s in d["slots"]:
            used.add(s.get("mapKey") or s.get("name", ""))

    # 开放信息一次性取回（同一景点只查一次，多天复用），避免每天重复消耗配额。
    # weekday 不在这里传：是否闭馆按天判断，opentime/closed_days 与日期无关。
    spot_names = [s.get("mapKey") or s["name"]
                  for d in plan["planDays"] for s in d["slots"] if s.get("kind") == "spot"]
    status_map = ctx.batch_status(
        spot_names,
        static_map={it["name"]: list(it.get("closed_weekday") or []) for it in spots})
    # 统一口径后的闭馆日：人工校正 >（本地标注 ∪ 高德解析）；换点时也用这份
    closed_map: Dict[str, List[int]] = {n: (v.get("closed_days") or [])
                                        for n, v in status_map.items()}

    day_notes: List[str] = []
    weather_notes: List[str] = []

    for day in plan["planDays"]:
        date_str = day.get("date", "")
        weekday = day.get("weekday", 0)
        is_whole_day = str(day.get("title", "")).startswith("整日")
        w = weather_map.get(date_str)
        # 法定节假日不套用「周一闭馆」这类常规规则（表在 data/trip/holidays.json）
        is_holiday = ctx.is_holiday(date_str)
        try:
            md = (int(date_str[5:7]), int(date_str[8:10])) if len(date_str) >= 10 else (0, 0)
        except ValueError:
            md = (0, 0)

        # ① 恶劣天气：室外景点换室内（换不到就如实提示）
        if w and w.get("grade") == "bad" and not is_whole_day:
            for s in day["slots"]:
                if s.get("kind") != "spot":
                    continue
                item = index.get(s.get("mapKey") or s["name"], {})
                if item.get("indoor"):
                    continue
                # 用户点名要去的景点不因天气换掉，只提醒——换掉了等于没满足需求
                if s.get("must"):
                    weather_notes.append(
                        f"{date_str} 预报{w.get('day')}，「{s['name']}」是您点名要去的室外景点，"
                        f"建议携带雨具并提前确认户外项目是否照常。")
                    continue
                sub = _find_substitute(spots, s, weekday, used,
                                       require_indoor=True, closed_map=closed_map)
                if sub:
                    _swap_slot(s, sub, used, "bad_weather")
                else:
                    weather_notes.append(
                        f"{date_str} 预报{w.get('day')}，「{s['name']}」为室外景点，"
                        f"建议携带雨具或改选室内场馆。")
            weather_notes.append(f"{date_str} 天气：{w.get('day')} {w.get('temp', '')}")

        # ② 闭馆核查：当天不开放的换点；开放时间原文挂到 slot 上供前端展示
        spot_slots = [s for s in day["slots"] if s.get("kind") == "spot"]
        if spot_slots and not is_whole_day:
            for s in spot_slots:
                name = s.get("mapKey") or s["name"]
                st = status_map.get(name) or {}
                if st.get("opentime"):
                    s["opentime"] = st["opentime"]
                # open=False 表示原文明说暂停/停止开放，与节假日无关，一律换点
                paused = st.get("open") is False
                closed_today = (not is_holiday and weekday in (st.get("closed_days") or []))
                if paused or closed_today:
                    sub = _find_substitute(spots, s, weekday, used, closed_map=closed_map)
                    if sub:
                        _swap_slot(s, sub, used, "closed")
                        day_notes.append(
                            f"{date_str} 「{name}」当天不开放，已替换为「{sub['name']}」。")
                    else:
                        day_notes.append(
                            f"{date_str} 查询到「{name}」可能不开放，"
                            f"出发前请在官方渠道确认，避免白跑一趟。")

        # ③ 转场重排：按真实耗时推进时间轴
        seq = [index.get(s.get("mapKey") or s.get("name", ""), {}) for s in day["slots"]]
        pairs = [((seq[i - 1]["name"], seq[i]["name"]), seq[i - 1], seq[i])
                 for i in range(1, len(seq))
                 if seq[i - 1].get("lat") and seq[i].get("lat")]
        if pairs:
            transfers = ctx.batch_transfer(pairs)
            day_notes.extend(_retime_day(day, transfers))

        # ④ 开放时段对齐：把每站塞进它当天的真实开放区间（区分淡旺季）
        if not is_whole_day:
            day_notes.extend(_fit_open_hours(day, status_map, md, spots, used, weekday))

    notes = ctx.finish()
    plan["tips"]["notes"] = list(plan["tips"]["notes"]) + weather_notes + day_notes + notes
    plan["realtime"] = {
        "enabled": ctx.enabled,
        "weather": weather_map,
        "stats": ctx.stats,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================ 额外需求（Agent 优先，纯解析兜底） ============================

def _resolve_extra(req: dict, spots: List[dict], use_agent: bool = True) -> dict:
    """核查事实 + 把游客原话变成排班约束

    正常路径走 core/trip/agent.py 的规划 Agent（工具 + 中间件 + 回表校验）。
    它每次规划都跑：天气、闭馆、开放时段这些事实每次都要用，
    Agent 不跑，挂在图上的中间件一个都不会执行。没写额外要求时照样核查，
    只是 must / avoid 会是空的。

    Agent 不可用或没给出结论时，降级到 agentkit/tool_extra.parse_extra 的单次解析；
    两条路都失败就返回空约束——骨架照常出方案，原话保留在备注里。

    :return: {"must", "avoid", "note", "facts", "ok", "error", "audit"}
    """
    out: dict = {"must": [], "avoid": [], "note": "", "facts": "", "ok": False,
                 "error": "", "audit": []}

    if use_agent:
        try:
            from core.trip.agent import run_planning_agent
            res = run_planning_agent(req, spots)
            out["audit"] = res.get("audit") or []
            out["facts"] = res.get("facts") or ""
            if res.get("ok"):
                out.update({"must": res.get("must") or [], "avoid": res.get("avoid") or [],
                            "note": res.get("note") or "", "ok": True})
                return out
            out["error"] = res.get("error") or "agent_no_result"
        except Exception as e:           # Agent 侧任何异常都不阻断行程
            out["error"] = type(e).__name__

    # 降级：没写额外要求就没得解析，直接返回空约束
    if not req.get("extra"):
        return out

    try:
        from core.trip.agentkit.tool_extra import parse_extra
        parsed = parse_extra(req["extra"], spots)
        out.update({"must": parsed.get("must") or [], "avoid": parsed.get("avoid") or [],
                    "note": parsed.get("note") or "", "ok": bool(parsed.get("ok")),
                    "error": out["error"] or parsed.get("error") or ""})
    except Exception as e:
        out["error"] = out["error"] or type(e).__name__
    return out


# ============================ 文案润色 ============================

def _polish(plan: dict) -> None:
    """用长输出 LLM 润色 desc / tip；任何异常都原样返回（只加 degraded 说明）"""
    items = []
    for d in plan["planDays"]:
        for s in d["slots"]:
            items.append({"i": len(items), "name": s["name"], "desc": s["desc"], "tip": s["tip"]})
    if not items:
        return

    try:
        from core.trip.agent import get_planner_llm
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = get_planner_llm()
        payload = json.dumps({
            "dest": plan["dest"], "days": plan["days"], "people": plan["people"],
            "interests": plan["interestNames"], "diet": plan["diet"],
            "extra": plan["extra"], "items": items,
        }, ensure_ascii=False)
        resp = llm.invoke([SystemMessage(content=TRIP_POLISH_PROMPT),
                           HumanMessage(content=payload)])
        data = extract_json(resp.content or "")
    except Exception as e:  # 润色失败不影响骨架
        plan["polished"] = False
        plan["degraded"] = True
        plan["warnings"].append(f"AI 文案润色未生效（{type(e).__name__}），当前展示为基础推荐文案。")
        return

    polished = data.get("items") if isinstance(data, dict) else data
    if not isinstance(polished, list) or len(polished) != len(items):
        plan["polished"] = False
        plan["degraded"] = True
        plan["warnings"].append("AI 文案润色结果结构不符，当前展示为基础推荐文案。")
        return

    idx = {}
    for d in plan["planDays"]:
        for s in d["slots"]:
            idx.setdefault(len(idx), s)
    replaced = 0
    for n, entry in enumerate(polished):
        target = idx.get(n)
        if not target or not isinstance(entry, dict):
            continue
        if entry.get("name") and entry["name"] != target["name"]:
            continue  # 名称对不上说明模型串了位，跳过该条
        if isinstance(entry.get("desc"), str) and entry["desc"].strip():
            target["desc"] = entry["desc"].strip()
            replaced += 1
        if isinstance(entry.get("tip"), str) and entry["tip"].strip():
            target["tip"] = entry["tip"].strip()
    plan["polished"] = replaced > 0
    if replaced == 0:
        plan["degraded"] = True
        plan["warnings"].append("AI 文案润色结果为空，当前展示为基础推荐文案。")


# ============================ 对外主入口 ============================

def plan_trip(params: dict) -> dict:
    """生成行程：Agent 理解需求 → 确定性骨架 → 实时校准 →（可选）LLM 文案润色"""
    req = normalize_params(params)
    spots, foods, meta = load_catalog()

    # 每次规划都先跑规划 Agent（core/trip/agent.py）：它调用工具核查天气/开放时间/转场，
    # 中间件负责注入事实、钉住候选档案、事后回表清洗，最后交出结构化约束。
    # 游客没写额外要求时它照样核查，只是 must / avoid 为空。
    # 排班仍然由下面的确定性骨架完成，行程依旧可复现。
    use_agent = str(params.get("agent", "1")).strip().lower() not in (
        "0", "false", "no", "off")
    extra_info = _resolve_extra(req, spots, use_agent=use_agent)

    # 用户点名不去的景点连替补池都不进，避免绕一圈又排上
    pool = spots
    if extra_info.get("avoid"):
        ban = {str(a.get("name") or "") for a in extra_info["avoid"]}
        pool = [a for a in spots if a.get("name") not in ban] or spots

    plan_days, meal_count, notes = build_plan_days(req, pool, foods, extra_info)
    if not plan_days:
        raise ValueError("未能生成任何行程，请检查候选目录 data/attractions.json 是否有内容")

    pace = PACE_CONF[req["pace"]]
    trans = TRANS_CONF[req["transport"]]
    per_person_day = req["budget"] / req["days"]
    tier_name, tier_label, hotel = _budget_tier(per_person_day)

    fruit: List[dict] = [s for d in plan_days for s in d["slots"]]
    spot_items = [s for s in fruit if s["kind"] == "spot"]
    ticket_total = sum(s["ticket"] for s in fruit)
    visit_hours = sum(s["duration"] for s in spot_items)

    base_notes = [
        "热门景区请提前 1-7 天在官方渠道实名预约，尤其是故宫、国博等场馆。",
        "博物馆类场馆多逢周一闭馆，本行程已按您的出发日期自动避开当日闭馆景点。",
        "早晚温差大，随身备一件外套；景区内餐饮偏贵，可适当自带饮水。",
        "留意返程地铁与景区专线末班时间，避免误车。",
    ]
    for d in req["diet"]:
        advice = DIET_ADVICE.get(d)
        if advice:
            base_notes.append(advice)
    if req["extra"]:
        parts: List[str] = []
        if extra_info.get("ok"):
            if extra_info.get("must"):
                parts.append("安排 " + "、".join(
                    f"{m['name']}" + (f"（{_HINT_CN.get(m['time_hint'], '')}）" if m.get("time_hint") else "")
                    for m in extra_info["must"]))
            if extra_info.get("avoid"):
                parts.append("避开 " + "、".join(a["name"] for a in extra_info["avoid"]))
        if parts:
            base_notes.insert(0, "已按您的要求：" + "；".join(parts) + "。")
        else:
            base_notes.insert(0, f"您的额外要求：{req['extra']}")
        if extra_info.get("note"):
            base_notes.insert(1, extra_info["note"])
    base_notes.extend(notes)

    plan = {
        "id": "",  # 由 trip_store 落盘时回填
        "dest": FIXED_DEST,
        "origin": req["origin"],
        "date": req["start"].strftime("%Y-%m-%d"),
        "days": req["days"],
        "people": req["people"],
        "pace": req["pace"],
        "paceName": pace["name"],
        "budgetAmount": req["budget"],
        "budgetPerDay": round(per_person_day, 1),
        "budgetTier": tier_name,
        "budgetTierName": tier_label,
        "transport": req["transport"],
        "transportName": trans["name"],
        "interests": req["interests"],
        "interestNames": [INTEREST_NAMES[k] for k in req["interests"]],
        "diet": req["diet"] or ["不忌口"],
        "extra": req["extra"],
        "extraParsed": {
            "must": extra_info.get("must") or [],
            "avoid": extra_info.get("avoid") or [],
            "note": extra_info.get("note") or "",
            "ok": bool(extra_info.get("ok")),
            "byAgent": bool(extra_info.get("audit")) and not extra_info.get("error"),
            "facts": extra_info.get("facts") or "",    # Agent 核查到的实时结论
            "audit": extra_info.get("audit") or [],   # 工具与中间件执行痕迹，排障与讲解用
        },
        "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "spots": len(spot_items),
            "slots": len(fruit),
            "visitHours": round(visit_hours, 1),
            "walkHours": round(visit_hours * 0.5, 1),
            "ticketTotal": round(ticket_total),
            "perPerson": round(req["budget"]),
            "budgetTotal": round(req["budget"] * req["people"]),
        },
        "tips": {
            "transport": trans["tip"],
            "hotel": hotel,
            "pace": pace["note"],
            "notes": base_notes,
        },
        "planDays": plan_days,
        "polished": False,
        "degraded": False,
        "warnings": [],
    }
    if extra_info.get("error") and not extra_info.get("ok"):
        if req["extra"]:
            plan["warnings"].append(
                f"AI 需求解析未生效（{extra_info.get('error') or '未知原因'}），"
                f"已按常规推荐排班，您的要求原文已保留在下方备注中。")
        else:
            plan["warnings"].append(
                f"出发前的事实核查未生效（{extra_info['error']}），"
                f"行程按常规推荐生成，开放时间与天气以官方渠道为准。")
    elif extra_info.get("error") and extra_info.get("ok"):
        plan["warnings"].append(
            f"规划 Agent 未返回结果（{extra_info['error']}），"
            f"已降级为单次需求解析，排班结果不受影响。")

    # 实时校准（转场时长 / 天气 / 开放状态）：换站与砍站都会发生在这里
    if str(params.get("realtime", "1")).strip().lower() not in ("0", "false", "no", "off"):
        try:
            _apply_real_world(plan, req, pool, foods)
        except Exception as e:  # 校准失败不破坏骨架
            plan["warnings"].append(
                f"实时路况与天气校准未生效（{type(e).__name__}），当前展示为基础推荐。")

    # 校准可能换掉景点或砍掉站点，汇总数据要重算
    fruit_now = [s for d in plan["planDays"] for s in d["slots"]]
    spots_now = [s for s in fruit_now if s["kind"] == "spot"]
    visit_now = sum(s["duration"] for s in spots_now)
    plan["summary"].update({
        "spots": len(spots_now),
        "slots": len(fruit_now),
        "visitHours": round(visit_now, 1),
        "walkHours": round(visit_now * 0.5, 1),
        "ticketTotal": round(sum(s["ticket"] for s in fruit_now)),
    })

    if req["polish"]:
        _polish(plan)
    return plan


def _budget_tier(per_person_day: float) -> Tuple[str, str, str]:
    for limit, name, hotel in BUDGET_TIERS:
        if per_person_day < limit:
            return name, name, hotel
    return BUDGET_TIERS[-1][1], BUDGET_TIERS[-1][1], BUDGET_TIERS[-1][2]


# ============================ 地图点位 ============================

def build_trip_map(plan: dict, day_key: str = "all") -> dict:
    """把行程里的地点解析成 MapPayload（不打路径规划，只做点位）

    day_key: "all" 为总览（标记带 Day 前缀）；"1"/"2"... 为指定某天
    依赖 map_guide 的地理编码与本地缓存（高德 REST，需要 AMAP_API_KEY）
    """
    plan_days = plan.get("planDays") or []
    if day_key == "all":
        targets = [(d["day"], s) for d in plan_days for s in d["slots"]]
        title = f"{plan.get('dest', FIXED_DEST)} · {plan.get('days', 0)} 天行程总览"
    else:
        try:
            day_no = int(day_key)
        except (TypeError, ValueError):
            raise ValueError("day 参数应为 all 或天数数字")
        matched = [d for d in plan_days if int(d["day"]) == day_no]
        if not matched:
            raise ValueError(f"行程中没有第 {day_key} 天")
        targets = [(day_no, s) for s in matched[0]["slots"]]
        title = f"Day{day_no} · {matched[0].get('title', '')}"

    try:
        from core.tools import map_guide
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"地图模块不可用：{e}")

    markers: List[dict] = []
    failed: List[str] = []
    seen: Dict[str, int] = {}
    order = 0
    for day_no, slot in targets:
        name = slot.get("mapKey") or slot["name"]
        if name in seen:
            continue  # 全天项目上下午共用同一坐标，只标一次
        seen[name] = 1
        try:
            pt = map_guide._resolve(name, plan.get("dest", FIXED_DEST))  # 复用 MP 地理编码与本地缓存
        except Exception:
            failed.append(name)
            continue
        order += 1
        markers.append({
            "name": name,
            "lng": pt["lng"], "lat": pt["lat"],
            "address": pt.get("address", ""),
            "desc": slot.get("desc", ""),
            "day": day_no,
            "order": order,
        })

    if not markers:
        raise RuntimeError("未能解析出任何地点坐标，请检查 AMAP_API_KEY 是否配置且有效")

    lons = [m["lng"] for m in markers]
    lats = [m["lat"] for m in markers]
    names = "|".join(m["name"] for m in markers[:8])
    return {
        "type": "trip",
        "title": title,
        "center": [sum(lons) / len(lons), sum(lats) / len(lats)],
        "bbox": [[min(lons), min(lats)], [max(lons), max(lats)]],
        "markers": markers,
        "path": None,
        "partial": bool(failed),
        "failed": failed,
        "fallback_url": f"https://uri.amap.com/marker?src=tour-guide-ai&callnative=1&name={names}",
        "timestamp": int(time.time()),
    }


# ============================ 本地自测 ============================

def _self_check() -> None:
    cases = [
        {"date": "2026-10-19", "days": 1, "people": 2, "pace": "relax",
         "budgetAmount": 800, "transport": "bus", "interests": ["history"]},
        {"date": "2026-10-19", "days": 3, "people": 4, "pace": "normal",
         "budgetAmount": 3000, "transport": "bus", "interests": ["nature", "photo"], "diet": ["清真"]},
        {"date": "2026-10-19", "days": 5, "people": 2, "pace": "tight",
         "budgetAmount": 6000, "transport": "car", "interests": ["history", "nature", "photo", "food", "family"],
         "extra": "带老人，少走台阶"},
        {"date": "2026-10-19", "days": 4, "people": 1, "pace": "normal",
         "budgetAmount": 2000, "transport": "walk", "interests": ["family"], "diet": ["素食"]},
        # 周一出发 + 纯博物馆兴趣：验证闭馆日避让
        {"date": "2026-10-19", "days": 5, "people": 2, "pace": "tight",
         "budgetAmount": 4000, "transport": "bus", "interests": ["history"]},
    ]
    for i, case in enumerate(cases, 1):
        plan = plan_trip(dict(case, polish=False))
        lines = [f"—— 用例 {i}：{case['days']}天/{case['pace']}/{'+'.join(case['interests'])} ——",
                 f"  概要：{plan['summary']['spots']} 个景点 / {plan['summary']['slots']} 条安排 / "
                 f"人均 {plan['summary']['perPerson']} 元（{plan['budgetTierName']}档）"]
        for d in plan["planDays"]:
            lines.append(f"  Day{d['day']}（{d['date']} {d['weekdayName']}）{d['title']} "
                         f"[{len(d['slots'])} 项] " + " → ".join(s["name"] for s in d["slots"]))
        print("\n".join(lines))


if __name__ == "__main__":
    _self_check()
