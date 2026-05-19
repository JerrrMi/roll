"""live signed 自动交易单进程互斥锁（基于 state JSON 旁的 .lock 文件）。"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LiveProcessLockError(RuntimeError):
    """已有其它 live 进程持有同一状态文件的互斥锁。"""


def lock_path_for_state_json(state_json: Path) -> Path:
    """与 `roll_state_live.json` 配对的锁文件路径（`roll_state_live.json.lock`）。"""
    p = state_json.expanduser().resolve(strict=False)
    return p.with_name(p.name + ".lock")


def _try_exclusive_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise LiveProcessLockError(
                "已有其它 live 进程在运行（无法获取进程锁）。"
                " 请停止重复的 `run-loop --no-dry-run`、systemd roll-live 或前台 live 进程后再启动。"
            ) from exc
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LiveProcessLockError(
                "已有其它 live 进程在运行（无法获取 flock）。"
                " 请停止重复的 `run-loop --no-dry-run`、systemd roll-live 或前台 live 进程后再启动。"
            ) from exc


def _release_lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def acquire_live_singleton_lock(state_json_path: Path) -> Iterator[Path]:
    """在 live signed 主循环存活期间持有互斥锁；退出 context 时自动释放。

    Parameters
    ----------
    state_json_path:
        配置中的 ``state.path``（例如 ``data/roll_state_live.json``）。
    """
    lock_path = lock_path_for_state_json(state_json_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        _try_exclusive_lock(fd)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        yield lock_path
    except Exception:
        _release_lock(fd)
        raise
    finally:
        _release_lock(fd)
