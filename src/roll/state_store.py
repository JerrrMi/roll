"""本地状态持久化：JSON（MVP）；后续可在此接入 SQLite backend。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _sanitize_path(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


@dataclass
class RuntimeState:
    trade_lock_state: str = "IDLE"
    active_symbol: str | None = None
    halt_automatic_trading: bool = False
    halt_reason: str | None = None
    cooldown_until_unix_ms: int | None = None
    last_signal: dict[str, Any] = field(default_factory=dict)


class StateStore:
    """当 backend=json 时使用 `path` 指向的 JSON；否则仅内存占位。"""

    def __init__(
        self,
        *,
        backend: str = "memory",
        path: str | Path | None = None,
    ) -> None:
        self._backend = backend.strip().lower() if backend else "memory"
        self._path = _sanitize_path(Path(path)) if path else None
        self._state = RuntimeState()

    @property
    def path(self) -> Path | None:
        return self._path

    def load(self) -> RuntimeState:
        if self._backend == "json" and self._path is not None and self._path.is_file():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    merged = _runtime_from_mapping(raw)
                    self._state = merged
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return self._state

    def save(self, state: RuntimeState) -> None:
        self._state = state
        if self._backend == "json" and self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n"
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        elif self._backend != "memory" and self._backend != "sqlite":
            # sqlite 占位：仍保持内存一致性，避免静默丢状态
            pass


def runtime_state_from_snap(snap: Mapping[str, Any]) -> RuntimeState:
    """从 PositionManager.snapshot_dict() 回填。"""
    return _runtime_from_mapping(dict(snap))


def _runtime_from_mapping(m: Mapping[str, Any]) -> RuntimeState:
    tls = str(m.get("trade_lock_state", "IDLE"))
    sym_raw = m.get("active_symbol")
    sym = str(sym_raw).strip().upper() if isinstance(sym_raw, str) else None
    halted = bool(m.get("halt_automatic_trading", False))
    reason_raw = m.get("halt_reason")
    reason = str(reason_raw) if isinstance(reason_raw, str) else None
    cdu = m.get("cooldown_until_unix_ms")
    cd: int | None = int(cdu) if isinstance(cdu, int) else None

    sig = m.get("last_signal")
    last_sig: dict[str, Any] = sig if isinstance(sig, dict) else {}

    return RuntimeState(
        trade_lock_state=tls,
        active_symbol=sym if sym else None,
        halt_automatic_trading=halted,
        halt_reason=reason,
        cooldown_until_unix_ms=cd,
        last_signal=dict(last_sig),
    )
