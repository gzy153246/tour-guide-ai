"""行程 Agent 的共享状态与常量

单独成文件的原因：中间件与工具都要用 TripPlanState，
若把它放进 __init__.py 会形成「子模块 → 包 → 子模块」的循环导入。
"""
from typing import Any, Dict, List

from langchain.agents.middleware.types import AgentState

# 允许的时间提示词；模型给别的词一律当作「无明确时间」
TIME_HINTS = ("dawn", "morning", "noon", "afternoon", "evening")

# 一条额外要求最多认几个必去/避开，防模型一口气把整个库都点名了
MAX_MUST = 6
MAX_AVOID = 8


class TripPlanState(AgentState):
    """行程 Agent 的自定义状态：在消息流之外再带几份结构化数据"""
    extra: str                      # 游客原话（可能为空）
    candidates: List[str]           # 库中全部可安排景点名
    dates: List[str]                # 出行日期 ["2026-11-02", ...]
    constraints: Dict[str, Any]    # 最终生效的 {must, avoid, note, facts}
    facts: str                      # 查到的实时事实简报
    audit: List[str]                # 中间件执行痕迹，排查与讲解都靠它


def empty_constraints() -> dict:
    return {"must": [], "avoid": [], "note": "", "facts": ""}
