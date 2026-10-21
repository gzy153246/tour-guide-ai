"""中间件：候选景点档案注入（防幻觉第一道闸）

模型最容易犯的错是自造一个「听着像」的景点名（把"故宫博物院"写成"北京故宫"），
或者凭印象说"故宫晚上也能逛"。与其事后抓，不如事前把档案摆出来：

  - 可安排的景点只能是这 N 个，必须原样引用标准名
  - 每个点的区域 / 适合时段 / 时长 / 票价 / 室内外 / 固定闭馆日一并给出

档案来自 data/attractions.json（本地，零 API 成本），
这也是「每次规划都要看景区详细信息」的第一层——实时开放时间由模型按需再查。
"""
from typing import List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from .state import TripPlanState

_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WINDOW_CN = {"morning": "上午", "noon": "中午", "afternoon": "下午",
              "evening": "晚间", "whole": "全天"}


class CatalogGroundingMiddleware(AgentMiddleware):
    """每次运行都把可安排景点的档案钉进上下文"""
    state_schema = TripPlanState
    name = "catalog_grounding"

    def before_agent(self, state, runtime) -> Optional[dict]:
        names = list(state.get("candidates") or [])
        audit = list(state.get("audit") or [])
        if not names:
            return None

        lines: List[str] = []
        try:
            from core.trip.itinerary import load_catalog
            spots, _foods, _meta = load_catalog()
            index = {a.get("name"): a for a in spots}
            for n in names:
                a = index.get(n)
                if not a:
                    lines.append(f"- {n}")
                    continue
                win = "、".join(_WINDOW_CN.get(w, w) for w in (a.get("windows") or [])) or "未标注"
                closed = "、".join(_WEEKDAY_NAMES[i] for i in (a.get("closed_weekday") or [])
                                  if 0 <= i < 7) or "无固定闭馆日"
                lines.append(
                    f"- {n}｜{a.get('area', '未知')}｜适合{win}｜{a.get('duration') or 0} 小时｜"
                    f"{'室内' if a.get('indoor') else '室外'}｜固定闭馆 {closed}")
        except Exception as e:
            audit.append(f"CatalogGrounding: 读景点档案失败（{type(e).__name__}），仅注入名称")
            lines = [f"- {n}" for n in names]

        text = (f"可安排的景点只能是下面这 {len(names)} 个，必须原样引用其标准名（禁止自造、改写、翻译）：\n"
                + "\n".join(lines))
        audit.append(f"CatalogGrounding: 钉住 {len(names)} 个候选景点档案")
        return {"audit": audit, "messages": [SystemMessage(content=text)]}
