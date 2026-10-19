"""
标题拓扑树提取

从解析出的文本块中识别「篇-章-节-条-款」层级标题，
构建树形结构，并把正文段落归属到对应标题下。

核心算法：栈式建树（时间复杂度 O(n)）
- 每个标题按 level（层级深度）入栈/出栈
- level 相同或更浅的节点出栈，直到找到父节点
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from .models import HeadingNode, ParsedBlock, BlockType


# ============ 标题编号模式 ============

@dataclass
class HeadingPattern:
    """一种标题编号模式"""
    name: str           # 如 "章"、"条"、"款"
    level: int          # 层级深度（越大越深，1=最高）
    regex: str          # 匹配编号的正则（从行首匹配）
    number_group: int = 0  # 提取编号文本的 group 序号


# 默认层级模型（中国标准/法规文档常见结构），可按文档类型覆盖
DEFAULT_PATTERNS: List[HeadingPattern] = [
    HeadingPattern("篇", 1, r"^第[0-9一二三四五六七八九十百千]+[篇]"),
    HeadingPattern("章", 2, r"^第[0-9一二三四五六七八九十百千]+[章]"),
    HeadingPattern("节", 3, r"^第[0-9一二三四五六七八九十百千]+[节]"),
    HeadingPattern("条", 4, r"^第[0-9一二三四五六七八九十百千]+[条]"),
    # 数字多级编号：1.2.3 / 1.2 / 1
    HeadingPattern("款", 7, r"^\d+(?:\.\d+){2,}"),  # 1.2.3 及以上
    HeadingPattern("款", 6, r"^\d+\.\d+"),            # 1.2
    HeadingPattern("项", 5, r"^\d+[、．.]"),          # 1、
    HeadingPattern("项", 6, r"^[一二三四五六七八九十]+[、．]"),   # 一、
    HeadingPattern("项", 7, r"^[（(][一二三四五六七八九十]+[）)]"),  # （一）
    HeadingPattern("项", 8, r"^[①②③④⑤⑥⑦⑧⑨⑩]"),   # ①
]


@dataclass
class HeadingMatch:
    """标题识别结果"""
    number: str
    level: int
    title: str   # 去掉编号后的标题文本
    name: str    # 编号模式名（篇/章/节/条/款/项）


# 标题最大长度（超过则判定为正文，避免误判）
MAX_HEADING_LENGTH = 60


def match_heading(
    text: str,
    font_size: Optional[float] = None,
    is_bold: bool = False,
    patterns: Optional[List[HeadingPattern]] = None,
) -> Optional[HeadingMatch]:
    """
    判断一段文本是否为标题，返回标题信息。

    判定依据（综合打分）：
    1. 行首匹配某个编号模式（强信号）
    2. 长度不能过长（标题一般短）
    3. 字体特征（加粗/大字号）作为辅助，但不是必需

    Args:
        text: 待判断文本（已 strip 行首空白）
        font_size: 字号（可为 None）
        is_bold: 是否加粗
        patterns: 编号模式（默认 DEFAULT_PATTERNS）

    Returns:
        HeadingMatch 或 None（不是标题）
    """
    text = text.strip()
    if not text:
        return None

    # 标题过长，基本是正文
    if len(text) > MAX_HEADING_LENGTH:
        return None

    patterns = patterns or DEFAULT_PATTERNS

    for pat in patterns:
        m = re.match(pat.regex, text)
        if not m:
            continue

        number = m.group(pat.number_group) if pat.number_group else m.group(0)
        # 去掉编号，得到标题正文
        title = text[m.end():].strip()
        # 去掉标题正文首部的常见分隔符（空格、顿号等）
        title = title.lstrip("、．.：: ")

        # 某些模式（如 ①）编号即全部内容，title 可能为空
        if not title:
            title = number

        return HeadingMatch(
            number=number,
            level=pat.level,
            title=title,
            name=pat.name,
        )

    return None


def build_heading_tree(
    blocks: List[ParsedBlock],
    patterns: Optional[List[HeadingPattern]] = None,
) -> HeadingNode:
    """
    从块列表构建标题拓扑树，并把正文归属到标题。

    流程：
    1. 识别每个块是否为标题
    2. 栈式建树
    3. 正文块归属到当前最深的标题节点

    Returns:
        根节点（level=0，不含标题文本）
    """
    root = HeadingNode(number="", level=0, title="")
    stack: List[HeadingNode] = [root]

    current = root  # 当前正在接收正文的节点

    for block in blocks:
        if block.block_type not in (BlockType.PARAGRAPH, BlockType.HEADING, BlockType.LIST_ITEM):
            continue

        # 优先使用样式级别（Word 的 Heading 1~9），否则靠编号文本识别
        style_level = block.metadata.get("style_level")
        match = match_heading(block.text, block.font_size, block.is_bold, patterns)

        if match is None and style_level is not None:
            # 有样式但无编号文本（如 Word 里纯文本标题）
            match = HeadingMatch(
                number="",
                level=style_level,
                title=block.text.strip(),
                name="style",
            )
        elif match is not None and style_level is not None:
            # 两者都有：Word 样式级别更权威，覆盖编号推断的 level
            match.level = style_level

        if match is None:
            # 非标题：正文归属到当前节点
            if block.block_type == BlockType.PARAGRAPH:
                current.content.append(block)
            continue

        # 是标题：构建节点
        node = HeadingNode(
            number=match.number,
            level=match.level,
            title=match.title,
        )

        # 栈式出栈：找到父节点（level 严格小于当前节点）
        while stack and stack[-1].level >= node.level:
            stack.pop()

        parent = stack[-1]
        parent.add_child(node)
        node.path = node.full_path()

        stack.append(node)
        current = node

    logger.info(f"构建标题树完成：共 {len(root.children)} 个一级标题")
    return root
