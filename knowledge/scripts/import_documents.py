"""
多格式文档入库脚本

扫描目录下的 PDF / Word / Excel，智能解析后写入 Milvus 知识库。

支持 PDF / DOCX / XLSX / XLSM / TXT / Markdown：
- 按「标题拓扑树 + 表格」逻辑切片，而非硬切字符
- metadata 携带标题路径（如 第三章/第二节/第5条），支持来源追踪
"""
import sys
import hashlib
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger

from knowledge.parser import parse_document, supported_extensions
from knowledge.parser.models import Document
from knowledge.milvus_kb import MedicalKnowledgeBase
from knowledge.chunking import DEFAULT_POLICY, split_table_text, split_text
from backend.storage import SQLiteStore


def infer_doc_type(path: Path, source_dir_name: str) -> str:
    """从文件名 + 目录名推断文档类型"""
    name = f"{path.stem}_{source_dir_name}".lower()
    if any(k in name for k in ["guideline", "指南", "诊疗"]):
        return "clinical_guideline"
    if any(k in name for k in ["standard", "标准", "规范"]):
        return "standard"
    if any(k in name for k in ["report", "报告", "督导"]):
        return "report"
    if any(k in name for k in ["regulation", "制度", "规程", "办法"]):
        return "regulation"
    return "document"


def document_to_chunks(doc: Document, doc_type: str, source: str) -> List[Dict[str, Any]]:
    """
    把解析后的 Document 切成入库块。

    切分策略（逻辑块，而非字符硬切）：
    1. 标题树：每个标题节点 + 其正文 = 一个块（metadata 带完整标题路径）
    2. 表格：每张表 = 一个块（表头路径已文本化）
    3. 兜底：无标题无表格时，整篇 clean_blocks 作为一个块
    """
    chunks: List[Dict[str, Any]] = []
    stem = Path(doc.source_path).stem
    filename = doc.metadata.get("filename", "")
    idx = 0

    def add_logical_chunk(content: str, metadata: Dict[str, Any], chunk_id: str, is_table: bool = False) -> None:
        splitter = split_table_text if is_table else split_text
        parts = splitter(content, DEFAULT_POLICY)
        for part_index, part in enumerate(parts):
            item_metadata = dict(metadata)
            item_metadata["chunk_part"] = part_index
            item_metadata["chunk_parts"] = len(parts)
            item_metadata["chunking_strategy"] = "table_row_group" if is_table else "topic_boundary"
            chunks.append({
                "id": f"{chunk_id}_{part_index}",
                "content": part,
                "metadata": item_metadata,
            })

    # 1. 标题树切片
    if doc.heading_tree:
        for node in doc.heading_tree.iter_depth_first():
            if node.level == 0:
                continue
            path = node.path or node.full_path()
            parts = [f"[标题路径] {path}", node.title]
            parts.extend(b.text for b in node.content if b.text)
            content = "\n".join(p for p in parts if p.strip())
            if not content.strip():
                continue
            add_logical_chunk(content, {
                    "type": doc_type,
                    "source": source,
                    "filename": filename,
                    "path": path,
                    "level": node.level,
                    "number": node.number,
                }, f"{doc_type}_{stem}_{idx}")
            idx += 1

    # 2. 表格切片
    for t in doc.tables:
        # 入库前不截断表格，交给表格切分器按行组拆分。
        add_logical_chunk(t.to_text(max_rows=len(t.rows)), {
                "type": doc_type,
                "source": source,
                "filename": filename,
                "is_table": True,
                "caption": t.caption,
                "page": t.page,
            }, f"{doc_type}_{stem}_{idx}", is_table=True)
        idx += 1

    # 3. 兜底
    if not chunks and doc.clean_blocks:
        content = "\n".join(b.text for b in doc.clean_blocks if b.text)
        if content.strip():
            add_logical_chunk(content, {
                    "type": doc_type,
                    "source": source,
                    "filename": filename,
                }, f"{doc_type}_{stem}_0")

    return chunks


def _index_registered_document(store: SQLiteStore, kb: MedicalKnowledgeBase, document_id: int) -> int:
    """使用与 API 相同的 SQLite chunks → Milvus 索引流程。"""
    chunks = store.list_chunks(document_id)
    if not chunks:
        raise ValueError("没有可索引的解析块")
    document = store.get_document(document_id) or {}
    version = int(document.get("version") or 1)
    documents = []
    for chunk in chunks:
        metadata = dict(chunk.get("metadata") or {})
        metadata.update({
            "document_record_id": document_id,
            "chunk_id": int(chunk["chunk_index"]),
            "version": version,
        })
        documents.append({
            "id": f"document-{document_id}-chunk-{chunk['chunk_index']}",
            "content": chunk["content"],
            "metadata": metadata,
        })
    kb.delete_by_document_record_id(document_id)
    return kb.add_documents(documents, chunk_size=0)


