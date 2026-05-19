"""凭据加载与 Secret 不落日志/异常/测试输出。"""

from __future__ import annotations

from pathlib import Path

import pytest

from roll.secrets import (
    ENV_API_KEY,
    ENV_API_SECRET,
    BinanceCredentials,
    SecretsError,
    load_binance_credentials,
    parse_dotenv_lines,
    read_secrets_file,
)

FAKE_KEY = "test_key_abc123"
FAKE_SECRET = "test_secret_xyz789_never_log"


def _write_env(path: Path, key: str = FAKE_KEY, secret: str = FAKE_SECRET) -> None:
    path.write_text(
        f"# comment\n{ENV_API_KEY}={key}\n{ENV_API_SECRET}={secret}\n",
        encoding="utf-8",
    )


def test_parse_dotenv_ignores_comments_and_export() -> None:
    text = (
        "# header\n"
        "export BINANCE_API_KEY='k1'\n"
        'BINANCE_API_SECRET="s1"\n'
        "\n"
        "# tail\n"
    )
    m = parse_dotenv_lines(text)
    assert m[ENV_API_KEY] == "k1"
    assert m[ENV_API_SECRET] == "s1"


def test_load_priority_cli_over_config_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "from_config.env"
    cli_file = tmp_path / "from_cli.env"
    _write_env(cfg_file, key="from_config_key", secret="from_config_secret")
    _write_env(cli_file, key="from_cli_key", secret="from_cli_secret")

    monkeypatch.setenv(ENV_API_KEY, "from_env_key")
    monkeypatch.setenv(ENV_API_SECRET, "from_env_secret")

    creds = load_binance_credentials(
        secrets_file_cli=cli_file,
        settings={"secrets": {"file": str(cfg_file)}},
        project_root=tmp_path,
    )
    assert creds.api_key == "from_cli_key"
    assert creds.api_secret == "from_cli_secret"


def test_load_priority_config_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "cfg.env"
    _write_env(cfg_file)
    monkeypatch.setenv(ENV_API_KEY, "env_key_only")
    monkeypatch.setenv(ENV_API_SECRET, "env_secret_only")

    creds = load_binance_credentials(
        secrets_file_cli=None,
        settings={"secrets": {"file": str(cfg_file)}},
        project_root=tmp_path,
    )
    assert creds.api_key == FAKE_KEY
    assert creds.api_secret == FAKE_SECRET


def test_load_falls_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    monkeypatch.setenv(ENV_API_SECRET, FAKE_SECRET)
    creds = load_binance_credentials(
        secrets_file_cli=None,
        settings={},
        project_root=tmp_path,
    )
    assert creds.api_key == FAKE_KEY


def test_missing_credentials_message_has_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_API_SECRET, raising=False)
    with pytest.raises(SecretsError) as exc:
        load_binance_credentials(secrets_file_cli=None, settings={}, project_root=tmp_path)
    msg = str(exc.value)
    assert FAKE_SECRET not in msg
    assert FAKE_KEY not in msg


def test_incomplete_file_error_has_no_secret(tmp_path: Path) -> None:
    bad = tmp_path / "bad.env"
    bad.write_text(f"{ENV_API_KEY}=only_key\n", encoding="utf-8")
    with pytest.raises(SecretsError) as exc:
        load_binance_credentials(secrets_file_cli=bad, settings={}, project_root=tmp_path)
    msg = str(exc.value)
    assert "only_key" not in msg
    assert FAKE_SECRET not in msg


def test_credentials_repr_redacts() -> None:
    c = BinanceCredentials(api_key=FAKE_KEY, api_secret=FAKE_SECRET)
    r = repr(c)
    assert FAKE_SECRET not in r
    assert FAKE_KEY not in r
    assert "***" in r


def test_load_from_relative_secrets_file(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "config" / "secrets"
    secrets_dir.mkdir(parents=True)
    env_path = secrets_dir / "testnet.env"
    _write_env(env_path)
    creds = load_binance_credentials(
        secrets_file_cli=Path("config/secrets/testnet.env"),
        settings=None,
        project_root=tmp_path,
    )
    assert creds.api_key == FAKE_KEY
