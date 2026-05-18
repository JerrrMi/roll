"""日志初始化：结构化字段后续可接入 JSON formatter。"""

from __future__ import annotations

import logging
import sys
from typing import TextIO


def get_logger(name: str, stream: TextIO | None = None) -> logging.Logger:
    """返回带简单格式的命名 logger。"""
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    return log
