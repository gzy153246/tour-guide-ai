"""行程规划 Agent：模型实例 + 图 + 运行入口

职责边界：
    - 它「核查事实」——每次规划都查天气、查开放时间/闭馆日、必要时查转场时长
    - 它「理解需求」——读游客原话，需要时调用 parse_extra_requirement，交出结构化约束
    - 它「不排班」——哪天去哪、几点去、多少钱，全部由 itinerary.py 的确定性骨架算
    - 它「说了不算」——输出的景点名要回表校验，命中不了一律丢弃（见 mw_guard.py）

一句话：模型负责理解与核查，代码负责执行。行程仍然可复现，变的只是约束从哪来。

为什么每次规划都跑：
    天气、闭馆、转场是每次排班都要用的实时数据，Agent 不跑，
    挂在图上的中间件一个都不会执行——中间件不是独立进程，它没有图就没有宿主。
    Agent 查过的结果会进 TripContext 的缓存，骨架后面的 _apply_real_world
    直接命中缓存，不会重复消耗高德配额。

为什么跑在骨架之前：
    骨架需要 must / avoid 才能排班，而这两个字段只有读完原话才有；
    核查到的闭馆/天气事实也要在排班前知道，否则排完再改等于返工。

对外：
    get_planner_llm()     获取模型单例（行程 Agent / 需求解析 / 文案润色共用）
    reset_planner_llm()   重置/替换单例
    get_trip_agent()      获取图单例（懒加载，首次调用才建图）
    reset_trip_agent()    重置/替换图单例
    run_planning_agent()  跑一次，返回 {ok, must, avoid, note, facts, audit, error}
"""
import os
import sys
import threading
from datetime import timedelta
from typing import Any, Dict, List, Optional

# 保证以任意工作目录启动时都能导入 core 包
# 层数：core/trip/agent.py -> trip -> core -> backend
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from core.prompts.trip import TRIP_AGENT_SYSTEM  # noqa: E402
from core.trip.agentkit import TRIP_AGENT_TOOLS, TripPlanState, build_middleware  # noqa: E402

# ============================ 模型实例 ============================
# 行程链路自持模型，不向问答链路借：core/chat/agent_engine.py 里那个实例是
# max_tokens=300 / temperature=0.7 的对话模型，写行程会被截断且发散。
# 这里另开一个长输出、低随机的实例，行程 Agent / 需求解析 / 文案润色共用。
_planner_llm: Optional[ChatOpenAI] = None
_planner_lock = threading.Lock()


def get_planner_llm() -> ChatOpenAI:
    """面向结构化长输出的模型单例：行程 Agent / 需求解析 / 文案润色共用"""
    global _planner_llm
    if _planner_llm is None:
        with _planner_lock:
            if _planner_llm is None:
                _planner_llm = ChatOpenAI(
                    model=os.getenv("PLANNER_MODEL", "deepseek-flash"),
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url=os.getenv("DEEPSEEK_BASE_URL"),
                    temperature=0.3,
                    max_tokens=4096,
                    timeout=float(os.getenv("PLANNER_TIMEOUT", "90")),
                    max_retries=0,
                )
    return _planner_llm


def reset_planner_llm(llm: Optional[ChatOpenAI] = None) -> None:
    """重置/替换单例；不传参则下次调用时重新构建"""
    global _planner_llm
    with _planner_lock:
        _planner_llm = llm


# ============================ Agent 图 ============================

_MIDDLEWARE = build_middleware()

_agent = None
_agent_lock = threading.Lock()


def get_trip_agent():
    """获取行程规划 Agent 的图单例（建图有开销，只建一次）"""
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                _agent = create_agent(
                    model=get_planner_llm(),
                    tools=TRIP_AGENT_TOOLS,
                    system_prompt=TRIP_AGENT_SYSTEM,
                    middleware=_MIDDLEWARE,
                    state_schema=TripPlanState,
                )
    return _agent


def reset_trip_agent(agent=None) -> None:
    """重置/替换图单例"""
    global _agent
    with _agent_lock:
        _agent = agent


