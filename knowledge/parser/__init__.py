"""
文档智能解析子系统

多格式（PDF/Word/Excel）→ 统一中间表示（IR）→ 标题树 / 表格语义化 / 版面清理
"""
# 导入各解析器，触发 base.py 中的自动注册（__init_subclass__）
from . import excel_parser, word_parser, pdf_parser, text_parser  # noqa: F401

from .base import DocumentParser, parse_document, supported_extensions
from .models import (
    Document,
    ParsedBlock,
    HeadingNode,
    TableCell,
    ParsedTable,
    BlockType,
)
from .heading_tree import (
    build_heading_tree,
    match_heading,
    HeadingPattern,
    HeadingMatch,
    DEFAULT_PATTERNS,
)
from .table_parser import (
    parse_table,
    forward_fill,
    build_header_paths,
    merge_continued_tables,
)
from .layout_cleaner import clean_layout

__all__ = [
    # 入口
    "parse_document",
    "supported_extensions",
    "DocumentParser",
    # 数据结构
    "Document",
    "ParsedBlock",
    "HeadingNode",
    "TableCell",
    "ParsedTable",
    "BlockType",
    # 标题树
    "build_heading_tree",
    "match_heading",
    "HeadingPattern",
    "HeadingMatch",
    "DEFAULT_PATTERNS",
    # 表格
    "parse_table",
    "forward_fill",
    "build_header_paths",
    "merge_continued_tables",
    # 版面
    "clean_layout",
]
