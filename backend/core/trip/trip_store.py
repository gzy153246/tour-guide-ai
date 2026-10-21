"""行程持久化：一条行程一个 JSON 文件

目录：<项目根>/data/trip/plans/{plan_id}.json
约定：
  - plan_id 只允许 [A-Za-z0-9_-]，杜绝路径穿越
  - 写盘用「临时文件 + os.replace」原子替换，配合线程锁，并发不会写坏
  - 列表按文件名倒序（文件名前缀是时间戳）再截断，等价于按创建时间倒序

对外：
    save_plan(plan)        落盘并返回带 id 的行程
    list_plans(limit)      摘要列表（不含 planDays，历史页够用）
    get_plan(plan_id)      完整行程
    delete_plan(plan_id)
    clear_plans()
    save_plan_map(plan_id, day_key, payload)  缓存地图点位，避免重复打高德
"""
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# 层数：core/trip/trip_store.py -> trip -> core -> backend -> 项目根
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)                                # 项目根，data 在这里
PLAN_DIR = os.path.join(_PROJECT_DIR, "data", "trip", "plans")
MAX_PLANS = 200

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.Lock()

SUMMARY_FIELDS = ("dest", "origin", "date", "days", "people", "pace", "paceName",
                  "budgetAmount", "budgetPerDay", "budgetTierName", "transportName",
                  "interests", "interestNames", "diet", "extra", "createdAt", "summary",
                  "degraded", "polished")


def _ensure_dir():
    os.makedirs(PLAN_DIR, exist_ok=True)


def _path(plan_id: str) -> str:
    if not _ID_RE.match(plan_id or ""):
        raise ValueError("非法的行程编号")
    return os.path.join(PLAN_DIR, f"{plan_id}.json")


def new_plan_id() -> str:
    """文件名前缀是时间戳，天然按创建时间倒序排列"""
    return "tp_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def summarize(plan: dict) -> dict:
    """历史列表用的轻量摘要：不返回 planDays，省带宽"""
    out = {"id": plan.get("id", "")}
    for k in SUMMARY_FIELDS:
        if k in plan:
            out[k] = plan[k]
    return out


def save_plan(plan: dict) -> dict:
    """写入一条行程，回填 id 后返回原对象"""
    _ensure_dir()
    plan = dict(plan)
    pid = plan.get("id") or new_plan_id()
    if not _ID_RE.match(pid):
        pid = new_plan_id()
    plan["id"] = pid
    path = _path(pid)
    with _lock:
        _atomic_write(path, plan)
        _trim()
    return plan


def _atomic_write(path: str, data: Any):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=PLAN_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _trim():
    """只保留最近的 MAX_PLANS 条；调用方需持有 _lock"""
    files = sorted([f for f in os.listdir(PLAN_DIR) if f.endswith(".json")], reverse=True)
    for f in files[MAX_PLANS:]:
        try:
            os.remove(os.path.join(PLAN_DIR, f))
        except OSError:
            pass


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_plans(limit: int = 20) -> List[dict]:
    _ensure_dir()
    limit = max(1, min(int(limit or 20), MAX_PLANS))
    files = sorted([f for f in os.listdir(PLAN_DIR) if f.endswith(".json")], reverse=True)
    out = []
    for f in files[:limit]:
        plan = _read(os.path.join(PLAN_DIR, f))
        if plan:
            out.append(summarize(plan))
    return out


def count_plans() -> int:
    _ensure_dir()
    return len([f for f in os.listdir(PLAN_DIR) if f.endswith(".json")])


def get_plan(plan_id: str) -> Optional[dict]:
    try:
        path = _path(plan_id)
    except ValueError:
        return None
    if not os.path.exists(path):
        return None
    return _read(path)


def delete_plan(plan_id: str) -> bool:
    try:
        path = _path(plan_id)
    except ValueError:
        return False
    with _lock:
        if not os.path.exists(path):
            return False
        os.remove(path)
        return True


def clear_plans() -> int:
    _ensure_dir()
    n = 0
    with _lock:
        for f in os.listdir(PLAN_DIR):
            if f.endswith(".json"):
                try:
                    os.remove(os.path.join(PLAN_DIR, f))
                    n += 1
                except OSError:
                    pass
    return n


def save_plan_map(plan_id: str, day_key: str, payload: dict) -> bool:
    """把某张图的点位缓存进该行程文件，避免每次切换都重复调用高德"""
    with _lock:
        plan = get_plan(plan_id)
        if not plan:
            return False
        maps = plan.get("maps") or {}
        maps[str(day_key)] = payload
        plan["maps"] = maps
        try:
            _atomic_write(_path(plan_id), plan)
        except ValueError:
            return False
        return True


if __name__ == "__main__":
    p = {"dest": "北京市", "days": 2, "summary": {"spots": 4}, "planDays": []}
    saved = save_plan(p)
    print("写入:", saved["id"])
    print("列表:", [x["id"] for x in list_plans()][:3])
    print("读取:", get_plan(saved["id"])["dest"])
    print("地图缓存:", save_plan_map(saved["id"], "all", {"markers": []}))
    print("删除:", delete_plan(saved["id"]), "剩余:", count_plans())
