"""景区知识检索工具

以函数形式提供检索能力，并用 @tool 暴露给 Agent 调用。
本模块零 LLM、零提示词：只负责「问题 -> 知识库原文片段」。

对外：
  retrieve() / format_docs() / rebuild_index() / last_sources()   检索能力
  search_scenic_knowledge                                         LangChain 工具
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional
from langchain_core.tools import tool

# 保证以任意工作目录启动时都能正确导入 core 包与定位数据目录
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.knowledge_base import KnowledgeBase

# ==================== 路径常量 ====================
_PROJECT_DATA_DIR = _BACKEND_DIR.parent / "data"
DEFAULT_DATA_DIR = str(_PROJECT_DATA_DIR / "raw")
DEFAULT_VECTOR_DB_PATH = str(_PROJECT_DATA_DIR / "chroma_storage")

# 向量库单例（打开索引有开销，懒加载并复用）
_kb: Optional[KnowledgeBase] = None
# 最近一次检索命中的来源，供调用方回传给前端
_last_sources: List[str] = []


def get_kb() -> KnowledgeBase:
    """获取向量库实例（懒加载，全局复用）"""
    global _kb
    if _kb is None:
        _kb = KnowledgeBase(
            data_dir=DEFAULT_DATA_DIR,
            vector_db_path=DEFAULT_VECTOR_DB_PATH,
        )
    return _kb


def retrieve(query: str, top_k: int = 3) -> List[Dict]:
    """检索知识库
    :return: [{'source': 文件名, 'content': 原文, ...}, ...]；无命中或失败返回空列表
    """
    global _last_sources
    _last_sources = []
    if not query or not query.strip():
        return []
    try:
        docs = get_kb().search(query, k=top_k) or []
    except Exception as e:
        print(f"❌ 知识库检索失败: {e}")
        return []
    _last_sources = [d["source"] for d in docs if d.get("source")]
    return docs


def format_docs(docs: List[Dict]) -> str:
    """片段拼接成可供模型阅读的上下文"""
    if not docs:
        return ""
    return "\n\n".join(f"[来源: {d['source']}]\n{d['content']}" for d in docs)


def rebuild_index():
    """增量同步知识库：只处理新增/变更的文件，未变动的不重复计算 Embedding"""
    print("🔄 同步知识库向量索引...")
    get_kb().sync_documents()
    print("✅ 向量索引同步完成")

def last_sources() -> List[str]:
    """最近一次检索命中的来源文件名"""
    return list(_last_sources)

@tool("search_scenic_knowledge")
def search_scenic_knowledge(query: str, top_k: int = 3) -> str:
    """检索北京旅游知识库，返回景点资料原文片段。

    当用户询问北京的景点、历史人文、餐饮美食、市内交通、行程安排，
    或涉及门票、开放时间、游览时长、游览路线等内容时，必须调用本工具。
    已有资料覆盖：故宫、天安门广场、天坛、颐和园、圆明园、长城、明十三陵、
    雍和宫、恭王府、什刹海、国家博物馆、首都博物馆等景点，以及北京出行须知。
    与北京旅游无关的问题（如问候、闲聊、通用知识）无需调用。

    Args:
        query: 检索关键词，提炼成景点或需求相关的短句，如"故宫 门票 开放时间"
        top_k: 返回片段条数，默认 3
    """
    docs = retrieve(query, top_k=max(1, min(int(top_k or 3), 10)))
    if not docs:
        return "【未检索到相关资料】请如实告知用户暂未查到，不要编造具体信息。"
    return format_docs(docs)


if __name__ == "__main__":
    hits = retrieve("故宫门票多少钱", top_k=2)
    print(f"命中 {len(hits)} 条，来源 {last_sources()}")
    for h in hits:
        print(f"- {h['source']}: {h['content'][:60]}...")
