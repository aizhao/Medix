"""Plain text and Markdown parser used by the upload pipeline."""
from pathlib import Path

from .base import DocumentParser
from .models import BlockType, Document, ParsedBlock
from .heading_tree import build_heading_tree


class TextParser(DocumentParser):
    extensions = ("txt", "md")

    def parse(self, path: Path) -> Document:
        document = self._new_document(path, "text")
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks = []
        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue
            block_type = BlockType.HEADING if value.startswith("#") else BlockType.PARAGRAPH
            blocks.append(ParsedBlock(block_type=block_type, text=value))
        document.blocks = blocks
        document.clean_blocks = blocks
        document.heading_tree = build_heading_tree(blocks)
        return document
