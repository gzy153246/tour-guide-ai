"""行程 Agent 的工具与中间件（一物一文件）

    tool_extra.py       parse_extra_requirement｜按需：游客写了额外要求才解析
    mw_facts.py         RealtimeFactsMiddleware｜每次：天气 / 节假日批量注入
    mw_grounding.py     CatalogGroundingMiddleware｜每次：候选景点档案钉死
    mw_guard.py         ConstraintGuardMiddleware｜收尾：回表校验后才准落地
    state.py            TripPlanState 与共用常量（单独放，避免循环导入）

工具与中间件的分工：
    工具 = 能力，调不调由模型看着办（按需）；
    中间件 = 切面，挂上就每次都跑（批量、校验、限流）。
    只在特定条件下才做的事，一律做成工具，不做成中间件——后者跳不掉。

本包只做汇总，不写实现。
"""
from typing import List

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from core.tools.trip_context import TRIP_CONTEXT_TOOLS

from .mw_facts import RealtimeFactsMiddleware
from .mw_grounding import CatalogGroundingMiddleware
from .mw_guard import ConstraintGuardMiddleware
from .state import TripPlanState, empty_constraints
from .tool_extra import parse_extra, parse_extra_requirement, resolve_name

# Agent 可用工具：共享的行程工具（core/tools/）+ 本链路专属的需求解析工具
TRIP_AGENT_TOOLS: List = list(TRIP_CONTEXT_TOOLS) + [parse_extra_requirement]


def build_middleware() -> List[AgentMiddleware]:
    """组装中间件链：先摆事实 → 再钉范围 → 收尾封口 → 尾部三件负责不跑飞

    顺序是有意的——模型先看到事实与候选档案，幻觉概率最低。
    """
    return [
        RealtimeFactsMiddleware(),
        CatalogGroundingMiddleware(),
        ConstraintGuardMiddleware(),
        # 工具偶发超时（高德限流）自动重试一次，仍失败就把结果交给模型让它另想办法
        ToolRetryMiddleware(max_retries=1, initial_delay=0.5, backoff_factor=1.5),
        # 死循环兜底：单次运行最多 4 次模型调用、12 次工具调用
        # （逐点核实开放时间时工具调用会到 9 次左右，上限留足余量）
        ModelCallLimitMiddleware(run_limit=4, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=12, exit_behavior="continue"),
    ]


__all__ = [
    "TRIP_AGENT_TOOLS", "build_middleware",
    "TripPlanState", "empty_constraints",
    "RealtimeFactsMiddleware", "CatalogGroundingMiddleware", "ConstraintGuardMiddleware",
    "parse_extra", "parse_extra_requirement", "resolve_name",
]
