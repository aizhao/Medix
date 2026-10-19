"""统一的知识库切分策略。

策略顺序：先保留段落/句子/表格行边界，再用 tokenizer 控制最大长度。
当 tokenizer 不可用时，使用保守的字符估算，避免导入链路因网络问题中断。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, List, Optional


@dataclass(frozen=True)
class ChunkPolicy:
    max_tokens: int = 384
    overlap_tokens: int = 48
    # 只有当前主题已经积累到一定规模，且连贯度明显下降时才切换主题，
    # 避免中文短句之间因为没有重复词而被过度切碎。
    min_tokens: int = 96
    topic_boundary_threshold: float = 0.08
    min_topic_units: int = 2


DEFAULT_POLICY = ChunkPolicy(
    max_tokens=int(os.getenv("MEDIX_CHUNK_MAX_TOKENS", "384")),
    overlap_tokens=int(os.getenv("MEDIX_CHUNK_OVERLAP_TOKENS", "48")),
    min_tokens=int(os.getenv("MEDIX_MIN_TOPIC_TOKENS", "96")),
    topic_boundary_threshold=float(os.getenv("MEDIX_TOPIC_BOUNDARY_THRESHOLD", "0.08")),
)


@lru_cache(maxsize=2)
def _load_tokenizer(model_name: str):
    """优先使用本地 tokenizer；离线或未缓存时返回 None。"""
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    except Exception:
        return None


class TokenCounter:
    def __init__(self, tokenizer: Any = None, model_name: Optional[str] = None):
        self.tokenizer = tokenizer or _load_tokenizer(
            model_name or os.getenv("MEDIX_TOKENIZER_MODEL", "BAAI/bge-small-zh-v1.5")
        )

    def encode(self, text: str) -> List[int]:
        if not text:
            return []
        if self.tokenizer is not None:
            # We intentionally inspect long input before splitting it. Disable
            # Transformers' misleading "running through the model" warning;
            # no model inference happens in this counting step.
            return list(self.tokenizer.encode(text, add_special_tokens=False, verbose=False))
        # 保守估算：中文/标点大致按字符计，连续 ASCII 单词按一个 token 计。
        return re.findall(r"[\u4e00-\u9fff]|\w+|[^\w\s]", text)

    def count(self, text: str) -> int:
        return len(self.encode(text))


def _hard_split_text(text: str, counter: TokenCounter, policy: ChunkPolicy) -> List[str]:
    """Split by token ids and verify decoded pieces against the real tokenizer.

    ``decode(encode(text))`` is not token-count preserving for malformed PDF
    text such as ``(cid:83)`` sequences.  A nominal 384-id window can become
    386 tokens after decoding, so every window is shrunk until re-encoding is
    demonstrably safe for the embedding model.
    """
    ids = counter.encode(text)
    if len(ids) <= policy.max_tokens:
        return [text.strip()] if text.strip() else []

    pieces: List[str] = []
    start = 0
    while start < len(ids):
        end = min(len(ids), start + policy.max_tokens)
        while end > start:
            piece_ids = ids[start:end]
            if counter.tokenizer is not None:
                piece = counter.tokenizer.decode(piece_ids, skip_special_tokens=True).strip()
            else:
                piece = "".join(piece_ids).strip()
            if piece and counter.count(piece) <= policy.max_tokens:
                break
            end -= 1
        if end <= start:
            raise ValueError("tokenizer 无法生成模型长度范围内的切片")
        pieces.append(piece)
        if end >= len(ids):
            break
        overlap = min(policy.overlap_tokens, max(0, end - start - 1))
        start = end - overlap
    return pieces


def _enforce_token_limit(chunks: List[str], counter: TokenCounter, policy: ChunkPolicy) -> List[str]:
    safe: List[str] = []
    for chunk in chunks:
        if counter.count(chunk) <= policy.max_tokens:
            safe.append(chunk)
        else:
            safe.extend(_hard_split_text(chunk, counter, policy))
    return safe


def _units(text: str, table: bool = False) -> List[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if table:
        return lines
    units: List[str] = []
    for line in lines:
        # 保留完整句子；没有标点的长行仍作为一个可继续拆分的单元。
        parts = re.split(r"(?<=[。！？!?；;])\s*", line)
        units.extend(part.strip() for part in parts if part.strip())
    return units


def _lexical_tokens(text: str) -> List[str]:
    """轻量中文词元，用于离线主题连贯度判断，不依赖额外分词模型。"""
    return [token.lower() for token in re.findall(r"[\u4e00-\u9fff]{1,4}|[a-zA-Z0-9_]+", text)]


def _topic_cohesion(left: str, right: str) -> float:
    """用词汇重叠近似相邻句子的主题连贯度。"""
    a = set(_lexical_tokens(left))
    b = set(_lexical_tokens(right))
    if not a or not b:
        return 1.0
    return len(a & b) / max(1, min(len(a), len(b)))


def split_text(
    text: str,
    policy: ChunkPolicy = DEFAULT_POLICY,
    tokenizer: Any = None,
    table: bool = False,
) -> List[str]:
    """按逻辑主题边界聚合，最后以 token 上限兜底。

    标题解析器提供第一层主题边界；这里再对标题下的句子做词汇连贯度
    检测。相邻句子主题明显跳变时提前结束当前块，而不是等到固定长度。
    """
    text = text.strip()
    if not text:
        return []
    counter = TokenCounter(tokenizer=tokenizer)
    units = _units(text, table=table)
    if not units:
        return []

    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            chunks.append("\n".join(current).strip())
        current = []
        current_tokens = 0

    for unit in units:
        unit_tokens = counter.count(unit)
        if unit_tokens > policy.max_tokens:
            # 单句/单行过长时才使用 token 窗口硬切。
            flush()
            chunks.extend(_hard_split_text(unit, counter, policy))
            continue

        separator_tokens = 1 if current else 0
        topic_shift = (
            len(current) >= policy.min_topic_units
            and current_tokens >= policy.min_tokens
            and _topic_cohesion(current[-1], unit) < policy.topic_boundary_threshold
        )
        if current and topic_shift:
            previous = current[:]
            flush()
            # 主题切换时只保留少量边界上下文，避免把两个主题重新混在一起。
            overlap: List[str] = []
            overlap_count = 0
            for item in reversed(previous):
                item_count = counter.count(item)
                if overlap_count + item_count > policy.overlap_tokens:
                    break
                overlap.insert(0, item)
                overlap_count += item_count
            # The next semantic unit may itself be close to max_tokens. In
            # that case carrying overlap would recreate an oversized chunk.
            if overlap_count + (1 if overlap else 0) + unit_tokens > policy.max_tokens:
                overlap = []
                overlap_count = 0
            current = overlap
            current_tokens = overlap_count

        if current and current_tokens + separator_tokens + unit_tokens > policy.max_tokens:
            previous = current[:]
            flush()
            # 仅从语义单元边界保留 overlap，避免字符中间重叠。
            overlap: List[str] = []
            overlap_count = 0
            for item in reversed(previous):
                item_count = counter.count(item)
                if overlap_count + item_count > policy.overlap_tokens:
                    break
                overlap.insert(0, item)
                overlap_count += item_count
            if overlap_count + (1 if overlap else 0) + unit_tokens > policy.max_tokens:
                overlap = []
                overlap_count = 0
            current = overlap
            current_tokens = overlap_count

        current.append(unit)
        current_tokens += separator_tokens + unit_tokens

    flush()
    return _enforce_token_limit(chunks, counter, policy)


def split_table_text(text: str, policy: ChunkPolicy = DEFAULT_POLICY, tokenizer: Any = None) -> List[str]:
    """表格按表头 + 数据行分组，确保每个子块都带字段语义。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 2:
        return split_text(text, policy, tokenizer, table=True)
    header = lines[:2]
    rows = lines[2:]
    counter = TokenCounter(tokenizer=tokenizer)
    header_text = "\n".join(header)
    header_tokens = counter.count(header_text)
    # A malformed/flattened table can contain a single row longer than the
    # model context. The general splitter hard-caps such rows safely.
    if (
        header_tokens >= policy.max_tokens
        or any(counter.count(row) >= policy.max_tokens for row in rows)
        or any(header_tokens + 1 + counter.count(row) > policy.max_tokens for row in rows)
    ):
        return split_text(text, policy, tokenizer=tokenizer, table=True)
    chunks: List[str] = []
    current = list(header)
    current_tokens = header_tokens
    for row in rows:
        row_tokens = counter.count(row)
        if len(current) > 2 and current_tokens + 1 + row_tokens > policy.max_tokens:
            chunks.append("\n".join(current))
            current = list(header)
            current_tokens = header_tokens
        current.append(row)
        current_tokens += 1 + row_tokens
    if len(current) > 2 or not chunks:
        chunks.append("\n".join(current))
    return _enforce_token_limit(chunks, counter, policy)