def _import_one_file(path: Path, source_dir: Path, store: SQLiteStore, kb: MedicalKnowledgeBase) -> int:
    """登记、解析、持久化、索引一个文件；失败状态保留在 SQLite。"""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    resolved = str(path.resolve())
    existing = store.find_document_by_path(resolved)
    if existing and existing.get("sha256") == digest and existing.get("status") == "indexed":
        logger.info(f"  ⏭️ {path.name}: 内容未变化，跳过（document_id={existing['id']}）")
        return int(existing.get("indexed_chunks") or 0)

    doc_type = infer_doc_type(path, source_dir.name)
    if existing:
        document_id = int(existing["id"])
        version = int(existing.get("version") or 1) + 1
        kb.delete_by_document_record_id(document_id)
        store.update_document(
            document_id,
            status="processing",
            version=version,
            progress=0,
            current_stage="parsing",
            total_chunks=0,
            indexed_chunks=0,
            error_code="",
            error_message="",
            started_at=store._now(),
            finished_at=None,
            sha256=digest,
            size_bytes=path.stat().st_size,
        )
    else:
        record = store.add_document(
            path.name,
            resolved,
            "processing",
            category=doc_type,
            source_type="internal",
            authority_level="unknown",
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            size_bytes=path.stat().st_size,
            sha256=digest,
        )
        document_id = int(record["id"])

    job_id = store.create_job(document_id, "batch_import")
    store.update_job(job_id, status="running", progress=10, stage="parsing", started_at=store._now())
    try:
        parsed = parse_document(path)
        store.update_job(job_id, progress=60, stage="chunking")
        chunks = document_to_chunks(parsed, doc_type, source_dir.name)
        if not chunks:
            raise ValueError("文档解析后没有可用内容")
        for chunk in chunks:
            chunk.setdefault("metadata", {}).update({
                "document_record_id": document_id,
                "category": doc_type,
                "source_type": "internal",
                "authority_level": "unknown",
            })
        store.replace_chunks(document_id, chunks)
        store.update_job(job_id, progress=75, stage="milvus_insert")
        store.update_document_status(document_id, "parsed", progress=75, current_stage="parsed", total_chunks=len(chunks), indexed_chunks=0)
        count = _index_registered_document(store, kb, document_id)
        store.update_job(job_id, status="completed", progress=100, stage="indexed", finished_at=store._now())
        store.update_document_status(document_id, "indexed", progress=100, current_stage="indexed", indexed_chunks=count, error_code="", error_message="", finished_at=store._now())
        logger.info(f"  ✅ {path.name}: {count} 块（document_id={document_id}, version={store.get_document(document_id)['version']}）")
        return count
    except Exception as exc:
        code = exc.__class__.__name__.lower()
        message = str(exc)[:1000] or "unknown ingestion error"
        store.update_job(job_id, status="failed", progress=100, stage="import_failed", error_code=code, error_message=message, finished_at=store._now())
        store.update_document_status(document_id, "failed", progress=100, current_stage="import_failed", error_code=code, error_message=message, finished_at=store._now())
        logger.error(f"  ❌ {path.name}: 导入失败 - {message}")
        return 0


def import_directory(doc_dir: Path, kb: Optional[MedicalKnowledgeBase] = None, store: Optional[SQLiteStore] = None) -> int:
    """扫描目录并通过 SQLite 文档登记后入库，返回入库块数量。"""
    total_chunks = 0
    store = store or SQLiteStore()
    kb = kb or MedicalKnowledgeBase()
    supported = {f".{e}" for e in supported_extensions()}

    files = [f for f in sorted(doc_dir.rglob("*")) if f.suffix.lower() in supported]
    if not files:
        logger.warning(f"目录 {doc_dir} 下未找到支持的文件（{supported}）")
        return 0

    logger.info(f"发现 {len(files)} 个文档（支持格式: {', '.join(sorted(supported))}）")

    for f in files:
        try:
            total_chunks += _import_one_file(f, doc_dir, store, kb)
        except Exception as e:
            logger.error(f"  ❌ {f.name}: 解析失败 - {e}")

    return total_chunks


def main():
    import argparse

    parser = argparse.ArgumentParser(description="多格式文档入库")
    parser.add_argument("doc_dir", nargs="?", default="knowledge/data/documents",
                        help="文档目录（默认 knowledge/data/documents）")
    args = parser.parse_args()

    doc_dir = Path(project_root) / args.doc_dir if not Path(args.doc_dir).is_absolute() else Path(args.doc_dir)

    if not doc_dir.exists():
        logger.error(f"目录不存在: {doc_dir}")
        return

    logger.info("=" * 70)
    logger.info("开始导入多格式文档到 Milvus 知识库")
    logger.info("=" * 70)

    store = SQLiteStore()
    kb = MedicalKnowledgeBase()
    total = import_directory(doc_dir, kb, store)

    logger.info("\n" + "=" * 70)
    logger.info(f"🎉 完成！共导入 {total} 个内容块")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
