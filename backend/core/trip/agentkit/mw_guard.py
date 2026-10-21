"""中间件：事后封口（模型说什么都要过一遍校验才准落地）

这是「模型负责理解、代码负责执行」那道闸门：
  - 景点名必须回表命中库里标准名，命中不了一律丢掉（防幻觉第二道闸）
  - time_hint 不在白名单里一律清空
  - must 与 avoid 冲突时以 must 为准
  - 条数设上限，防模型一口气点名整个库
  - 模型没吐出合法 JSON 时，用工具那份解析结果兜底（extra 为空则给空约束）

校验结果写进 state["constraints"]，骨架只读这一份，不读模型的原话。
"""
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware

from core.prompts.shared import extract_json

from .state import MAX_AVOID, MAX_MUST, TIME_HINTS, TripPlanState, empty_constraints
from .tool_extra import parse_extra, resolve_name


class ConstraintGuardMiddleware(AgentMiddleware):
    """收尾校验：不合格的一律丢，丢了的写进 note"""
    state_schema = TripPlanState
    name = "constraint_guard"

    def after_agent(self, state, runtime) -> Optional[dict]:
        audit = list(state.get("audit") or [])
        pool: List[dict] = [{"name": n} for n in (state.get("candidates") or [])]
        extra = str(state.get("extra") or "").strip()

        raw = ""
        msgs = list(state.get("messages") or [])
        if msgs:
            raw = getattr(msgs[-1], "content", "") or ""
            if isinstance(raw, list):                  # 部分模型返回分块内容
                raw = "".join(
                    (c.get("text", "") for c in raw if isinstance(c, dict)))
        data = extract_json(raw) if raw else None
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)

        if not isinstance(data, dict):
            # 模型没给出可解析的结论：有原话就用工具那份，没有就空约束
            if len(extra) >= 2:
                parsed = parse_extra(extra, pool)
                audit.append("ConstraintGuard: 末条消息不是合法 JSON，"
                             "改用 parse_extra_requirement 的解析结果")
                return {"constraints": {"must": parsed.get("must") or [],
                                        "avoid": parsed.get("avoid") or [],
                                        "note": parsed.get("note") or "",
                                        "facts": str(state.get("facts") or "")},
                        "audit": audit}
            audit.append("ConstraintGuard: 末条消息不是合法 JSON，且无额外要求，输出空约束")
            return {"constraints": empty_constraints(), "audit": audit}

        must: List[dict] = []
        avoid: List[dict] = []
        missed: List[str] = []
        seen: set = set()

        for entry in (data.get("must") or [])[:MAX_MUST]:
            if not isinstance(entry, dict):
                continue
            std = resolve_name(str(entry.get("name") or ""), pool)
            if not std:
                missed.append(str(entry.get("name") or "").strip())
                continue
            if std in seen:
                continue
            seen.add(std)
            hint = str(entry.get("time_hint") or "").strip().lower()
            must.append({"name": std,
                         "time_hint": hint if hint in TIME_HINTS else "",
                         "reason": str(entry.get("reason") or "").strip()[:80]})

        for entry in (data.get("avoid") or [])[:MAX_AVOID]:
            if not isinstance(entry, dict):
                continue
            std = resolve_name(str(entry.get("name") or ""), pool)
            if not std:
                missed.append(str(entry.get("name") or "").strip())
                continue
            if std in seen:
                missed.append(f"{std}（同时出现在必去里，已按必去处理）")
                continue
            seen.add(std)
            avoid.append({"name": std, "reason": str(entry.get("reason") or "").strip()[:80]})

        note = str(data.get("note") or "").strip()[:200]
        if missed:
            note = (note + "；" if note else "") + "未能匹配到库中景点：" + "、".join(
                [m for m in missed if m][:5])

        facts = str(data.get("facts") or "").strip()[:300] or str(state.get("facts") or "")
        audit.append(f"ConstraintGuard: 放行 must {len(must)} 项 / avoid {len(avoid)} 项"
                     + (f"，丢弃 {len(missed)} 项" if missed else ""))
        return {"constraints": {"must": must, "avoid": avoid, "note": note, "facts": facts},
                "audit": audit}
