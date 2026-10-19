"""行程编排引擎（确定性骨架 + LLM 文案润色）
职责边界：
  - 这里只做「生成行程」这一件事，不认识 FastAPI，可 `python core/itinerary.py` 单跑
  - 候选景点来自 data/attractions.json，改动该文件无需重启之外的操作（按 mtime 自动失效）
  - 地图点位是惰性产物：生成行程时不打高德，等前端要看图时才调用 build_trip_map()
设计取舍：
  - 骨架（选哪些点、放哪天、什么时段、多少钱）全部由规则算出，稳定、可复现、毫秒级
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
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)                                # 项目根，data 在这里
if _BACKEND_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _BACKEND_DIR)

from core.prompts import TRIP_POLISH_PROMPT, extract_json  # noqa: E402

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
WINDOW_NAME = {"morning": "上午", "noon": "中午", "afternoon": "下午",
               "evening": "晚间", "whole": "全天"}

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


def _slot(item: dict, slot_key: str, time_text: str, desc: str = "", tip: str = "") -> dict:
    return {
        "slot": slot_key,
        "slotName": WINDOW_NAME.get(slot_key, slot_key),
        "time": time_text,
        "name": item["name"],
        "kind": item.get("kind", "spot"),
        "desc": desc or item.get("desc", ""),
        "tip": tip or item.get("tip", ""),
        "duration": item.get("duration", 0),
        "ticket": item.get("ticket", 0) or 0,
        "area": item.get("area", ""),
        # 地图去重键：全天项目分上下两段时共用同一个原始名称
        "mapKey": item.get("mapKey", item["name"]),
    }


def build_plan_days(req: dict, spots: List[dict], foods: List[dict]) -> Tuple[List[dict], Dict[str, int], List[str]]:
    """返回 (planDays, 餐饮计数, 提示语)"""
    cap = PACE_CONF[req["pace"]]["cap"]
    per_day = cap["morning"] + cap["afternoon"] + cap["evening"]
    dates = _date_list(req["start"], req["days"])
    weekdays = [d.weekday() for d in dates]

    scored = []
    for a in spots:
        item = dict(a)
        item["_score"] = _score(a, req["interests"])
        scored.append(item)
    scored.sort(key=lambda a: (-a["_score"], a["name"]))
    whole_items = [a for a in scored if a.get("whole_day") and a["_score"] >= 7.0]

    used: set = set()
    meal_count: Dict[str, int] = {}
    notes: List[str] = []

    # 主题乐园占用一整天，只在行程够长或明确亲子时才给，避免"沾标签就吞掉一天"
    whole_day_index = -1
    whole_item = None
    if whole_items and (req["days"] >= 4 or "family" in req["interests"]):
        pick = next((a for a in whole_items if any(_open_on(a, w) for w in weekdays)), None)
        if pick:
            whole_item = pick
            used.add(pick["name"])
            whole_day_index = req["days"] - 1

    plan_days: List[dict] = []
    for i, day_date in enumerate(dates):
        wd = weekdays[i]
        if i == whole_day_index and whole_item:
            day_area = whole_item["area"]
            plan_days.append(_whole_day_block(i, day_date, whole_item, foods, meal_count, req))
            plan_days[-1]["area"] = day_area
            continue

        need = per_day
        picked = _pick_day_spots(scored, used, wd, need,
                                 prefer_area="core" if i == 0 else "")
        picked = _ensure_window_fit(scored, picked, need, cap, used)
        for p in picked:
            used.add(p["name"])
        if len(picked) < need:
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
        day["slots"] = _layout_slots(picked, foods, cap, area, meal_count, req)
        day["title"] = _day_title(i, req["days"], area, req["origin"], None)
        plan_days.append(day)

    return plan_days, meal_count, notes


def _ensure_window_fit(pool: List[dict], picked: List[dict], need: int, cap: dict,
                       used: set) -> List[dict]:
    """挑中的景点必须撑得起当天要排的时段；缺哪个窗口就往候选池后面换一个能撑的进来

    否则会出现「晚间 18:30 去天坛」这种傍晚已闭园的安排。
    注意：替补必须从「此前几天都没用过」的候选里找，否则会造成跨天重复。
    """
    picked = list(picked)
    for window, k in (("morning", cap["morning"]), ("evening", cap["evening"])):
        if k <= 0 or not picked:
            continue
        if any(window in (a.get("windows") or []) for a in picked):
            continue
        backup = next((a for a in pool
                       if a not in picked and a["name"] not in used
                       and window in (a.get("windows") or [])), None)
        if backup:
            picked[-1] = backup
    return picked


def _layout_slots(picked: List[dict], foods: List[dict], cap: dict, area: str,
                  meal_count: Dict[str, int], req: dict) -> List[dict]:
    """把当天挑好的景点落位到 morning → noon → afternoon → evening"""
    remaining = list(picked)
    slots: List[dict] = []

    def take(window: str, count: int) -> List[Tuple[dict, str]]:
        """按窗口偏好取景点。

        - 优先取本身在该窗口开放的景点；
        - 上午/下午尽量不吃掉「晚间型」景点（否则晚间只能硬排一个傍晚关门的地方）；
        - 晚间排不出合适的就不排，那一段留给晚餐或自由活动。
        """
        out: List[Tuple[dict, str]] = []
        for idx in range(count):
            times = WINDOW_TIME[window]
            t = times[min(idx, len(times) - 1)]
            cands = [a for a in remaining if window in (a.get("windows") or [])]
            if not cands:
                if window == "evening":
                    break  # 没有适合晚间的安排，宁缺勿滥
                cands = list(remaining)
            elif window in ("morning", "afternoon") and len(cands) > 1:
                # 保留能放晚间的景点给晚间
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
        slots.append(_slot(item, "morning", t))

    meal = _pick_meal(foods, area, meal_count, req["diet"], "noon")
    if meal:
        meal_count[meal["name"]] = meal_count.get(meal["name"], 0) + 1
        slots.append(_slot(meal, "noon", WINDOW_TIME["noon"][0]))

    for item, t in take("afternoon", cap["afternoon"]):
        slots.append(_slot(item, "afternoon", t))

    evening_spot_filled = False
    for item, t in take("evening", cap["evening"]):
        slots.append(_slot(item, "evening", t))
        evening_spot_filled = True

    if not evening_spot_filled:
        dinner = _pick_meal(foods, area, meal_count, req["diet"], "evening")
        if dinner:
            meal_count[dinner["name"]] = meal_count.get(dinner["name"], 0) + 1
            slots.append(_slot(dinner, "evening", WINDOW_TIME["evening"][0]))

    return slots


def _whole_day_block(idx: int, day_date, item: dict, foods: List[dict],
                     meal_count: Dict[str, int], req: dict) -> dict:
    """整日项目（主题乐园）：上午入园 + 园区午餐 + 下午续玩"""
    slots = [_slot(item, "morning", "09:00 - 12:30",
                   item.get("desc", ""), item.get("tip", ""))]
    meal = _pick_meal(foods, item["area"], meal_count, req["diet"], "noon")
    if meal:
        meal_count[meal["name"]] = meal_count.get(meal["name"], 0) + 1
        slots.append(_slot(meal, "noon", WINDOW_TIME["noon"][0]))
    afternoon = _slot(
        item, "afternoon", "13:30 - 17:30",
        f"继续游览{item['name']}的其余园区，留意官方 App 上的演出与花车时刻。",
        "热门项目可用单次通道，闭园前两小时排队最快")
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
        from core.agent_engine import get_planner_llm
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
    """生成行程：确定性骨架 +（可选）LLM 文案润色"""
    req = normalize_params(params)
    spots, foods, meta = load_catalog()

    plan_days, meal_count, notes = build_plan_days(req, spots, foods)
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
        base_notes.insert(0, f"您的额外要求：{req['extra']}")
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
