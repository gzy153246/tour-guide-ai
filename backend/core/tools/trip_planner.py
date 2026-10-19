"""出行票价规划：火车票（12306 MCP，免 key）+ 机票（SerpApi / Google Flights）

12306 无官方 API，`mcp-server-12306` 把其公开查询端点包成 MCP 工具，只查不订。
后续接其他 MCP 工具源只需往 `MCP_SERVERS` 加一条配置。

MCP 会话常驻独立后台 loop：stdio session 绑定创建它的 loop（不能借用 FastAPI 主线程
那个），且 uvx 拉子进程要数秒，会话复用可避免每次调用都重启。

失败路径一律返回「如实告知 / 不要编造」的文本，交给 AGENT_RULES 兜底。
接入 Agent（当前未接）：agent_engine.py 的 TOOLS 里追加 *TRIP_TOOLS
"""
import asyncio
import datetime as dt
import os
import re
import threading
from typing import Any, Dict, Optional

import requests
from langchain_core.tools import tool

#: MCP 服务器清单。新增工具源在此追加，值即 MultiServerMCPClient 的连接配置
MCP_SERVERS: Dict[str, Dict[str, Any]] = {
    "train12306": {"command": "uvx", "args": ["mcp-server-12306"], "transport": "stdio"},
}

SERPAPI_URL = "https://serpapi.com/search.json"
CONNECT_TIMEOUT = int(os.getenv("MCP_CONNECT_TIMEOUT", "90"))  # 首次含 uvx 拉包
CALL_TIMEOUT = int(os.getenv("MCP_CALL_TIMEOUT", "40"))

TRAIN_CODE = re.compile(r"\b([GDCZTKS]\d{1,4})\b")
PRICE_LEVELS = {"low": "偏低", "typical": "正常", "high": "偏高"}
DATE_OFFSETS = {"今天": 0, "当日": 0, "": 0, "明天": 1, "后天": 2}
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
NO_GUESS = "请如实告知用户暂未查到，不要编造具体票价与时间。"

# ==================== 后台事件循环与 MCP 调用 ====================

_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_sessions: Dict[str, tuple] = {}
_lock = threading.Lock()


def _bg():
    """单例后台事件循环（daemon 线程持有）"""
    global _bg_loop
    if _bg_loop is None:
        with _lock:
            if _bg_loop is None:
                _bg_loop = asyncio.new_event_loop()
                threading.Thread(target=_bg_loop.run_forever, name="mcp", daemon=True).start()
    return _bg_loop


def _discard(server: str) -> None:
    """丢弃失效会话，下次调用自动重连"""
    old = _sessions.pop(server, None)
    if old:
        try:
            asyncio.run_coroutine_threadsafe(old[1].__aexit__(None, None, None), _bg()).result(5)
        except Exception:
            pass


