"""
PDF 解析器

基于 PyMuPDF (fitz) 提取文本块（含字体、坐标），
用 pdfplumber 抽取表格，
配合 layout_cleaner 过滤水印/页眉页脚，再用 heading_tree 建标题树。

仅支持数字版 PDF（有文本层）。扫描版需 OCR（后置）。
"""
from pathlib import Path
from typing import List

from loguru import logger

from .base import DocumentParser
from .models import Document, ParsedBlock, BlockType
from .layout_cleaner import clean_layout
from .heading_tree import build_heading_tree
from .table_parser import parse_table

# PyMuPDF flags 位含义
_FLAG_ITALIC = 2
_FLAG_BOLD = 16


class PdfParser(DocumentParser):
    extensions = ("pdf",)

    def parse(self, path: Path) -> Document:
        import fitz  # PyMuPDF

        doc = self._new_document(path, "pdf")
        blocks: List[ParsedBlock] = []
        page_height = None

        with fitz.open(str(path)) as pdf:
            for page_num, page in enumerate(pdf, start=1):
                if page_height is None:
                    page_height = page.rect.height
                blocks.extend(self._extract_text_blocks(page, page_num))

        # 版面清理（过滤页眉页脚/页码/水印）
        clean_blocks = clean_layout(blocks, page_height)

        # 标题拓扑树
        heading_tree = build_heading_tree(clean_blocks)

        # 表格（pdfplumber）
        tables = self._extract_tables(path)
        for t in tables:
            doc.blocks.append(ParsedBlock(
                block_type=BlockType.TABLE,
                text=t.to_text(),
                page=t.page,
            ))

        doc.blocks = blocks
        doc.clean_blocks = clean_blocks
        doc.heading_tree = heading_tree
        doc.tables = tables

        logger.info(
            f"  PDF 解析完成：{len(blocks)} 块，"
            f"清理后 {len(clean_blocks)} 块，{len(tables)} 张表"
        )
        return doc

    def _extract_text_blocks(self, page, page_num: int) -> List[ParsedBlock]:
        """从单页提取文本块（按行合并 span）"""
        blocks: List[ParsedBlock] = []
        data = page.get_text("dict")

        for block in data.get("blocks", []):
            if block.get("type") != 0:  # 0 = 文本块
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue

                first = spans[0]
                flags = first.get("flags", 0)
                bbox = line.get("bbox")

                blocks.append(ParsedBlock(
                    block_type=BlockType.PARAGRAPH,
                    text=text,
                    page=page_num,
                    font_size=first.get("size"),
                    is_bold=bool(flags & _FLAG_BOLD),
                    is_italic=bool(flags & _FLAG_ITALIC),
                    bbox=tuple(bbox) if bbox else None,
                ))

        return blocks

    def _extract_tables(self, path: Path) -> list:
        """用 pdfplumber 抽取表格（未安装则跳过）"""
        tables = []
        try:
            import pdfplumber
        except ImportError:
            logger.warning("pdfplumber 未安装，跳过 PDF 表格抽取")
            return tables

        with pdfplumber.open(str(path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                for tbl in page.extract_tables():
                    if not tbl:
                        continue
                    matrix = [
                        [cell.strip() if cell else None for cell in row]
                        for row in tbl
                    ]
                    tables.append(parse_table(
                        raw_matrix=matrix,
                        header_row_count=1,
                        caption="",
                        page=page_num,
                    ))

        return tables
