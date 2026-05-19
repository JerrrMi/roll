# Live 上线验收脚本

配合 [`docs/live-go-live-acceptance.md`](../../docs/live-go-live-acceptance.md) 使用。

**约定：** 脚本内部会执行 `conda activate roll-env`；若你在交互式 shell 中单独运行 `python -m main`，也请先手动激活同一环境。

## 快速顺序

```bash
cd /opt/roll
conda activate roll-env

bash scripts/acceptance/preflight.sh
export ROLL_ACCEPTANCE_SESSION="accept-$(date -u +%Y%m%dT%H%M%SZ)"

bash scripts/acceptance/phase1-testnet-closed-loop.sh
# 另开终端或 tmux：
bash scripts/acceptance/phase2-live-dry-run-start.sh
# ≥24h 后 Ctrl+C，再：
bash scripts/acceptance/phase2-live-dry-run-check.sh

bash scripts/acceptance/phase3-live-reconcile.sh
# 合并 minimal-funds 参数并 live_trading_enabled: true 后：
bash scripts/acceptance/phase4-live-first-signed-once.sh
bash scripts/acceptance/collect-session.sh "$ROLL_ACCEPTANCE_SESSION"
```

## 脚本说明

| 脚本 | 作用 |
| --- | --- |
| `preflight.sh` | 配置/密钥/状态路径隔离 |
| `phase1-testnet-closed-loop.sh` | Testnet 对账 → `--once --no-dry-run` → 对账 |
| `phase2-live-dry-run-start.sh` | live 配置 dry-run 前台循环 + 日志 |
| `phase2-live-dry-run-check.sh` | 验证 dry-run 是否已满 24h |
| `phase3-live-reconcile.sh` | live 对账并断言空仓 |
| `phase4-live-first-signed-once.sh` | live 对账 → 单轮 signed → 对账 |
| `collect-session.sh` | 归档对账、状态、journalctl |

产物目录：`logs/acceptance/<会话ID>/`（已在 `.gitignore` 的 `logs/` 下）。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `ROLL_ACCEPTANCE_SESSION` | 会话 ID，默认 UTC 时间戳 |
| `ROLL_DRY_RUN_MIN_HOURS` | dry-run 最短小时数，默认 `24` |

## Windows

Git Bash / WSL 可运行上述 bash 脚本。纯 PowerShell 请按 `docs/live-go-live-acceptance.md` 中的手动命令执行。
