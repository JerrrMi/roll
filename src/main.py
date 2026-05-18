"""应用入口：骨架阶段仅演示配置加载与模块导入，不连接交易所。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Binance COIN-M 滚仓系统（骨架）")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/settings.yaml"),
        help="配置文件路径（默认可复制 settings.example.yaml）",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    cfg_path = args.config if args.config.is_absolute() else project_root / args.config

    import yaml

    from roll.logger import get_logger

    log = get_logger("main")
    if not cfg_path.is_file():
        log.warning("配置文件不存在: %s — 请复制 config/settings.example.yaml", cfg_path)
        return 0

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    settings = raw if isinstance(raw, dict) else {}
    log.info(
        "已加载配置 environment=%s rest_base=%s（骨架未发起任何 API 请求）",
        settings.get("environment"),
        settings.get("binance", {}).get("rest_base"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
