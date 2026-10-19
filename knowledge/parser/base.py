"""
解析器基类 + 格式分发工厂

所有解析器实现同一接口：parse(path) -> Document。
工厂函数根据扩展名选择对应解析器。
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Type

from loguru import logger

from .models import Document

# 支持的扩展名 -> 解析器类（延迟导入，避免循环依赖）
_REGISTRY: Dict[str, Type["DocumentParser"]] = {}


class DocumentParser(ABC):
    """文档解析器抽象基类"""

    # 该解析器支持的扩展名（子类覆盖）
    extensions: tuple = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 自动注册到工厂
        for ext in cls.extensions:
            _REGISTRY[ext.lower()] = cls

    @abstractmethod
    def parse(self, path: Path) -> Document:
        """
        解析一份文档，返回统一的中间表示 Document。

        Args:
            path: 文档路径

        Returns:
            Document 对象
        """
        raise NotImplementedError

    def _new_document(self, path: Path, fmt: str) -> Document:
        """构造带基础元数据的 Document"""
        return Document(
            source_path=str(path),
            format=fmt,
            metadata={
                "filename": path.name,
                "stem": path.stem,
                "suffix": path.suffix,
            },
        )


def parse_document(path) -> Document:
    """
    工厂函数：按文件扩展名选择解析器并解析。

    Args:
        path: 文件路径（str 或 Path）

    Returns:
        Document 对象

    Raises:
        ValueError: 不支持的格式
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文档不存在: {path}")

    ext = path.suffix.lower().lstrip(".")
    parser_cls = _REGISTRY.get(ext)

    if parser_cls is None:
        raise ValueError(
            f"不支持的文档格式: .{ext}（支持: {sorted(_REGISTRY.keys())}）"
        )

    logger.info(f"解析文档: {path.name}（格式: .{ext}）")
    return parser_cls().parse(path)


def supported_extensions() -> list:
    """返回当前已注册支持的文件扩展名"""
    return sorted(_REGISTRY.keys())