# ============================ 运行入口 ============================

def run_planning_agent(req: dict, spots: List[dict]) -> Dict[str, Any]:
    """跑一次行程规划 Agent：核查事实 + 把游客原话翻译成排班约束

    每次规划都跑，不跳过：天气、闭馆、开放时段这些事实每次都要用。
    没写额外要求时照样跑核查，只是 must / avoid 会是空的。

    :param req:   normalize_params() 的产出（需要 start / days / interests / extra）
    :param spots: 景点池，用于给中间件提供候选名清单
    :return: {"ok", "must", "avoid", "note", "facts", "audit", "error"}
             ok=False 时调用方应退回 core/trip/agentkit/tool_extra.parse_extra
    """
    extra = str(req.get("extra") or "").strip()
    names = [a.get("name", "") for a in spots
             if a.get("kind", "spot") == "spot" and a.get("name")]
    start, days = req.get("start"), int(req.get("days") or 0)
    try:
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    except Exception:
        dates = []

    payload = {
        "extra": extra,
        "dates": dates,
        "interests": list(req.get("interests") or []),
        "pace": req.get("pace", ""),
        "transport": req.get("transport", ""),
        "city": os.getenv("TRIP_CITY", "北京"),
    }
    state = {
        "messages": [HumanMessage(content=_human_text(payload))],
        "extra": extra,
        "candidates": names,
        "dates": dates,
        "constraints": {},
        "facts": "",
        "audit": [],
    }

    try:
        res = get_trip_agent().invoke(state)
    except Exception as e:                      # 建图失败、模型不可用、图内部异常
        return {"ok": False, "must": [], "avoid": [], "note": "", "facts": "",
                "audit": [f"run_planning_agent: 调用失败（{type(e).__name__}）"],
                "error": type(e).__name__}

    cons = res.get("constraints") or {}
    audit = list(res.get("audit") or [])
    calls = _tool_calls(res)
    if calls:
        audit.append(f"run_planning_agent: 实际调用工具 {len(calls)} 次（"
                     + "、".join(sorted(set(calls))) + "）")

    return {
        "ok": True,
        "must": cons.get("must") or [],
        "avoid": cons.get("avoid") or [],
        "note": cons.get("note") or "",
        "facts": cons.get("facts") or "",
        "audit": audit,
        "error": "",
    }


def _tool_calls(res: dict) -> List[str]:
    """从消息流里数出模型真正发起的工具调用，审计要看得见"""
    out: List[str] = []
    for m in (res.get("messages") or []):
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name:
                out.append(name)
    return out


def _human_text(payload: dict) -> str:
    """给模型的首条消息：把日期与原话摆出来，别让它自己猜"""
    dates = payload.get("dates") or []
    span = f"{dates[0]} 起共 {len(dates)} 天" if dates else "日期未提供"
    extra = payload.get("extra", "")
    req_line = (f"游客原话：「{extra}」" if len(extra) >= 2
                else "游客未填写额外要求（must 与 avoid 都给空数组）")
    return (f"出行：{span}，城市 {payload.get('city', '北京')}；"
            f"交通偏好 {payload.get('transport') or '公交'}，节奏 {payload.get('pace') or '适中'}。\n"
            f"兴趣标签：{', '.join(payload.get('interests') or []) or '无'}。\n"
            f"{req_line}\n"
            f"先按系统规则完成事实核查，再输出那段 JSON 约束。")


if __name__ == "__main__":
    from datetime import date
    import json

    from core.trip.itinerary import load_catalog
    sp, _fd, _meta = load_catalog()
    for text in ("想去天安门广场看一次升旗", ""):
        demo = {"start": date(2026, 11, 2), "days": 3, "interests": ["history"],
                "pace": "normal", "transport": "bus", "extra": text}
        r = run_planning_agent(demo, sp)
        print(f"\n=== extra = {text!r} ===")
        print("ok =", r["ok"], "| must =", json.dumps(r["must"], ensure_ascii=False))
        print("facts =", r["facts"][:120])
        for a in r["audit"]:
            print("  -", a)
