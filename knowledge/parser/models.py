"""
统一中间表示（IR）

PDF / Word / Excel 三种格式解析后，统一产出这一套数据结构，
供标题树构建、表格语义化、版面清理以及最终入库使用。

设计目标：
- 所有解析器只做「格式 → IR」这一件事
- 下游（标题树 / 表格 / 入库）只依赖 IR，不关心原始格式
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple


# ============ 块类型常量 ============

class BlockType:
    """块类型枚举（字符串常量，避免 import enum 的繁琐）"""
    HEADING = "heading"            # 标题
    PARAGRAPH = "paragraph"        # 正文段落
    TABLE = "table"                # 表格
    LIST_ITEM = "list_item"        # 列表项
    HEADER = "header"              # 页眉
    FOOTER = "footer"              # 页脚
    PAGE_NUMBER = "page_number"    # 页码
    WATERMARK = "watermark"        # 水印
    FIGURE = "figure"              # 图片/图
    OTHER = "other"                # 未知


# ============ 基础块 ============

@dataclass
class ParsedBlock:
    """
    解析后的最小文本块。

    一个块可能是：标题、正文段落、页眉、水印、表格……
    携带坐标与字体信息，供版面分析（过滤水印页眉）和标题识别使用。
    """
    block_type: str = BlockType.PARAGRAPH
    text: str = ""
    page: int = 0

    # 字体特征（标题识别的辅助信号）
    font_size: Optional[float] = None
    is_bold: bool = False
    is_italic: bool = False

    # 坐标（PDF 有精确坐标；Word/Excel 可为 None）
    bbox: Optional[Tuple[float, float, float, float]] = None  # (x0, y0, x1, y1)

    # 扩展元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        text_preview = self.text[:30].replace("\n", " ")
        return f"<Block {self.block_type} p{self.page} '{text_preview}'>"


# ============ 标题树 ============

@dataclass
class HeadingNode:
    """
    标题拓扑树节点，对应「篇-章-节-条-款」中的一级。

    number: 编号文本，如 "3.2.1"、"第X条"、"（一）"
    level:  层级（数字越大越深，1 = 篇，2 = 章，……）
    path:   完整路径，如 "第三章/第二节/第5条"，入库时写入 metadata
    content: 归属该标题下的正文块列表
    """
    number: str = ""
    level: int = 0
    title: str = ""
    parent: Optional["HeadingNode"] = None
    children: List["HeadingNode"] = field(default_factory=list)
    content: List[ParsedBlock] = field(default_factory=list)
    path: str = ""

    def add_child(self, child: "HeadingNode") -> "HeadingNode":
        child.parent = self
        self.children.append(child)
        return child

    def full_path(self) -> str:
        """从根到当前节点的路径（如 "第一章 总则/第1条 目的"）"""
        parts = []
        node: Optional[HeadingNode] = self
        while node is not None:
            label = f"{node.number} {node.title}".strip()
            if label:
                parts.append(label)
            node = node.parent
        return "/".join(reversed(parts))

    def iter_depth_first(self):
        """深度优先遍历所有节点（含自身）"""
        yield self
        for child in self.children:
            yield from child.iter_depth_first()

    def __repr__(self) -> str:
        return f"<Heading L{self.level} '{self.number} {self.title[:20]}'>"


# ============ 表格 ============

@dataclass
class TableCell:
    """
    语义化单元格。

    rowspan / colspan: 合并跨度
    header_path:       该单元格所属的表头路径，如 "性别.男.18岁"
    is_header:         是否为表头单元格
    """
    value: str = ""
    row: int = 0
    col: int = 0
    rowspan: int = 1
    colspan: int = 1
    header_path: str = ""
    is_header: bool = False

    def __repr__(self) -> str:
        return f"<Cell r{self.row}c{self.col} '{self.value[:15]}'>"


@dataclass
class ParsedTable:
    """
    结构化解析后的表格。

    header_rows:   多表头（可能多行表头）
    header_paths:  每一列展开后的表头路径（长度 = 列数）
    rows:          forward-fill 之后的完整矩阵（不含表头行）
    cells:         携带语义的单元格（含 rowspan/colspan/header_path）
    """
    caption: str = ""
    page: int = 0

    header_rows: List[List[str]] = field(default_factory=list)  # 原始表头矩阵
    header_paths: List[str] = field(default_factory=list)       # 每列的表头路径
    rows: List[List[str]] = field(default_factory=list)         # 数据矩阵（已 forward-fill）
    cells: List[TableCell] = field(default_factory=list)        # 语义单元格

    def to_text(self, max_rows: int = 200) -> str:
        """
        转为纯文本（用于向量化入库）。

        表头路径 + 每行值拼成可读文本，保留「行列语义」。
        例如："性别=男 | 年龄组=18岁 | 人数=12"
        """
        lines = []
        if self.caption:
            lines.append(f"[表] {self.caption}")

        header_display = " | ".join(p for p in self.header_paths if p)
        if header_display:
            lines.append(f"字段: {header_display}")

        for row in self.rows[:max_rows]:
            cells = []
            for col_idx, value in enumerate(row):
                path = self.header_paths[col_idx] if col_idx < len(self.header_paths) else f"col{col_idx}"
                cells.append(f"{path}={value}" if path else str(value))
            lines.append("; ".join(cells))

        if len(self.rows) > max_rows:
            lines.append(f"...（共 {len(self.rows)} 行，截断显示）")

        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Table {len(self.rows)}x{len(self.header_paths)} '{self.caption[:20]}'>"


# ============ 文档 ============

@dataclass
class Document:
    """
    一份文档解析后的完整中间表示。
    """
    source_path: str = ""
    format: str = ""  # "pdf" / "word" / "excel"

    blocks: List[ParsedBlock] = field(default_factory=list)          # 原始块（含噪声）
    clean_blocks: List[ParsedBlock] = field(default_factory=list)    # 过滤水印/页眉后的块

    heading_tree: Optional[HeadingNode] = None   # 标题拓扑树根节点
    tables: List[ParsedTable] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)  # 文档级元数据

    def headings(self) -> List[HeadingNode]:
        """扁平化所有标题节点"""
        if self.heading_tree is None:
            return []
        return [n for n in self.heading_tree.iter_depth_first() if n.level > 0]

    def __repr__(self) -> str:
        return f"<Document {self.format} '{self.source_path}' blocks={len(self.blocks)}>"
