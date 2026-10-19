"""联网实时搜索工具（Tavily）

零装包：直接用 requests 调用 Tavily 官方 REST 接口，不依赖 tavily-python。
适用于天气、票价、车次航班、新闻、当日景区开放状态等随时间变化的信息。
对外：
    search_web_raw(query, ...)   原始检索，返回 Tavily 原始 dict
    format_results(data)         结果整理成模型可读文本
    search_web                   LangChain @tool，供 Agent 调用
"""
import os
from typing import Any, Dict, Optional
import requests
from langchain_core.tools import tool

# ==================== 常量 ====================
NO_KEY_TIP = "【未配置 TAVILY_API_KEY，无法联网搜索】请如实告知用户暂无法获取实时信息，不要编造具体数据。"
NO_RESULT_TIP = "【联网搜索未找到结果】请如实告知用户暂未查到，不要编造具体信息。"

def _api_key() -> Optional[str]:
    """Tavily 密钥（优先 TAVILY_API_KEY，兼容 TAVILY_API_TOKEN）"""
    return os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_API_TOKEN")


def search_web_raw(query: str, max_results: int =5,
                   search_depth: str = "basic", topic: str = "general") -> Dict[str, Any]:
    """调用 Tavily 搜索接口
    :raises RuntimeError: 未配置密钥
    :raises requests.HTTPError: 接口返回非 2xx
    """
    key = _api_key()
    if not key:
        raise RuntimeError("TAVILY_API_KEY 未配置")

    payload = {
        "api_key": key,
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "max_results": max(1, min(int(max_results or 5), 10)),
        "include_answer": True,
        "include_images": False,
        "include_raw_content": False,
    }
    resp = requests.post(os.getenv("TAVILY_API_URL"), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json() or {}


def format_results(data: Dict[str, Any]) -> str:
    """Tavily 返回 -> 模型可读文本（综合答案 + 标题/链接/摘要）"""
    if not data:
        return ""
    parts = []
    answer = (data.get("answer") or "").strip()
    if answer:
        parts.append(f"[综合答案] {answer}")
    for i, item in enumerate(data.get("results") or [], 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        content = (item.get("content") or "").strip().replace("\n", " ")
        parts.append(f"{i}. {title}\n   链接: {url}\n   摘要: {content}")
    return "\n\n".join(parts)


@tool("search_web")
def search_web(query: str, max_results: int = 5) -> str:
    """通过 Tavily 联网搜索实时信息。
    当用户询问随时间变化、知识库里没有的实时信息时，必须调用本工具，例如：
    今天/明天的天气、当前门票价格与优惠活动、最新新闻、
    景区当日开放状态、节庆活动时间等。
    景区固有信息（景点介绍、历史文化、固定游览路线）请用 search_scenic_knowledge。
    Args:
        query: 联网搜索关键词，提炼成简洁的查询语句
        max_results: 返回条目数，1-10，默认 5
    """
    if not _api_key():
        return NO_KEY_TIP
    try:
        data = search_web_raw(query, max_results=max_results)
    except Exception as e:
        print(f"❌ 联网搜索失败: {e}")
        return f"【联网搜索失败：{e}】请如实告知用户暂时无法获取实时信息，不要编造具体数据。"
    return format_results(data) or NO_RESULT_TIP


if __name__ == "__main__":
    if not _api_key():
        print("未配置 TAVILY_API_KEY，请在 backend/.env 中添加后重启服务")
    else:
        print(search_web.invoke({"query": "灵山胜境景区 门票价格", "max_results": 3}))
