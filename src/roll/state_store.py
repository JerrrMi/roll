"""本地状态持久化（骨架：内存占位，无 IO）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeState:
    """后续与 SQLite / JSON 对齐的字段将逐步补充。"""

    trade_lock: str = "IDLE"
    last_signal: dict[str, Any] = field(default_factory=dict)


class StateStore:
    """状态存储占位。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._state = RuntimeState()

    def load(self) -> RuntimeState:
        return self._state

    def save(self, state: RuntimeState) -> None:
        self._state = state
