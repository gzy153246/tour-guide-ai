"""数字人对话的会话记忆：近期几轮原文 + 更早历史压成的摘要

背景：/api/chat/* 每次请求只带当前这一句话，模型看不到前面聊过什么，
于是「我刚才问的那个景点几点开门」这种追问它答不上来——对话没有记忆。

做法（两截式，不做全量回放）：
    最近 4 轮        原样发给模型，刚说过的事不会被曲解
    更早的轮次       交给压缩模型压成一段 400 字以内的「交接笔记」，随每轮注入
    累计超过 2000 字  触发一次压缩，压完只留最近 4 轮原文

为什么不全量回放：对话会越来越长，token 与延迟都线性上涨，
而绝大多数早期内容（寒暄、重复确认）对当前这句毫无用处。

为什么不用 langgraph 的 checkpointer：
    本项目的 Agent 引擎带知识库版本缓存与重建逻辑（agent_engine_cache / version_id），
    再叠一层 thread_id 状态会让版本切换与缓存失效规则互相打架。
    记忆放在应用层，图本身保持无状态，改动面与风险都小一个量级。

落盘：项目根 data/chat/sessions.json（运行时数据，不入库）。
    超过 50 个会话或 7 天没更新会淘汰最旧的，避免文件无限膨胀。

对外：
    context_messages(session_id)  取出要注入的上下文消息（摘要 + 最近 N 轮）
    record(session_id, user, ai)  记一轮，必要时自动压缩
    list_sessions(current_id)     所有会话的列表，每个只带一句预览（前端会话历史面板）
    history(session_id)           某个会话的完整内容（摘要 + 逐条轮次），排查用
    clear(session_id)             删掉某个会话（不传则清空全部）
    stats()                        当前会话数与提示词占用，排查用

一条约定：**换数字人形象不等于换个人聊。**
人设只是每轮注入的系统提示词（persona），不属于会话状态；
切换形象既不切换 session_id，也不清历史——游客换了个形象接着聊，
前面说过的话新形象照样接得住。要"换个人聊"必须显式清会话。
"""
import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# 层数：core/chat/chat_memory.py -> chat -> core -> backend -> 项目根
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
STORE_PATH = os.path.join(_PROJECT_DIR, "data", "chat", "sessions.json")

# 触发压缩的累计字数；保留的最近轮数；落盘淘汰策略；会话列表预览字数
THRESHOLD_CHARS = 2000
KEEP_TURNS = 4
MAX_SESSIONS = 50
TTL_SECONDS = 7 * 24 * 3600
PREVIEW_CHARS = 80

_DEFAULT_SESSION = "default"

# session_id -> {"turns": [{"role","text","ts"}], "summary": str, "updated": float}
_STORE: Dict[str, dict] = {}
_LOADED = False
_LOCK = threading.RLock()


# ============================ 落盘 ============================

def _ensure_loaded() -> None:
    """首次访问时把磁盘上的会话读进内存；顺手淘汰过期与超量的"""
    global _LOADED
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        try:
            if os.path.exists(STORE_PATH):
                with open(STORE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _STORE.update({k: v for k, v in data.items() if isinstance(v, dict)})
        except Exception as e:
            print(f"⚠️ 会话记忆读取失败（{type(e).__name__}），本次从空记忆开始")
        _prune()
        _LOADED = True


def _prune() -> None:
    """淘汰 7 天未更新的会话，并最多保留 MAX_SESSIONS 个"""
    now = time.time()
    stale = [k for k, v in _STORE.items()
             if now - float(v.get("updated") or 0) > TTL_SECONDS]
    for k in stale:
        _STORE.pop(k, None)
    if len(_STORE) > MAX_SESSIONS:
        by_age = sorted(_STORE.items(), key=lambda kv: float(kv[1].get("updated") or 0))
        for k, _v in by_age[:len(_STORE) - MAX_SESSIONS]:
            _STORE.pop(k, None)


def _save() -> None:
    """整份写回（会话量小，一次写完比增量合并简单且不易写坏）"""
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STORE_PATH), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_STORE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STORE_PATH)      # 原子替换，进程被杀也不会留下半个文件
    except Exception as e:
        print(f"⚠️ 会话记忆落盘失败（{type(e).__name__}），本次记忆只留在内存里")


def _get(session_id: str, create: bool = False) -> dict:
    """取会话条目。

    create=False（默认）只读：查记忆不该顺手建档，否则每查一次就多一条
    空会话混进会话列表；只有 record 这类真正要写的地方才 create=True。
    """
    key = (session_id or _DEFAULT_SESSION).strip() or _DEFAULT_SESSION
    with _LOCK:
        _ensure_loaded()
        item = _STORE.get(key)
        if item is None:
            item = {"turns": [], "summary": "", "updated": time.time()}
            if create:
                _STORE[key] = item
        return item


