"""
多格式文档入库脚本

扫描目录下的 PDF / Word / Excel，智能解析后写入 Milvus 知识库。

对比旧的 import_hardcoded_data.py（只支持 txt、固定字符切分）：
- 支持 PDF / DOCX / XLSX / XLSM
- 按「标题拓扑树 + 表格」逻辑切片，而非硬切字符
- metadata 携带标题路径（如 第三章/第二节/第5条），支持来源追踪
"""
import sys
from pathlib import Path
from typing import List, Dict, Any

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger

from knowledge.parser import parse_document, supported_extensions
from knowledge.parser.models import Document
from knowledge.milvus_kb import MedicalKnowledgeBase


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

    # 1. 标题树切片
    if doc.heading_tree:
        for node in doc.heading_tree.iter_depth_first():
            if node.level == 0:
                continue
            parts = [node.title]
            parts.extend(b.text for b in node.content if b.text)
            content = "\n".join(p for p in parts if p.strip())
            if not content.strip():
                continue
            chunks.append({
                "id": f"{doc_type}_{stem}_{idx}",
                "content": content,
                "metadata": {
                    "type": doc_type,
                    "source": source,
                    "filename": filename,
                    "path": node.path or node.full_path(),
                    "level": node.level,
                    "number": node.number,
                },
            })
            idx += 1

    # 2. 表格切片
    for t in doc.tables:
        chunks.append({
            "id": f"{doc_type}_{stem}_{idx}",
            "content": t.to_text(),
            "metadata": {
                "type": doc_type,
                "source": source,
                "filename": filename,
                "is_table": True,
                "caption": t.caption,
            },
        })
        idx += 1

    # 3. 兜底
    if not chunks and doc.clean_blocks:
        content = "\n".join(b.text for b in doc.clean_blocks if b.text)
        if content.strip():
            chunks.append({
                "id": f"{doc_type}_{stem}_0",
                "content": content,
                "metadata": {
                    "type": doc_type,
                    "source": source,
                    "filename": filename,
                },
            })

    return chunks


def import_directory(doc_dir: Path, kb: MedicalKnowledgeBase) -> int:
    """扫描目录并入库，返回入库块数量"""
    total_chunks = 0
    supported = {f".{e}" for e in supported_extensions()}

    files = [f for f in sorted(doc_dir.rglob("*")) if f.suffix.lower() in supported]
    if not files:
        logger.warning(f"目录 {doc_dir} 下未找到支持的文件（{supported}）")
        return 0

    logger.info(f"发现 {len(files)} 个文档（支持格式: {', '.join(sorted(supported))}）")

    for f in files:
        try:
            doc = parse_document(f)
            doc_type = infer_doc_type(f, doc_dir.name)
            chunks = document_to_chunks(doc, doc_type, doc_dir.name)

            if not chunks:
                logger.warning(f"  ⚠️ {f.name}: 解析后无有效内容")
                continue

            # Preserve the semantic heading/table boundary, but split unusually
            # long sections so a single embedding does not mix many topics.
            kb.add_documents(chunks, chunk_size=1600)
            total_chunks += len(chunks)
            logger.info(f"  ✅ {f.name}: {len(chunks)} 块（类型={doc_type}）")
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

    kb = MedicalKnowledgeBase()
    total = import_directory(doc_dir, kb)

    logger.info("\n" + "=" * 70)
    logger.info(f"🎉 完成！共导入 {total} 个内容块")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
