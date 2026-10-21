"""中间件：实时事实注入（天气 / 节假日）

天气与节假日是「一次批量取回」最划算的——结果有落盘缓存，
让模型自己挨个调工具问反而慢。所以这两样由中间件在开局摆到模型眼前，
开放时间与转场时长留给模型按需细查。

查不到就如实说查不到，绝不编。
"""
from typing import List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from .state import TripPlanState


class RealtimeFactsMiddleware(AgentMiddleware):
    """每次运行都把能查到的实时事实摆到模型眼前"""
    state_schema = TripPlanState
    name = "realtime_facts"

    def before_agent(self, state, runtime) -> Optional[dict]:
        dates = list(state.get("dates") or [])
        audit = list(state.get("audit") or [])
        if not dates:
            return None
        lines: List[str] = []
        try:
            from core.tools.trip_context import weather, holidays
            wmap = weather() or {}
            hit = {d: wmap[d] for d in dates if d in wmap}
            if hit:
                lines.append("天气预报：" + "；".join(
                    f"{d} {w.get('day')} {w.get('temp', '')}（适宜度 {w.get('grade')}）"
                    for d, w in hit.items()))
            else:
                lines.append("天气预报：所选日期不在预报覆盖范围内（通常只有今天起 4 天），"
                             "按常规天气考虑，不要编造天气。")
            hol = holidays() or {}
            hol_hit = [f"{d} {hol[d]}" for d in dates if d in hol]
            if hol_hit:
                lines.append("法定节假日：" + "、".join(hol_hit)
                             + "，博物馆「周一闭馆」等常规规则不适用。")
        except Exception as e:            # 事实注入失败不阻断，模型还有工具可用
            audit.append(f"RealtimeFacts: 取实时事实失败（{type(e).__name__}），降级为不注入")
            return {"audit": audit}

        facts = "\n".join(lines)
        audit.append(f"RealtimeFacts: 注入 {len(lines)} 条实时事实")
        return {"facts": facts, "audit": audit,
                "messages": [SystemMessage(content="出行期间的已知事实：\n" + facts)]}