# ============================ 读取上下文 ============================

def context_messages(session_id: str, keep: int = KEEP_TURNS) -> List:
    """取出要注入到本轮对话的上下文消息

    返回 [] 表示首次对话（无摘要、无历史），调用方照常只发当轮问题即可。
    """
    with _LOCK:
        item = _get(session_id)
        turns = list(item.get("turns") or [])[-keep:]
        summary = str(item.get("summary") or "").strip()

    out: List = []
    if summary:
        out.append(SystemMessage(content="【前面聊过的内容，供你接上话头，不要在回答里复述】\n"
                                        + summary))
    for t in turns:
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        out.append(HumanMessage(content=text) if t.get("role") == "user"
                   else AIMessage(content=text))
    return out


def stats(session_id: str = "") -> dict:
    """当前会话的轮数、累计字数、摘要长度，排查与前端展示都用它"""
    with _LOCK:
        if session_id:
            item = _get(session_id)
            turns = item.get("turns") or []
            return {"sessions": 1, "turns": len(turns),
                    "chars": _chars(turns), "summaryChars": len(item.get("summary") or ""),
                    "compressed": bool(item.get("summary"))}
        _ensure_loaded()
        return {"sessions": len(_STORE),
                "turns": sum(len(v.get("turns") or []) for v in _STORE.values()),
                "chars": sum(_chars(v.get("turns") or []) for v in _STORE.values()),
                "sessionsWithSummary": sum(1 for v in _STORE.values() if v.get("summary"))}


def _chars(turns: List[dict]) -> int:
    return sum(len(str(t.get("text") or "")) for t in turns)


# ============================ 记录与压缩 ============================

def record(session_id: str, user_text: str, ai_text: str,
           threshold: int = THRESHOLD_CHARS, keep: int = KEEP_TURNS) -> bool:
    """记一轮对话；累计字数超阈值就把早期轮次压成摘要

    压缩本身扔到后台线程：它要跑一次模型（冷启动可能十几秒），
    卡在请求里会让游客干等；响应先回去，摘要晚一两秒到位不影响下一轮。

    :return: 本次是否触发了压缩（后台排队也算）
    """
    user_text = str(user_text or "").strip()
    ai_text = str(ai_text or "").strip()
    if not user_text and not ai_text:
        return False

    with _LOCK:
        item = _get(session_id, create=True)
        turns: List[dict] = list(item.get("turns") or [])
        if user_text:
            turns.append({"role": "user", "text": user_text, "ts": time.time()})
        if ai_text:
            turns.append({"role": "ai", "text": ai_text, "ts": time.time()})
        item["turns"] = turns
        item["updated"] = time.time()
        over = _chars(turns) > threshold
        _save()

    if not over:
        return False
    if session_id in _COMPRESSING:
        return True                      # 已在压了，别重复排队
    threading.Thread(target=_compress_async, args=(session_id, keep),
                     daemon=True, name=f"compress-{session_id}").start()
    return True


# 正在后台压缩的会话；daemon 线程随进程退出，不需要回收
_COMPRESSING: Dict[str, bool] = {}


def _compress_async(session_id: str, keep: int) -> None:
    """后台压缩：砍掉「除最近 keep 轮之外」的部分交给压缩模型

    轮数本来就不多却仍然超长时全部拿去压（不能让上下文无限涨）。

    关键：**模型调用期间不持锁**。压缩要跑十几秒（冷启动更久），
    锁要是握在手里，这期间所有新请求取上下文都得排队等它——
    所以先摘出待压缩的片段并落盘，再放开锁慢慢压，压完回来写摘要。
    压缩失败就把原文塞回去：宁可上下文长一点，也不能把游客说过的话弄丢。
    """
    if _COMPRESSING.get(session_id):
        return
    _COMPRESSING[session_id] = True
    try:
        with _LOCK:
            item = _get(session_id)
            turns = list(item.get("turns") or [])
            split = len(turns) - keep if len(turns) > keep else len(turns)
            if split <= 0:
                return
            old, item["turns"] = turns[:split], turns[split:]
            old_summary = str(item.get("summary") or "")
            item["updated"] = time.time()
            _save()

        summary = _summarize(old_summary, old)      # 无锁：慢活在这儿

        with _LOCK:
            item = _get(session_id)
            if summary:
                item["summary"] = summary
            else:
                item["turns"] = old + list(item.get("turns") or [])
            item["updated"] = time.time()
            _save()
        print(f"🧠 会话 {session_id} 历史已压缩：{split} 条 -> {len(summary)} 字摘要")
    except Exception as e:
        print(f"⚠️ 后台压缩异常（{type(e).__name__}），原文保留")
    finally:
        _COMPRESSING.pop(session_id, None)


