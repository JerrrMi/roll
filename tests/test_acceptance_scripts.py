"""验收脚本与文档存在性（不执行交易所或 conda）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ACCEPTANCE_SCRIPTS = [
    "scripts/acceptance/preflight.sh",
    "scripts/acceptance/phase1-testnet-closed-loop.sh",
    "scripts/acceptance/phase2-live-dry-run-start.sh",
    "scripts/acceptance/phase2-live-dry-run-check.sh",
    "scripts/acceptance/phase3-live-reconcile.sh",
    "scripts/acceptance/phase4-live-first-signed-once.sh",
    "scripts/acceptance/collect-session.sh",
    "scripts/acceptance/_common.sh",
]

DOCS = [
    "docs/live-go-live-acceptance.md",
    "docs/checklists/live-go-live-checklist.md",
    "docs/templates/live-acceptance-record.template.md",
    "config/settings.live.minimal-funds.example.yaml",
    "config/settings.testnet.minimal-funds.example.yaml",
]


def test_acceptance_scripts_exist() -> None:
    for rel in ACCEPTANCE_SCRIPTS:
        assert (ROOT / rel).is_file(), rel


def test_acceptance_docs_exist() -> None:
    for rel in DOCS:
        assert (ROOT / rel).is_file(), rel


def test_common_sh_mentions_roll_env() -> None:
    text = (ROOT / "scripts/acceptance/_common.sh").read_text(encoding="utf-8")
    assert "roll-env" in text
    assert "conda activate" in text
