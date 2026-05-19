"""live 单进程互斥锁单元测试。"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

import pytest

from roll.process_lock import LiveProcessLockError, acquire_live_singleton_lock, lock_path_for_state_json


def test_lock_path_suffix() -> None:
    p = Path("data/roll_state_live.json")
    assert lock_path_for_state_json(p).name == "roll_state_live.json.lock"


def test_second_acquire_in_same_process_succeeds_after_release(tmp_path: Path) -> None:
    state = tmp_path / "roll_state_live.json"
    state.write_text("{}", encoding="utf-8")
    with acquire_live_singleton_lock(state):
        pass
    with acquire_live_singleton_lock(state):
        pass


def _child_hold_lock(state_str: str, q: multiprocessing.Queue) -> None:
    state = Path(state_str)
    try:
        with acquire_live_singleton_lock(state):
            q.put("held")
            q.get(timeout=30)
    except LiveProcessLockError as exc:
        q.put(f"blocked:{exc}")


@pytest.mark.skipif(sys.platform == "win32", reason="spawn 子进程在 Windows CI 上不稳定")
def test_second_process_blocked(tmp_path: Path) -> None:
    state = tmp_path / "roll_state_live.json"
    state.write_text("{}", encoding="utf-8")
    q: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_child_hold_lock, args=(str(state), q))
    proc.start()
    assert q.get(timeout=10) == "held"
    with pytest.raises(LiveProcessLockError):
        with acquire_live_singleton_lock(state):
            pass
    q.put("release")
    proc.join(timeout=10)
    assert proc.exitcode == 0
