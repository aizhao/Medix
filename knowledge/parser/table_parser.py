"""
表格结构化解析

处理三大难题：
1. 合并单元格 → forward-fill 展开成完整矩阵
2. 多表头     → 展开成「表头路径」，如 "人数.男.18岁"
3. 跨页表格   → 识别重复表头并去重合并

输入：原始矩阵（合并单元格的值在左上角，被覆盖处为 None）
输出：ParsedTable（完整矩阵 + 表头路径 + 语义单元格）
"""
from typing import List, Optional

from loguru import logger

from .models import ParsedTable, TableCell


def forward_fill(matrix: List[List[Optional[str]]]) -> List[List[str]]:
    """
    合并单元格展开：把 None 用上方/左方的值填充。

    规则：从上到下、从左到右单轮遍历。
    - 优先取上方值（处理 rowspan 纵向合并）
    - 否则取左方值（处理 colspan 横向合并）

    矩形合并单元格（如 2x2）经过一轮即可填满，因为总是引用已填充的邻居。
    """
    if not matrix:
        return []

    ncols = max(len(row) for row in matrix)
    filled: List[List[str]] = []

    for r, row in enumerate(matrix):
        new_row: List[str] = []
        for c in range(ncols):
            val = row[c] if c < len(row) else None

            if val is None or str(val).strip() == "":
                # 上方值
                above = filled[r - 1][c] if r > 0 and c < len(filled[r - 1]) else None
                # 左方值
                left = new_row[c - 1] if c > 0 else None

                if above is not None and str(above).strip() != "":
                    val = above
                elif left is not None and str(left).strip() != "":
                    val = left
                else:
                    val = ""

            new_row.append("" if val is None else str(val))

        filled.append(new_row)

    return filled


def build_header_paths(header_rows: List[List[Optional[str]]]) -> List[str]:
    """
    多表头展开成每列的表头路径。

    例：
        [["", "人数", "人数"],
         ["性别", "男", "女"],
         ["", "18岁", "19岁"]]
    →
        ["性别", "人数.男.18岁", "人数.女.19岁"]
    """
    if not header_rows:
        return []

    # 先 forward-fill，把表头自身的合并单元格展开
    filled = forward_fill(header_rows)
    ncols = max(len(row) for row in filled)
    paths: List[str] = []

    for c in range(ncols):
        parts: List[str] = []
        for r in range(len(filled)):
            v = filled[r][c] if c < len(filled[r]) else ""
            v = str(v).strip()
            if v and v not in parts:  # 去重（跨行的同一表头只保留一次）
                parts.append(v)
        paths.append(".".join(parts))

    return paths


def parse_table(
    raw_matrix: List[List[Optional[str]]],
    header_row_count: int = 1,
    caption: str = "",
    page: int = 0,
) -> ParsedTable:
    """
    解析一张表格。

    Args:
        raw_matrix: 原始矩阵（合并单元格左上角有值，其余 None/空）
        header_row_count: 表头行数（前 N 行为表头）
        caption: 表格标题
        page: 所在页码

    Returns:
        ParsedTable
    """
    if not raw_matrix:
        return ParsedTable(caption=caption, page=page)

    # 拆分表头与数据
    header_rows = raw_matrix[:header_row_count]
    data_rows = raw_matrix[header_row_count:]

    # 表头路径
    header_paths = build_header_paths(header_rows)

    # 数据区 forward-fill
    filled_rows = forward_fill(data_rows)

    # 生成语义单元格
    cells: List[TableCell] = []
    for r, row in enumerate(filled_rows):
        for c, value in enumerate(row):
            path = header_paths[c] if c < len(header_paths) else ""
            cells.append(TableCell(
                value=value,
                row=r,
                col=c,
                header_path=path,
            ))

    table = ParsedTable(
        caption=caption,
        page=page,
        header_rows=[["" if v is None else str(v) for v in row] for row in header_rows],
        header_paths=header_paths,
        rows=filled_rows,
        cells=cells,
    )

    return table


def headers_equal(a: List[str], b: List[str]) -> bool:
    """判断两个表头路径列表是否一致（用于跨页表格识别）"""
    return [x for x in a if x] == [x for x in b if x]


def merge_continued_tables(tables: List[ParsedTable]) -> List[ParsedTable]:
    """
    合并跨页表格：如果相邻两张表表头一致，判定为同一张表的延续，
    去掉第二张的重复表头，拼接数据行。

    Args:
        tables: 按出现顺序排列的表格列表

    Returns:
        合并后的表格列表
    """
    if not tables:
        return []

    merged: List[ParsedTable] = [tables[0]]

    for nxt in tables[1:]:
        prev = merged[-1]
        if headers_equal(prev.header_paths, nxt.header_paths):
            # 同一张表的延续：行号偏移后拼接数据行
            offset = len(prev.rows)
            for cell in nxt.cells:
                cell.row += offset
            prev.rows.extend(nxt.rows)
            prev.cells.extend(nxt.cells)
            logger.debug(f"跨页表格合并（表头一致），累计 {len(prev.rows)} 行")
        else:
            merged.append(nxt)

    return merged
