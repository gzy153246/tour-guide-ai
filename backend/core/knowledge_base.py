"""景区知识库 - 基于 ChromaDB 向量存储

职责：文档切分、向量化、检索
文档解析（多格式读取、OCR、清洗去重）见 core/document_loader.py

两者通过 DocumentLoader 解耦：本模块只接收结构化文档，不关心文件来自哪种格式。
"""
import os
import time
from typing import Dict, List

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter,
)
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.vectorstores import Chroma

try:
    from core.document_loader import (
        DocumentLoader, MinerUCloudClient, MinerUError, _force_utf8_streams,
    )
except ImportError:  # 兼容 `python core/knowledge_base.py` 直接运行
    from document_loader import (
        DocumentLoader, MinerUCloudClient, MinerUError, _force_utf8_streams,
    )

_force_utf8_streams()  # 幂等

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
DEFAULT_DATA_DIR = os.path.join(_PROJECT_DIR, "data", "raw")
DEFAULT_VECTOR_DB_PATH = os.path.join(_PROJECT_DIR, "data", "chroma_storage")


class KnowledgeBase:
    """向量知识库：拼接 DocumentLoader 的输出，完成切分与检索"""

    def __init__(self, data_dir=DEFAULT_DATA_DIR, vector_db_path=DEFAULT_VECTOR_DB_PATH,
                 mineru_token=None, mineru_model="pipeline", pdf_parser="auto",
                 pdf_text_threshold=50, max_file_mb=200, max_pdf_pages=600,
                 min_content_length=10, recursive=False, enable_dedup=True):
        self.vector_db_path = vector_db_path
        self.loader = DocumentLoader(
            data_dir=data_dir,
            mineru_token=mineru_token,
            mineru_model=mineru_model,
            pdf_parser=pdf_parser,
            pdf_text_threshold=pdf_text_threshold,
            max_file_mb=max_file_mb,
            max_pdf_pages=max_pdf_pages,
            min_content_length=min_content_length,
            recursive=recursive,
            enable_dedup=enable_dedup,
        )

        self.embeddings = DashScopeEmbeddings(
            model="text-embedding-v2",
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY")
        )

        # 正文切分：到达 chunk_size 上限时按中文标点降级切分
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]
        )

        # Markdown 标题感知切分：md/markdown 先按 # / ## 分层，
        # 再把「文件名 + 章节路径」作为前缀写回每个文本块，
        # 避免切出来的块里不知道在讲哪个景点（参见 _split_markdown）
        self.md_headers = [("#", "chapter"), ("##", "section")]
        self.md_header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.md_headers, strip_headers=True
        )

        # 集合名保持中立：库里装什么目的地的内容由 data/raw 决定，不绑定具体景区
        self.collection_name = "tour_knowledge"
        self._vector_store = None      # search 实例缓存
        self.stats = {}                # 最近一次构建/加载统计

    @property
    def data_dir(self):
        return self.loader.data_dir

    @property
    def mineru(self):
        """转发到 loader，保留 kb.mineru 的旧访问方式"""
        return self.loader.mineru

    def load_documents(self):
        """委托给 DocumentLoader，并把统计同步过来"""
        docs = self.loader.load_documents()
        self.stats.update(self.loader.stats)
        return docs

    # ==================== 向量库 ====================
    @staticmethod
    def _hash(content: str) -> str:
        """文档内容指纹：用于判断文件是否发生过变更"""
        import hashlib
        return hashlib.md5((content or "").encode("utf-8", "ignore")).hexdigest()

    def _split_markdown(self, doc: Dict) -> tuple:
        """Markdown 文档切分：按标题分层 + 章节路径写回块首 + 超长再降级切

        普通切分器会把「一个景点的一节」从中间劈开，切出来的块既不完整也不知
        所说何物。这里先按 # / ## 切成完整小节，再把「文件名 > 章节 > 景点」
        拼到每个块开头，让每个块都能自报家门。
        """
        file_type = (doc.get("file_type") or "").lower()
        source = doc.get("source", "")
        if file_type not in ("md", "markdown"):
            return None

        try:
            sections = self.md_header_splitter.split_text(doc["content"])
        except Exception as e:
            print(f"⚠️  {source} Markdown 标题切分失败，改用普通切分: {e}")
            return None
        if not sections:
            return None

        file_hash = self._hash(doc["content"])
        texts, metadatas = [], []
        for sec in sections:
            header = " > ".join(
                str(sec.metadata[k]) for k in ("chapter", "section")
                if sec.metadata.get(k)
            )
            prefix = f"[来源: {source}]"
            if header:
                prefix += f" [章节: {header}]"
            prefix += "\n"
            body = (sec.page_content or "").strip()
            if not body:
                continue
            pieces = (self.splitter.split_text(body)
                      if len(body) > 500 else [body])
            for piece in pieces:
                if not piece.strip():
                    continue
                texts.append(prefix + piece.strip())
                metadatas.append({
                    "source": source,
                    "chunk_id": len(texts),
                    "file_type": file_type,
                    "parser": doc.get("parser", ""),
                    "file_hash": file_hash,
                    "header": header,
                })
        return (texts, metadatas) if texts else None

    def _split_docs(self, docs):
        all_texts, all_metadatas = [], []
        for doc in docs:
            md_result = self._split_markdown(doc)
            if md_result is not None:
                texts, metadatas = md_result
                all_texts.extend(texts)
                all_metadatas.extend(metadatas)
                continue

            chunks = self.splitter.split_text(doc["content"])
            file_hash = self._hash(doc["content"])
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                all_texts.append(chunk)
                all_metadatas.append({
                    "source": doc.get("source", ""),
                    "chunk_id": i,
                    "file_type": doc.get("file_type", ""),
                    "parser": doc.get("parser", ""),
                    "file_hash": file_hash,
                    "header": "",
                })
        return all_texts, all_metadatas

    def _drop_collection(self):
        """清空整个 collection（全量重建前调用，避免历史数据累积）"""
        try:
            import chromadb
            client = chromadb.PersistentClient(path=self.vector_db_path)
            try:
                client.delete_collection(self.collection_name)
                print(f"🧹 已清空旧向量库: {self.collection_name}")
            except Exception:
                pass  # 原本就不存在
        except Exception as e:
            print(f"⚠️  清空旧向量库失败: {e}")
        self._vector_store = None

    def _delete_source(self, source: str) -> int:
        """删除某个来源文件的全部块（保证重复写入同一文件时幂等）"""
        if not source:
            return 0
        store = self._get_vector_store()
        try:
            got = store._collection.get(where={"source": source}, include=[])
            ids = got.get("ids") or []
            if ids:
                store._collection.delete(ids=ids)
            return len(ids)
        except Exception as e:
            print(f"⚠️  删除旧块失败 [{source}]: {e}")
            return 0

    def _source_hashes(self) -> Dict[str, str]:
        """读取库中已有文件的指纹：{文件名: hash}"""
        store = self._get_vector_store()
        try:
            data = store._collection.get(include=["metadatas"])
        except Exception as e:
            print(f"⚠️  读取向量库元数据失败: {e}")
            return {}
        out = {}
        for m in data.get("metadatas") or []:
            if not m:
                continue
            s = m.get("source")
            if s:
                out[s] = m.get("file_hash", "")
        return out

    def sync_documents(self):
        """增量同步：只写新增/变更的文件，未变动的跳过，已删除的移除

        :return: {'added': 新增或更新的文件数, 'removed': 移除的文件数, 'chunks': 写入块数}
        """
        docs = self.load_documents()
        if not docs:
            print("⚠️  未加载到任何文档，跳过同步")
            return {"added": 0, "removed": 0, "chunks": 0}

        stored = self._source_hashes()
        current = {d["source"]: self._hash(d["content"]) for d in docs}

        changed = [d for d in docs if current[d["source"]] != stored.get(d["source"])]
        stale = [s for s in stored if s not in current]

        for s in stale:
            self._delete_source(s)

        chunks = self.add_documents(changed) if changed else 0
        print(f"🔄 同步完成：新增/更新 {len(changed)} 个文件，移除 {len(stale)} 个，写入 {chunks} 块")
        return {"added": len(changed), "removed": len(stale), "chunks": chunks}

    def build_vector_store(self):
        """全量重建：先清库再写入（用于首次构建或强制重建）"""
        started = time.time()
        docs = self.load_documents()
        all_texts, all_metadatas = self._split_docs(docs)
        print(f"✂️  切分为 {len(all_texts)} 个文本块")

        if not all_texts:
            print("⚠️  无有效文本，跳过向量库构建")
            return None

        self._drop_collection()

        vector_store = Chroma.from_texts(
            texts=all_texts,
            embedding=self.embeddings,
            metadatas=all_metadatas,
            collection_name=self.collection_name,
            persist_directory=self.vector_db_path
        )
        self._vector_store = vector_store
        self.stats["chunks"] = len(all_texts)
        self.stats["elapsed"] = round(time.time() - started, 1)
        print(f"✅ 向量库构建完成：{len(all_texts)} 条记录，耗时 {self.stats['elapsed']}s")
        return vector_store

    def add_documents(self, docs):
        """增量添加文档，避免全量重建索引

        幂等：写入前先删掉同名文件的旧块，重复添加同一文件不会累积副本
        """
        if not docs:
            return 0
        all_texts, all_metadatas = self._split_docs(docs)
        if not all_texts:
            return 0
        store = self._get_vector_store()
        for src in {m["source"] for m in all_metadatas if m.get("source")}:
            self._delete_source(src)
        store.add_texts(texts=all_texts, metadatas=all_metadatas)
        print(f"➕ 增量写入 {len(all_texts)} 个文本块")
        return len(all_texts)

    def _get_vector_store(self, refresh=False):
        if self._vector_store is not None and not refresh:
            return self._vector_store
        self._vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.vector_db_path
        )
        return self._vector_store

    def search(self, query: str, k: int = 5, refresh: bool = False):
        """检索相关文档（复用向量库实例，避免每次重建连接）"""
        try:
            vector_store = self._get_vector_store(refresh=refresh)
            docs = vector_store.similarity_search(query, k=k)
        except Exception as e:
            print(f"⚠️  检索失败，尝试重建连接: {e}")
            vector_store = self._get_vector_store(refresh=True)
            docs = vector_store.similarity_search(query, k=k)

        results = []
        for doc in docs:
            results.append({
                "content": doc.page_content,
                "source": doc.metadata.get("source", ""),
                "file_type": doc.metadata.get("file_type", ""),
            })
        return results


if __name__ == "__main__":
    print("=" * 50)
    print("🏗️  旅游知识库构建 (ChromaDB)")
    print("=" * 50)

    kb = KnowledgeBase()
    kb.build_vector_store()

    print("\n" + "=" * 50)
    print("🔍 检索测试")
    print("=" * 50)

    queries = ["故宫门票多少钱", "颐和园怎么逛最合理", "北京有哪些必吃美食"]
    for q in queries:
        print(f"\n❓ {q}")
        results = kb.search(q, k=2)
        if results:
            r = results[0]
            print(f"  📖 来源: {r['source']}")
            print(f"  📝 {r['content'][:150]}...")
        else:
            print("  ⚠️  未找到相关结果")

    print("\n" + "=" * 50)
    print("📊 构建统计")
    print("=" * 50)
    for key, value in kb.stats.items():
        print(f"  {key}: {value}")
