"""
版面清理：过滤水印、页眉、页脚、页码

输入：ParsedBlock 列表（带 page / bbox / font_size）
输出：过滤后的块列表

识别启发式（规则法，适合规整文档）：
1. 页眉/页脚：跨页重复出现、且位于页面顶部或底部的短文本
2. 页码：纯数字或含「页」字、位于页面底部
3. 水印：大字号、短文本（如「机密」「内部资料」）
"""
import re
from collections import defaultdict
from typing import List, Optional

from .models import ParsedBlock, BlockType


def _normalize(text: str) -> str:
    """归一化文本（去空白、去标点），用于跨页重复匹配"""
    return re.sub(r"[\s\u3000]+", "", text)


def _is_page_number(text: str) -> bool:
    """判断是否为页码"""
    t = text.strip()
    if re.fullmatch(r"\d+", t):
        return True
    if re.fullmatch(r"[-—–]?\s*\d+\s*[-—–]?", t):
        return True
    if re.fullmatch(r"第\s*\d+\s*页", t):
        return True
    return False


def clean_layout(
    blocks: List[ParsedBlock],
    page_height: Optional[float] = None,
) -> List[ParsedBlock]:
    """
    一键清理：依次过滤页眉页脚、页码、水印。
    """
    # 估计页面高度（若未提供）
    if page_height is None:
        ys = [b.bbox[1] for b in blocks if b.bbox]
        if ys:
            page_height = max(ys) + 1

    blocks = _remove_repeated_headers_footers(blocks, page_height)
    blocks = _remove_page_numbers(blocks, page_height)
    blocks = _remove_watermarks(blocks)

    return blocks


def _remove_repeated_headers_footers(
    blocks: List[ParsedBlock],
    page_height: Optional[float],
    min_repeat: int = 2,
) -> List[ParsedBlock]:
    """
    移除页眉/页脚：跨页重复、且位于页面顶部或底部区域的短文本。

    顶部区域：y0 < 10% 页面高度
    底部区域：y0 > 85% 页面高度
    """
    # 统计每个归一化文本出现的页码
    text_pages = defaultdict(set)
    for b in blocks:
        if b.block_type in (BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER):
            continue
        if b.text.strip():
            text_pages[_normalize(b.text)].add(b.page)

    kept = []
    for b in blocks:
        norm = _normalize(b.text)
        repeated = len(text_pages.get(norm, set())) >= min_repeat

        if repeated and len(b.text.strip()) < 40:
            # 跨页重复的短文本：结合位置判断是否为页眉页脚
            if b.bbox and page_height:
                y0 = b.bbox[1]
                y1 = b.bbox[3]
                in_top = y0 < page_height * 0.10
                in_bottom = y1 > page_height * 0.85
                if in_top or in_bottom:
                    b.block_type = BlockType.HEADER if in_top else BlockType.FOOTER
                    continue  # 丢弃
        kept.append(b)

    return kept


def _remove_page_numbers(
    blocks: List[ParsedBlock],
    page_height: Optional[float],
) -> List[ParsedBlock]:
    """移除页码：纯数字/「第N页」且位于底部区域"""
    kept = []
    for b in blocks:
        if _is_page_number(b.text):
            if b.bbox and page_height and b.bbox[3] > page_height * 0.80:
                b.block_type = BlockType.PAGE_NUMBER
                continue  # 丢弃
        kept.append(b)
    return kept


def _remove_watermarks(
    blocks: List[ParsedBlock],
    large_font_threshold: float = 26.0,
) -> List[ParsedBlock]:
    """移除水印：大字号 + 短文本（「机密」「内部资料」等）"""
    kept = []
    for b in blocks:
        if (
            b.font_size is not None
            and b.font_size >= large_font_threshold
            and len(b.text.strip()) <= 12
        ):
            b.block_type = BlockType.WATERMARK
            continue  # 丢弃
        kept.append(b)
    return kept
