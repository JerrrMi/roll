"""Binance API 凭据解析：本地密钥文件优先，环境变量为兼容 fallback。

禁止在异常消息、repr 或日志中输出 Secret 明文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_API_KEY = "BINANCE_API_KEY"
ENV_API_SECRET = "BINANCE_API_SECRET"

_MISSING_MSG = (
    "缺少 Binance API 凭据：请通过 --secrets-file、配置 secrets.file，"
    "或进程环境变量 BINANCE_API_KEY / BINANCE_API_SECRET 提供（禁止打印 Secret）。"
)


class SecretsError(Exception):
    """凭据加载失败；message 中不得包含 Secret 明文。"""


@dataclass(frozen=True, slots=True)
class BinanceCredentials:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:
        return "BinanceCredentials(api_key='***', api_secret='***')"


def parse_dotenv_lines(text: str) -> dict[str, str]:
    """解析 KEY=VALUE 行（支持 # 注释与可选引号），不记录日志。"""
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        k = key.strip()
        if not k:
            continue
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def read_secrets_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SecretsError(f"密钥文件不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"无法读取密钥文件: {path}") from exc
    return parse_dotenv_lines(text)


def secrets_path_from_settings(settings: dict | None, project_root: Path) -> Path | None:
    if not settings:
        return None
    sec = settings.get("secrets")
    if not isinstance(sec, dict):
        return None
    raw = sec.get("file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = Path(raw.strip())
    return p if p.is_absolute() else project_root / p


def resolve_secrets_file_path(
    *,
    secrets_file_cli: Path | None,
    settings: dict | None,
    project_root: Path,
) -> Path | None:
    if secrets_file_cli is not None:
        p = secrets_file_cli
        return p if p.is_absolute() else project_root / p
    return secrets_path_from_settings(settings, project_root)


def _from_mapping(mapping: dict[str, str], *, source: str) -> BinanceCredentials:
    key = mapping.get(ENV_API_KEY, "").strip()
    secret = mapping.get(ENV_API_SECRET, "").strip()
    if not key or not secret:
        raise SecretsError(
            f"密钥来源 {source} 缺少 {ENV_API_KEY} 或 {ENV_API_SECRET}（禁止打印 Secret）。"
        )
    return BinanceCredentials(api_key=key, api_secret=secret)


def _from_environment() -> BinanceCredentials | None:
    key_raw = os.environ.get(ENV_API_KEY)
    secret_raw = os.environ.get(ENV_API_SECRET)
    key = key_raw.strip() if isinstance(key_raw, str) else ""
    secret = secret_raw.strip() if isinstance(secret_raw, str) else ""
    if not key or not secret:
        return None
    return BinanceCredentials(api_key=key, api_secret=secret)


def load_binance_credentials(
    *,
    secrets_file_cli: Path | None = None,
    settings: dict | None = None,
    project_root: Path,
) -> BinanceCredentials:
    """按优先级加载凭据：CLI --secrets-file > secrets.file > 环境变量。"""
    if secrets_file_cli is not None:
        path = secrets_file_cli if secrets_file_cli.is_absolute() else project_root / secrets_file_cli
        return _from_mapping(read_secrets_file(path), source=f"file:{path}")

    cfg_path = secrets_path_from_settings(settings, project_root)
    if cfg_path is not None:
        return _from_mapping(read_secrets_file(cfg_path), source=f"file:{cfg_path}")

    env_creds = _from_environment()
    if env_creds is not None:
        return env_creds

    raise SecretsError(_MISSING_MSG)
