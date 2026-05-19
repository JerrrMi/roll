# Live 上线验收脚本（USD-M / U 本位）

配合 [`docs/live-go-live-acceptance.md`](../../docs/live-go-live-acceptance.md) 使用。

**当前系统标准：** Binance **USD-M / U 本位 USDT 永续**（`product: usdm`，live `https://fapi.binance.com` + `/fapi/v1`）。脚本会校验配置不含 COIN-M 的 `/dapi` 或 `dapi.binance.com`。

**约定：** 交互式 shell 中**每条命令前先** `conda activate roll-env`；脚本内部运行 `python -m main` 前也会自动激活 `roll-env`。

## 快速顺序

```bash
conda activate roll-env
cd /opt/roll

bash scripts/acceptance/preflight.sh
export ROLL_ACCEPTANCE_SESSION="accept-$(date -u +%Y%m%dT%H%M%SZ)"

bash scripts/acceptance/phase1-testnet-closed-loop.sh
# 另开终端或 tmux（同样先 conda activate roll-env）：
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
| `preflight.sh` | 配置/密钥/状态路径隔离；拒绝 `/dapi` 与 `dapi.binance.com` |
| `phase1-testnet-closed-loop.sh` | USD-M Testnet 对账 → `--once --no-dry-run` → 对账 |
| `phase2-live-dry-run-start.sh` | live 配置 dry-run 前台循环 + 日志（`fapi.binance.com` 公共行情） |
| `phase2-live-dry-run-check.sh` | 验证 dry-run 是否已满 24h |
| `phase3-live-reconcile.sh` | live 对账并断言空仓 |
| `phase4-live-first-signed-once.sh` | live 对账 → 单轮 signed → 对账 |
| `collect-session.sh` | 归档对账、状态、journalctl |

产物目录：`logs/acceptance/<会话ID>/`（已在 `.gitignore` 的 `logs/` 下）。

阶段完成后请在 Binance **U 本位合约 / USD-M Futures** 网页复核（Testnet 或实盘），勿在 COIN-M 板块查找持仓。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `ROLL_ACCEPTANCE_SESSION` | 会话 ID，默认 UTC 时间戳 |
| `ROLL_DRY_RUN_MIN_HOURS` | dry-run 最短小时数，默认 `24` |

## Windows

Git Bash / WSL 可运行上述 bash 脚本。纯 PowerShell 请按 `docs/live-go-live-acceptance.md` 中的手动命令执行（每条前先 `conda activate roll-env`）。
