"""
Excel 解析器

基于 openpyxl。关键点：
- openpyxl 读取合并单元格时，左上角有值、其余被覆盖处为 None，
  天然就是 table_parser 需要的「原始矩阵」格式
- 识别标题行（caption）与多表头
- 每个 sheet 解析为一张（或多张）表
"""
from pathlib import Path
from typing import Optional

from loguru import logger

from .base import DocumentParser
from .models import Document, ParsedBlock, BlockType
from .table_parser import parse_table


class ExcelParser(DocumentParser):
    extensions = ("xlsx", "xlsm")

    def parse(self, path: Path) -> Document:
        import openpyxl

        doc = self._new_document(path, "excel")

        # data_only=True 读取计算后的值（需文件曾被 Excel 保存过）
        wb = openpyxl.load_workbook(path, data_only=True)

        for ws in wb.worksheets:
            logger.info(f"  解析 sheet: {ws.title}（{ws.max_row} 行 x {ws.max_column} 列）")
            self._parse_sheet(ws, doc)

        wb.close()
        return doc

    def _parse_sheet(self, ws, doc: Document):
        """解析单个 sheet"""
        caption, header_row_count, matrix = self._sheet_to_matrix(ws)

        if not matrix:
            return

        table = parse_table(
            raw_matrix=matrix,
            header_row_count=header_row_count,
            caption=caption,
            page=doc.metadata.get("sheet_index", 0),
        )
        doc.tables.append(table)

        # 同时把表格转成文本块，参与后续标题树/入库
        doc.blocks.append(ParsedBlock(
            block_type=BlockType.TABLE,
            text=table.to_text(),
            metadata={"sheet": ws.title, "caption": caption},
        ))

    def _sheet_to_matrix(self, ws) -> tuple:
        """
        将 sheet 转为原始矩阵，并推断 caption 与表头行数。

        Returns:
            (caption, header_row_count, matrix)
        """
        # 1. 读取所有值（合并单元格覆盖处为 None）
        raw = [
            [self._clean(v) for v in row]
            for row in ws.iter_rows(values_only=True)
        ]

        # 去掉全空行
        raw = [row for row in raw if any(v not in (None, "") for v in row)]
        if not raw:
            return "", 1, []

        caption = ""
        start = 0

        # 2. 检测标题行：第一行若是跨全表的合并单元格，作为 caption
        merged = ws.merged_cells.ranges
        ncols = max(len(row) for row in raw)
        first_row_span = self._row_span(merged, 1)
        if first_row_span >= ncols and len(raw) > 1:
            caption = str(raw[0][0] or "").strip()
            start = 1

        matrix = raw[start:]

        # 3. 推断表头行数：数据起始后，若第二行仍是合并/全表头特征，扩展表头
        header_row_count = 1
        # 简单启发式：表头行之后若紧跟的若干行没有数据值（或首列仍为空），
        # 视为多级表头。这里默认 1，可后续扩展。
        # TODO: 通过合并单元格分布更准确地推断多表头行数

        return caption, header_row_count, matrix

    def _row_span(self, merged_ranges, row_idx: int) -> int:
        """计算某一行（1-based）被合并单元格覆盖的列跨度"""
        total = 0
        for rng in merged_ranges:
            if rng.min_row <= row_idx <= rng.max_row:
                total = max(total, rng.max_col)
        return total

    @staticmethod
    def _clean(v) -> Optional[str]:
        """None 保持 None；数字转字符串；去首尾空白"""
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None