def _summarize(old_summary: str, turns: List[dict]) -> str:
    """调压缩模型把旧摘要 + 一段历史压成一份新摘要；失败返回空串"""
    if not turns and not old_summary:
        return ""
    try:
        from core.chat.agent_engine import get_summary_llm
        from core.prompts.chat import CHAT_SUMMARY_PROMPT

        payload = json.dumps(
            {"old_summary": old_summary,
             "turns": [{"role": t.get("role"), "text": t.get("text")} for t in turns]},
            ensure_ascii=False)
        resp = get_summary_llm().invoke([SystemMessage(content=CHAT_SUMMARY_PROMPT),
                                        HumanMessage(content=payload)])
        text = str(getattr(resp, "content", "") or "").strip()
        return text[:1200]                  # 防止模型失控吐长文
    except Exception as e:
        print(f"⚠️ 会话历史压缩失败（{type(e).__name__}），保留原文继续对话")
        return ""


def history(session_id: str) -> dict:
    """读出这个会话记住的全部内容，给前端「会话历史」面板展示

    turns 里的 index 是轮次在列表中的下标，delete_turn 直接吃它。
    """
    with _LOCK:
        item = _get(session_id)
        turns = []
        for i, t in enumerate(item.get("turns") or []):
            turns.append({
                "index": i,
                "role": t.get("role") or "user",
                "text": str(t.get("text") or ""),
                "time": _fmt_ts(t.get("ts")),
            })
        summary = str(item.get("summary") or "").strip()
    # 注意别直接 **stats()：它也带一个 turns（那是条数 int），会把列表覆盖掉
    st = stats(session_id)
    return {"session_id": (session_id or _DEFAULT_SESSION).strip() or _DEFAULT_SESSION,
            "summary": summary, "turns": turns, "rounds": (len(turns) + 1) // 2,
            "chars": st["chars"], "summaryChars": st["summaryChars"],
            "compressed": st["compressed"]}


def list_sessions(current_id: str = "", preview_chars: int = PREVIEW_CHARS) -> List[dict]:
    """列出所有会话，每个只给「最后一句游客留言」当预览（给前端会话历史面板）

    会话历史面板的定位是**挑会话**，不是翻聊天记录，所以这里不返回全部轮次——
    要看某个会话的完整内容用 history(session_id)。
    预览优先取最后一条游客留言；全都已被压进摘要时退回用摘要开头。
    """
    cur = (current_id or "").strip()
    rows: List[dict] = []
    with _LOCK:
        for sid, item in _STORE.items():
            turns = list(item.get("turns") or [])
            # 空会话（没说过话也没摘要）不上榜：列表里全是能聊的会话
            if not turns and not str(item.get("summary") or "").strip():
                continue
            last_user = next((t for t in reversed(turns)
                              if (t.get("role") or "user") == "user"), None)
            preview = str((last_user or {}).get("text") or "").strip()
            if not preview:
                preview = str(item.get("summary") or "").strip()
            preview = " ".join(preview.split())          # 折叠换行，占一行
            if len(preview) > preview_chars:
                preview = preview[:preview_chars] + "…"
            rows.append({
                "session_id": sid,
                "preview": preview,
                "time": _fmt_ts(item.get("updated")),
                "rounds": (len(turns) + 1) // 2,
                "hasSummary": bool(str(item.get("summary") or "").strip()),
                "current": sid == cur,
                "_ts": float(item.get("updated") or 0),
            })
    rows.sort(key=lambda r: r["_ts"], reverse=True)
    for r in rows:
        r.pop("_ts", None)
    return rows


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def clear(session_id: Optional[str] = None) -> int:
    """清空某个会话（不传则清空全部），返回被清掉的会话数"""
    with _LOCK:
        _ensure_loaded()
        if session_id:
            existed = 1 if session_id in _STORE else 0
            _STORE.pop(session_id, None)
            _save()
            return existed
        n = len(_STORE)
        _STORE.clear()
        _save()
        return n


if __name__ == "__main__":
    sid = "demo"
    clear(sid)
    for i in range(6):
        record(sid, f"第{i+1}个问题：故宫明天开门吗？人多不多？我想避开人流。",
               f"第{i+1}个回答：故宫淡季 08:30 开门，16:30 停止入园，周一闭馆，建议上午入场。",
               threshold=300)          # 调小阈值，几轮就能看到压缩
        s = stats(sid)
        print(f"第{i+1}轮 -> 轮数 {s['turns']}，累计 {s['chars']} 字，"
              f"摘要 {s['summaryChars']} 字")
    for _ in range(40):                  # 压缩在后台跑，等它落地
        if stats(sid)["summaryChars"]:
            break
        time.sleep(0.5)
    print("\n注入上下文：")
    for m in context_messages(sid):
        print(f"  [{type(m).__name__}] {str(m.content)[:80]}...")
    print("\n摘要全文：\n", (_get(sid).get("summary") or "(无)"))