def _mcp_call(server: str, tool_name: str, arguments: Dict[str, Any]) -> str:
    """调用 MCP 工具并返回纯文本；会话复用，失效自动重连一次"""

    async def job():
        if server not in _sessions:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient({server: MCP_SERVERS[server]})
            ctx = client.session(server)
            _sessions[server] = (client, ctx, await ctx.__aenter__())
        result = await _sessions[server][2].call_tool(tool_name, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError("MCP 工具执行失败")
        blocks = getattr(result, "content", None) or []
        return "\n\n".join(
            (b.text if hasattr(b, "text") else b.get("text", "")).strip() for b in blocks
        ).strip()

    for attempt in range(2):
        try:
            timeout = CALL_TIMEOUT if server in _sessions else CONNECT_TIMEOUT
            return asyncio.run_coroutine_threadsafe(job(), _bg()).result(timeout)
        except Exception:
            _discard(server)
            if attempt:
                raise


# ==================== 地名与日期 ====================

#: 城市（关键词）-> 常用机场三字码；未命中则原样返回，便于直接传三字码
AIRPORTS = {
    "北京": "PEK", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "重庆": "CKG", "西安": "XIY", "杭州": "HGH", "南京": "NKG", "武汉": "WUH",
    "长沙": "CSX", "昆明": "KMG", "厦门": "XMN", "青岛": "TAO", "大连": "DLC",
    "沈阳": "SHE", "哈尔滨": "HRB", "郑州": "CGO", "天津": "TSN", "贵阳": "KWE",
    "南宁": "NNG", "海口": "HAK", "三亚": "SYX", "兰州": "LHW", "太原": "TYN",
    "乌鲁木齐": "URC", "拉萨": "LXA", "无锡": "WUX", "常州": "CZX", "宁波": "NGB",
    "温州": "WNZ", "合肥": "HFE", "福州": "FOC", "济南": "TNA", "南昌": "KHN",
    "西宁": "XNN", "桂林": "KWL", "丽江": "LJG", "徐州": "XUZ", "台北": "TPE",
    "香港": "HKG", "首尔": "ICN", "东京": "HND", "曼谷": "BKK", "新加坡": "SIN",
}


def _airport(city: str) -> str:
    """城市名或三字码 -> 机场三字码"""
    name = (city or "").strip()
    if re.fullmatch(r"[A-Za-z]{3}", name):
        return name.upper()
    for key, code in AIRPORTS.items():
        if key in name or name in key:
            return code
    return name


def _station(city: str) -> str:
    """城市名 -> 12306 站名（多数城市名即站名，仅去掉行政后缀）"""
    return (city or "").strip().rstrip("市区县")


def _date(text: str) -> str:
    """归一成 YYYY-MM-DD；无法识别时原样返回，交给下游处理"""
    today = dt.date.today()
    s = (text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if m := re.fullmatch(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", s):
        return dt.date(*map(int, m.groups())).isoformat()
    if s in DATE_OFFSETS:
        return (today + dt.timedelta(days=DATE_OFFSETS[s])).isoformat()
    if m := re.fullmatch(r"(本|下)?周([一二三四五六日天])", s):
        days = (WEEKDAYS[m[2]] - today.weekday()) % 7 + (7 if m[1] == "下" else 0)
        return (today + dt.timedelta(days=days)).isoformat()
    return s


# ==================== 工具 ====================

@tool("search_train_tickets")
def search_train_tickets(from_city: str, to_city: str,
                         train_date: str = "", need_price: bool = False) -> str:
    """查询境内火车/高铁的车次、时刻与余票，需要时附票价。只查不订，无需用户账号。
    用户问「怎么坐火车去某地、有几趟、几点到、多久、还有票吗、票价多少」时调用。
    Args:
        from_city: 出发城市或车站名，如「无锡」「北京南」
        to_city: 到达城市或车站名，如「南京」
        train_date: 乘车日期，支持 YYYY-MM-DD 或「今天/明天/后天/周六/下周三」，留空为今天
        need_price: 是否查票价。只在用户明确问票价时传 True，可省下多次往返查询
    """
    args = {"from_station": _station(from_city), "to_station": _station(to_city),
            "train_date": _date(train_date)}
    try:
        raw = _mcp_call("train12306", "query-tickets", args)
    except Exception as e:
        print(f"❌ 火车票查询失败: {e}")
        return f"【火车票查询失败：{e}】{NO_GUESS}"

    # 只保留含车次号的数据行，自动滤掉表头与分隔线
    lines = [ln.strip().strip("|").strip() for ln in (raw or "").splitlines()]
    lines = [ln for ln in lines
             if TRAIN_CODE.search(ln) and not set(ln) <= set("-| :")][:8]
    if not lines:
        return f"【{args['train_date']} {args['from_station']}→{args['to_station']} 未查到车次】{NO_GUESS}"

    out = [f"查询日期: {args['train_date']}　{args['from_station']} → {args['to_station']}", "", *lines]
    if need_price:
        out += ["", "票价："]
        got = 0
        for line in lines[:4]:  # 限 4 次，避免触发 12306 限频
            code = TRAIN_CODE.search(line).group(1)
            try:
                no_text = _mcp_call("train12306", "get-train-no-by-train-code",
                                    {**args, "train_code": code})
                ids = re.findall(r"[0-9A-Za-z]{6,}", (no_text or "").strip().splitlines()[-1])
                if not ids:
                    continue
                price = _mcp_call("train12306", "query-ticket-price",
                                  {**args, "train_no": ids[-1]})
                out.append(f"- {code}: {price.strip()}")
                got += 1
            except Exception as e:
                out.append(f"- {code}: 票价查询失败（{e}）")
        if not got:
            out.append("未能取得票价，请勿编造金额。")
    return "\n".join(out)


@tool("search_flight_prices")
def search_flight_prices(from_city: str, to_city: str, date: str = "", limit: int = 5) -> str:
    """查询境内外航班的实时票价与时刻（Google Flights 数据源）。
    用户问「机票多少钱、哪趟便宜、几点起飞、飞多久」时调用；
    中短途（如无锡→南京）建议同时用 search_train_tickets 对比高铁方案。
    Args:
        from_city: 出发城市名或机场三字码，如「无锡」「北京」「PVG」
        to_city: 到达城市名或机场三字码，如「三亚」
        date: 出发日期，支持 YYYY-MM-DD 或「今天/明天/后天/周六/下周三」，留空为今天
        limit: 返回航班条数，1-10，默认 5
    """
    dep, arr, day = _airport(from_city), _airport(to_city), _date(date)
    try:
        if not (key := os.getenv("SERPAPI_API_KEY")):
            raise RuntimeError("未配置 SERPAPI_API_KEY")
        resp = requests.get(SERPAPI_URL, timeout=25, params={
            "engine": "google_flights", "departure_id": dep, "arrival_id": arr,
            "outbound_date": day, "currency": "CNY", "hl": "zh-cn", "gl": "cn",
            "deep_search": "true",  # 保证与浏览器所见一致，代价是响应更慢
            "api_key": key,
        })
        resp.raise_for_status()
        if (data := resp.json() or {}).get("error"):
            raise RuntimeError(data["error"])
    except Exception as e:
        print(f"❌ 机票查询失败: {e}")
        return f"【机票查询失败：{e}】请如实告知用户暂无法获取实时票价，不要估算价格。"

    lines = []
    if level := (data.get("price_insights") or {}).get("price_level"):
        lines.append(f"[价格水平] 相对历史同期{PRICE_LEVELS.get(level, level)}")
    n = max(1, min(int(limit or 5), 10))
    for bucket, label in (("best_flights", "推荐航班"), ("other_flights", "其他航班")):
        blocks = (data.get(bucket) or [])[:n]
        if not blocks:
            continue
        lines.append(f"== {label} ==")
        for blk in blocks:
            lines.append(f"- {blk.get('price')} 元 | 全程 {blk.get('total_duration')} 分钟")
            for leg in blk.get("flights") or []:
                d, a = leg.get("departure_airport") or {}, leg.get("arrival_airport") or {}
                lines.append(f"    {leg.get('airline', '')}{leg.get('flight_number', '')} "
                             f"{d.get('id', '')} {d.get('time', '')} → "
                             f"{a.get('id', '')} {a.get('time', '')}")
    if not lines:
        return f"【{day} {dep}→{arr} 未查询到航班】{NO_GUESS}"
    return "\n".join([f"查询日期: {day}　{dep} → {arr}", "", *lines,
                      "", "数据来源：Google Flights，价格随余位浮动，以航司官网为准"])


#: 本文件的工具清单，接入 Agent 时并入 TOOLS
TRIP_TOOLS = [search_train_tickets, search_flight_prices]


if __name__ == "__main__":
    print([t.name for t in TRIP_TOOLS])
    print("日期:", [(v, _date(v)) for v in ["", "明天", "下周六", "2026年12月1日"]])
    print("机场:", [(v, _airport(v)) for v in ["无锡", "北京", "PVG", "三亚"]])
