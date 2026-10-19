"""
医学知识库（Milvus）

功能：
1. 文档向量化和存储
2. 语义检索
3. 知识库管理

参考实现：/Users/saintgeo/Desktop/self-learn/shanglv
"""
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer
from dataclasses import replace

from knowledge.chunking import DEFAULT_POLICY, split_text


def delete_document_vectors(
    document_id: int,
    db_path: str = "./knowledge/data/milvus_lite.db",
    collection_name: str = "medical_knowledge",
) -> bool:
    """Delete a document's vectors without loading the embedding model."""
    resolved_path = os.getenv("MEDIX_MILVUS_DB_PATH", db_path)
    if not Path(resolved_path).exists():
        return False
    client = MilvusClient(resolved_path)
    if not client.has_collection(collection_name):
        return False
    expression = f'metadata like "%\\"document_record_id\\": {int(document_id)}%"'
    client.delete(collection_name, filter=expression)
    logger.info(f"Removed vectors for document_record_id={document_id}")
    return True


class MedicalKnowledgeBase:
    """医学知识库"""

    _instance = None

    def __new__(cls, *args, **kwargs):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        db_path: str = "./knowledge/data/milvus_lite.db",
        collection_name: str = "medical_knowledge",
        embedding_model: str = "BAAI/bge-small-zh-v1.5"
    ):
        """
        初始化医学知识库

        Args:
            db_path: Milvus Lite 数据库文件路径
            collection_name: Collection 名称
            embedding_model: Embedding 模型名称或本地路径
        """
        # 防止重复初始化
        if hasattr(self, '_initialized'):
            return

        self.db_path = os.getenv("MEDIX_MILVUS_DB_PATH", db_path)
        self.collection_name = collection_name
        self.embedding_model_name = os.getenv("MEDIX_EMBEDDING_MODEL", embedding_model)

        # 确保数据目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # 初始化 Embedding 模型（支持本地路径）
        # 优先检查本地缓存路径
        hf_home = Path(os.getenv("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
        model_cache_path = hf_home / "hub" / f"models--{self.embedding_model_name.replace('/', '--')}" / "snapshots"

        if model_cache_path.exists():
            # 找到最新的 snapshot
            snapshots = sorted(
                (p for p in model_cache_path.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if snapshots:
                model_path = str(snapshots[0])
                logger.info(f"Loading embedding model from local cache: {model_path}")
                self.embedding_model = SentenceTransformer(model_path, device='cpu')
            else:
                logger.info(f"Loading embedding model: {self.embedding_model_name}")
                self.embedding_model = SentenceTransformer(self.embedding_model_name, device='cpu')
        else:
            logger.info(
                f"Loading embedding model: {self.embedding_model_name} "
                f"(HF_ENDPOINT={os.getenv('HF_ENDPOINT', 'default')}, "
                f"timeout={os.getenv('HF_HUB_DOWNLOAD_TIMEOUT', 'default')}s)"
            )
            try:
                self.embedding_model = SentenceTransformer(self.embedding_model_name, device='cpu')
            except Exception as exc:
                raise RuntimeError(
                    "Embedding model is unavailable. Check HF_ENDPOINT/network or pre-download "
                    "the model into the persistent Hugging Face cache."
                ) from exc

        self.embedding_dim = self.embedding_model.get_sentence_embedding_dimension()
        logger.info(f"Embedding model loaded (dimension={self.embedding_dim})")

        # 初始化 Milvus Lite
        logger.info(f"Connecting to Milvus Lite: {self.db_path}")
        self.milvus_client = MilvusClient(self.db_path)

        # 创建 collection（如果不存在）
        if not self.milvus_client.has_collection(collection_name):
            logger.info(f"Creating collection: {collection_name}")
            self.milvus_client.create_collection(
                collection_name=collection_name,
                dimension=self.embedding_dim,
                metric_type="COSINE",  # 余弦相似度
                auto_id=True  # 自动生成整数ID
            )
        else:
            logger.info(f"Collection already exists: {collection_name}")

        # 加载 collection 到内存（Milvus 需 load 后才能 search/query）
        self.milvus_client.load_collection(collection_name)
        logger.info(f"Collection loaded: {collection_name}")

        self._initialized = True

    def _chunk_text(self, text: str, chunk_size: Optional[int] = None, overlap: int = 0) -> List[str]:
        """
        分块文本

        Args:
            text: 原始文本
            chunk_size: 兼容旧调用参数；新逻辑统一使用 token 策略

        Returns:
            文本块列表
        """
        if chunk_size is not None and chunk_size <= 0:
            return [text]
        policy = DEFAULT_POLICY if chunk_size is None else replace(DEFAULT_POLICY, max_tokens=chunk_size)
        # 使用 embedding 模型自带 tokenizer，确保切分上限与实际向量化模型一致。
        model = getattr(self, "embedding_model", None)
        model_tokenizer = getattr(model, "tokenizer", None)
        return split_text(text, policy, tokenizer=model_tokenizer)

    def add_documents(self, documents: List[Dict[str, Any]], chunk_size: Optional[int] = None) -> int:
        """
        添加文档到知识库（支持分块）

        Args:
            documents: 文档列表，每个文档包含 id, content, metadata
            chunk_size: 传 0 表示文档已按统一策略切分；其他情况使用 token/句子策略

        Returns:
            成功添加的文档块数量
        """
        if not documents:
            logger.warning("No documents to add")
            return 0

        logger.info(f"Adding {len(documents)} documents to knowledge base (chunk_size={chunk_size})...")

        # 分块并向量化
        all_chunks = []
        for doc in documents:
            # Ingestion jobs pass already-persisted chunks with chunk_size=0 so
            # the SQLite chunk record and Milvus vector remain one-to-one.
            chunks = [doc["content"]] if chunk_size == 0 else self._chunk_text(doc["content"], chunk_size=chunk_size)
            for i, chunk in enumerate(chunks):
                metadata = doc.get("metadata", {}).copy()
                metadata["doc_id"] = doc["id"]
                # Ingestion may already provide the persisted SQLite chunk
                # index. Preserve it so citation clicks resolve exact content.
                metadata.setdefault("chunk_id", i)
                metadata["total_chunks"] = len(chunks)

                all_chunks.append({
                    "content": chunk,
                    "metadata": metadata
                })

        logger.info(f"Split into {len(all_chunks)} chunks")

        # 向量化
        contents = [chunk["content"] for chunk in all_chunks]
        vectors = self.embedding_model.encode(contents, show_progress_bar=True)

        # 准备数据
        data = []
        for i, chunk in enumerate(all_chunks):
            data.append({
                "vector": vectors[i].tolist(),
                "content": chunk["content"],
                "metadata": json.dumps(chunk["metadata"], ensure_ascii=False)
            })

        # 插入
        self.milvus_client.insert(self.collection_name, data)
        logger.info(f"Successfully added {len(data)} chunks")

        return len(data)

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        检索相关文档

        Args:
            query: 查询文本
            top_k: 返回top K个结果
            filter_type: 可选的类型过滤（如 "lifestyle", "disease_classification"）

        Returns:
            文档列表，每个文档包含 id, content, metadata, score
        """
        logger.debug(f"Searching for: {query} (top_k={top_k}, filter_type={filter_type})")

        # 向量化查询
        query_vector = self.embedding_model.encode([query])[0]

        filter_expr = self._type_filter(filter_type) if filter_type else None
        return self._search_encoded(query_vector.tolist(), top_k, filter_expr)

    def search_by_sources(
        self,
        query: str,
        source_types: List[str],
        top_k_per_source: int = 5,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Search each knowledge corpus independently with one query embedding.

        The paper retrieves an equal quota from every corpus before global
        reranking. Keeping the result lists separate prevents a large corpus
        from crowding out guidelines, labels, textbooks, or research papers.
        """
        query_vector = self.embedding_model.encode([query])[0].tolist()
        output: Dict[str, List[Dict[str, Any]]] = {}
        for source_type in dict.fromkeys(source_types):
            if not source_type:
                continue
            results = self._search_encoded(
                query_vector,
                max(1, int(top_k_per_source)),
                self._type_filter(source_type),
            )
            if results:
                output[source_type] = results
        return output

    @staticmethod
    def _type_filter(filter_type: str) -> str:
        """Build a safe JSON-string filter for the stored metadata field."""
        normalized = str(filter_type).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", normalized):
            raise ValueError(f"Invalid knowledge source type: {filter_type}")
        return f'metadata like "%\\"type\\": \\"{normalized}\\"%"'

    def _search_encoded(
        self,
        query_vector: List[float],
        top_k: int,
        filter_expr: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Execute and normalize a Milvus search for a precomputed vector."""
        try:
            results = self.milvus_client.search(
                collection_name=self.collection_name,
                data=[query_vector],
                limit=top_k,
                filter=filter_expr,
                output_fields=["content", "metadata"]
            )
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

        # 格式化结果
        documents = []
        for hits in results:
            for hit in hits:
                try:
                    documents.append({
                        "id": hit["id"],
                        "content": hit["entity"]["content"],
                        "metadata": json.loads(hit["entity"]["metadata"]),
                        # Milvus COSINE already returns a similarity score:
                        # greater values mean more similar. Do not invert it.
                        "score": float(hit["distance"]),
                        "raw_score": float(hit["distance"]),
                    })
                except Exception as e:
                    logger.warning(f"Failed to parse result: {e}")
                    continue

        logger.debug(f"Found {len(documents)} documents (filter={filter_expr})")
        return documents

    def delete_collection(self):
        """删除 collection（用于测试）"""
        if self.milvus_client.has_collection(self.collection_name):
            self.milvus_client.drop_collection(self.collection_name)
            logger.info(f"Deleted collection: {self.collection_name}")

    def count_documents(self) -> int:
        """统计文档数量"""
        try:
            stats = self.milvus_client.describe_collection(self.collection_name)
            # Note: Milvus Lite may not return accurate count, this is a best-effort
            return stats.get("num_entities", 0)
        except Exception as e:
            logger.warning(f"Failed to count documents: {e}")
            return 0

    def delete_by_document_record_id(self, document_id: int) -> None:
        """Remove vectors created by an ingestion job before retrying it.

        The collection uses auto IDs, so the application-level document record
        is stored in metadata and acts as the idempotency key.
        """
        expression = f'metadata like "%\\"document_record_id\\": {int(document_id)}%"'
        try:
            self.milvus_client.delete(self.collection_name, filter=expression)
            logger.info(f"Removed previous vectors for document_record_id={document_id}")
        except Exception as exc:
            logger.warning(f"No previous vectors removed for document_record_id={document_id}: {exc}")
