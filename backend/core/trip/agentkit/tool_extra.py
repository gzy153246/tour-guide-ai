"""工具：额外需求解析（模型按需调用）

为什么是工具而不是中间件：
    中间件是横切钩子，挂上去就「每次都跑」，跳不掉；
    工具是能力，调不调由模型看着办——用户没写额外要求时压根不该解析。
    「只在有要求时才做」这件事，本质就是一次按需决策，所以它是工具。

本文件两样东西：
    parse_extra()            纯实现：一次 LLM 调用 + 回表校验（Agent 降级时也走它）
    parse_extra_requirement  @tool 包装：注册给 Agent，由模型决定是否调用

三条底线：
  1. 解析出的景点名必须回表校验，命中不了库里标准名的一律丢弃（防幻觉）；
  2. 任何异常都降级为空结果，骨架照常出方案，只在 note 里如实说明；
  3. 同一句 extra 只解析一次（内存缓存），避免重复消耗 token。
"""
import hashlib
import json
import re
from typing import Dict, List, Optional

from core.prompts.trip import EXTRA_PARSE_PROMPT
from core.prompts.shared import extract_json

from .state import TIME_HINTS

_CACHE: Dict[str, dict] = {}
_CACHE_MAX = 256

_PUNCT_RE = re.compile(r"[\s，,。、；;：:！!？?\"'“”‘’()（）\[\]【】·\-—~]")


def _norm(text: str) -> str:
    """去掉空白与标点，便于做 tolerant 匹配"""
    return _PUNCT_RE.sub("", str(text or ""))


def resolve_name(raw: str, spots: List[dict]) -> Optional[str]:
    """把模型给出的景点名回表成库里的标准名；对不上返回 None

    先精确匹配（去标点后相等），再做双向包含并取最长命中，
    这样「天安门」能落到「天安门广场」、「故宫」能落到「故宫博物院」。
    """
    key = _norm(raw)
    if not key:
        return None
    for a in spots:
        if _norm(a.get("name", "")) == key:
            return a["name"]
    best, best_len = None, 0
    for a in spots:
        n = _norm(a.get("name", ""))
        if not n or len(n) < 2:
            continue
        if (n in key or key in n) and len(n) > best_len:
            best, best_len = a["name"], len(n)
    return best


def _blank(ok: bool = False, error: str = "") -> dict:
    return {"must": [], "avoid": [], "note": "", "ok": ok, "error": error}


def parse_extra(extra: str, spots: List[dict]) -> dict:
    """解析用户额外需求

    :return: {"must":[{"name","time_hint","reason"}], "avoid":[{"name","reason"}],
              "note": str, "ok": bool, "error": str}
    """
    text = str(extra or "").strip()
    if len(text) < 2:
        return _blank(ok=True)

    cache_key = hashlib.md5(text.encode("utf-8")).hexdigest()
    if cache_key in _CACHE:
        return dict(_CACHE[cache_key])

    names = [a.get("name", "") for a in spots
             if a.get("kind", "spot") == "spot" and a.get("name")]
    try:
        # 惰性导入：core/trip/agent.py 会导入本包，模块级反向导入会成环
        from core.trip.agent import get_planner_llm
        from langchain_core.messages import HumanMessage, SystemMessage

        payload = json.dumps({"extra": text, "candidates": names}, ensure_ascii=False)
        resp = get_planner_llm().invoke([SystemMessage(content=EXTRA_PARSE_PROMPT),
                                         HumanMessage(content=payload)])
        data = extract_json(resp.content or "")
    except Exception as e:                       # 解析失败不阻断行程
        out = _blank(ok=False, error=type(e).__name__)
        _put(cache_key, out)
        return out

    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    if not isinstance(data, dict):
        out = _blank(ok=False, error="bad_json")
        _put(cache_key, out)
        return out

    must, avoid, missed, seen = [], [], [], set()
    for entry in (data.get("must") or []):
        if not isinstance(entry, dict):
            continue
        std = resolve_name(entry.get("name", ""), spots)
        if not std:
            missed.append(str(entry.get("name") or "").strip())
            continue
        if std in seen:
            continue
        seen.add(std)
        hint = str(entry.get("time_hint") or "").strip().lower()
        if hint not in TIME_HINTS:
            hint = ""
        must.append({"name": std, "time_hint": hint,
                     "reason": str(entry.get("reason") or "").strip()[:80]})

    for entry in (data.get("avoid") or []):
        if not isinstance(entry, dict):
            continue
        std = resolve_name(entry.get("name", ""), spots)
        if not std:
            missed.append(str(entry.get("name") or "").strip())
            continue
        if std not in seen:
            seen.add(std)
            avoid.append({"name": std,
                          "reason": str(entry.get("reason") or "").strip()[:80]})

    note = str(data.get("note") or "").strip()[:200]
    if missed:
        note = (note + "；" if note else "") + "未能在景点库中匹配：" + "、".join(
            [m for m in missed if m][:5])

    out = {"must": must, "avoid": avoid, "note": note, "ok": True, "error": ""}
    _put(cache_key, out)
    return out


def _put(key: str, value: dict) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = value


def parse_extra_requirement(extra: str) -> str:
    """把游客的额外要求解析成结构化约束 {must, avoid}。

    只在游客确实写了额外要求时才调用；没写就不要调用，直接输出空的 must/avoid。
    入参是游客原话，返回 JSON 字符串：景点名已回表校验，命中不了库里标准名的会被丢弃。
    返回的 must/avoid 仅供参考，最终仍以系统给出的候选清单为准。
    """
    text = str(extra or "").strip()
    if len(text) < 2:
        return json.dumps({"must": [], "avoid": [], "note": "游客未填写额外要求"},
                          ensure_ascii=False)
    try:
        from core.trip.itinerary import load_catalog      # 惰性：避免与 itinerary 成环
        spots, _foods, _meta = load_catalog()
    except Exception as e:
        return json.dumps({"must": [], "avoid": [], "note": f"景点库读取失败：{type(e).__name__}"},
                          ensure_ascii=False)

    parsed = parse_extra(text, spots)
    return json.dumps({"must": parsed.get("must") or [],
                       "avoid": parsed.get("avoid") or [],
                       "note": parsed.get("note") or ""}, ensure_ascii=False)


try:
    from langchain_core.tools import tool as _lc_tool
    parse_extra_requirement = _lc_tool(parse_extra_requirement)
except Exception:      # 没装 langchain 时保持为普通函数，降级路径照样能用
    pass
