# Live 上线前检查清单（可打印）

> 完整步骤与命令见 [`docs/live-go-live-acceptance.md`](../live-go-live-acceptance.md)。  
> **每条**需要执行 `python -m main …` 的终端命令前必须先：`conda activate roll-env`。

## A. 环境与隔离（一次性）

- [ ] `config/settings.testnet.yaml` 与 `config/settings.live.yaml` 已分别从 example 复制并审查
- [ ] `secrets.file` 分别指向 `testnet.env` / `live.env`，**未混用**
- [ ] `state.path` 分别为 `roll_state_testnet.json` / `roll_state_live.json`
- [ ] Testnet `rest_base` = `https://testnet.binancefuture.com`
- [ ] live `rest_base` = `https://dapi.binance.com`
- [ ] `chmod 700 config/secrets`；`chmod 600 config/secrets/*.env`（Linux）
- [ ] live API Key：**禁止提现**；已配置服务器 IP **白名单**（若启用）
- [ ] `pytest` 通过；`bash scripts/acceptance/preflight.sh` 通过

## B. 阶段 1 — Testnet 开仓→平仓闭环（必做）

- [ ] `strategy.testnet_signed_orders_enabled: true`
- [ ] `bash scripts/acceptance/phase1-testnet-closed-loop.sh` **或** 手动对账 + `run-loop --once --no-dry-run`
- [ ] 运行中观察到至少一次开仓与最终平仓（或策略退出至空仓）
- [ ] 停止后 `reconcile-state`：`nonzero_position_symbols=[]`、`symbols_with_open_orders=[]`
- [ ] `halt_automatic_trading=False`
- [ ] [Binance Futures Testnet](https://testnet.binancefuture.com) COIN-M 网页：仓位 0、无挂单
- [ ] 已填写 `docs/templates/live-acceptance-record.template.md` 阶段 1

## C. 阶段 2 — Live dry-run ≥24h（必做）

- [ ] `strategy.live_trading_enabled` **仍为 `false`**（本阶段禁止 signed）
- [ ] `bash scripts/acceptance/phase2-live-dry-run-start.sh` 启动；**勿**加 `--no-dry-run`
- [ ] 连续运行 ≥24 小时（`phase2-live-dry-run-check.sh` 显示已满 24h）
- [ ] 日志：候选标的足够、周期扫描正常、无 Secret 明文
- [ ] `[dry-run]` 决策与人工预期一致
- [ ] 已记录 dry-run 起止时间与日志路径

## D. 阶段 3 — Live 对账（signed 前必做）

- [ ] `bash scripts/acceptance/phase3-live-reconcile.sh` 退出码为 0
- [ ] `nonzero_position_symbols=[]`
- [ ] `symbols_with_open_orders=[]`
- [ ] `halt_automatic_trading=False`；`halt_reason` 为空
- [ ] [Binance](https://www.binance.com) 实盘 **COIN-M** 与对账一致
- [ ] 已清理一切非预期持仓/挂单（若有）

## E. 阶段 4 — Live 首次 signed 极小资金（必做）

- [ ] 账户仅保留**可承受全部损失**的极小资金
- [ ] 已合并 `config/settings.live.minimal-funds.example.yaml` 中的保守参数到 `settings.live.yaml`
- [ ] `strategy.live_trading_enabled: true`
- [ ] `pgrep` / `systemctl` 确认**无**其它 live signed 进程
- [ ] `bash scripts/acceptance/phase4-live-first-signed-once.sh`（= 对账 + `--once --no-dry-run`）
- [ ] 单轮行为符合预期；停止后对账空仓
- [ ] 网页 COIN-M 复核
- [ ] `bash scripts/acceptance/collect-session.sh <会话ID>` 已归档日志与对账

## F. 阶段 5 — 移交 systemd（单轮通过后再做）

- [ ] **不要**与前台 `run-loop --no-dry-run` 同时运行
- [ ] 再次 `phase3-live-reconcile.sh` 通过
- [ ] `sudo systemctl start roll-live`；`status` 为 `active (running)`
- [ ] `journalctl -u roll-live -n 200` 无持续错误
- [ ] **未**执行 `enable roll-live`（除非明确接受重启自启）
- [ ] 记录人工复查结论：是否批准常驻

## G. 仍须人工确认（无法自动化）

- [ ] 合约规则（最小数量、tick、杠杆上限）与 Testnet 差异已阅读
- [ ] 服务器 NTP / 时间同步正常
- [ ] 告警与值班：谁负责停机和网页平仓
- [ ] 密钥泄漏应急预案
- [ ] 接受「不保证盈利」与最大可承受亏损
