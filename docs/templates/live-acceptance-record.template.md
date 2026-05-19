# Live 验收 / 试运行记录

> 复制本模板到 `logs/acceptance/<会话ID>/record.md`（`logs/` 已在 `.gitignore` 中，勿提交真实 Key 或账户明细到 Git）。
> 收集脚本：`bash scripts/acceptance/collect-session.sh <会话ID>` 可自动拉取对账输出与状态快照。

## 元信息

| 字段 | 填写 |
| --- | --- |
| 会话 ID | |
| 执行人 | |
| 服务器 / 环境 | 例：`ubuntu@x.x.x.x` `/opt/roll` |
| 阶段 | 例：`testnet-closed-loop` / `live-dry-run-24h` / `live-first-signed-once` / `systemd-handoff` |
| 开始时间 (UTC) | |
| 结束时间 (UTC) | |

## 阶段 1：Testnet 开仓→平仓闭环

| 检查项 | 结果 (✓/✗/N/A) | 备注 |
| --- | --- | --- |
| `testnet_signed_orders_enabled: true` | | |
| 启动前 `reconcile-state` 通过（无 halt） | | |
| `run-loop --once --no-dry-run` 或等价闭环完成 | | |
| 停止后 `reconcile-state`：`nonzero_position_symbols=[]` | | |
| 停止后 `reconcile-state`：`symbols_with_open_orders=[]` | | |
| Testnet 网页 COIN-M 仓位=0、无挂单 | | |

**命令与日志文件路径：**

```
（粘贴或指向 logs/acceptance/<会话ID>/ 下文件）
```

**当次交易摘要（Testnet）：**

| 时间 (UTC) | Symbol | 方向 | 数量 | 开仓/平仓 | 订单 ID / 备注 |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## 阶段 2：Live dry-run ≥24h（实盘公共行情）

| 检查项 | 结果 | 备注 |
| --- | --- | --- |
| 使用 `config/settings.live.yaml`，**未**加 `--no-dry-run` | | |
| `live_trading_enabled` 仍为 `false`（dry-run 阶段） | | |
| 连续运行 ≥24 小时 | | 起止见 `dry-run-started-at.txt` |
| 候选标的 ≥ `min_monitor_symbols` | | |
| 日志无持续 API 错误 / 无 Secret 泄露 | | |
| 信号与 `[dry-run]` 决策符合预期 | | |

**dry-run 起止：**

- 开始：
- 结束：
- 日志：`logs/acceptance/<会话ID>/live-dry-run.log`（或 tee 路径）

## 阶段 3：Live 对账（signed 前）

**`reconcile-state` 原始输出（粘贴或附件）：**

```
```

| 字段 | 期望值 | 实际 |
| --- | --- | --- |
| `environment` | `live` | |
| `rest_base` | `https://dapi.binance.com` | |
| `nonzero_position_symbols` | `[]` | |
| `symbols_with_open_orders` | `[]` | |
| `halt_automatic_trading` | `False` | |
| `halt_reason` | `None` 或空 | |

| 检查项 | 结果 | 备注 |
| --- | --- | --- |
| 无非预期持仓（含手动遗留仓） | | |
| 无非预期挂单 | | |
| Binance 实盘 COIN-M 网页与对账一致 | | |

## 阶段 4：Live 首次 signed（极小资金，`--once --no-dry-run`）

| 检查项 | 结果 | 备注 |
| --- | --- | --- |
| 已采用极小资金 / 保守 `strategy` 参数（见 `settings.live.minimal-funds.example.yaml`） | | |
| `live_trading_enabled: true` | | |
| 启动前对账通过 | | |
| 仅一个 live 进程（无重复 `run-loop` / `roll-live`） | | |
| `--once --no-dry-run` 单轮完成 | | |
| 停止后对账空仓、无挂单 | | |
| 网页复核 COIN-M | | |

**当次交易摘要（Live）：**

| 时间 (UTC) | Symbol | 方向 | 数量 | 开仓/平仓 | 成交价/手续费 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

**运行日志摘录（关键行）：**

```
```

## 阶段 5：移交 systemd 持续运行（可选）

| 检查项 | 结果 | 备注 |
| --- | --- | --- |
| 单轮 live 行为符合预期 | | |
| `sudo systemctl start roll-live` 前再次对账 | | |
| 未与前台 `run-loop` 重复运行 | | |
| `journalctl -u roll-live` 正常 | | |
| **未**默认 `enable roll-live`（除非明确接受重启自启） | | |

## 人工复查结论

- [ ] 策略行为与 Testnet / dry-run 观察一致，无意外开仓/重复下单
- [ ] 止损 / STOP 挂单在交易所可见且符合预期（若有持仓）
- [ ] 亏损与滑点在可接受范围
- [ ] 应急流程已演练：停进程 → 网页撤单 → 市价平仓 → 对账

**签字 / 日期：**

**是否批准进入 systemd 常驻：**

- [ ] 是，自 ______ UTC 起 `systemctl start roll-live`
- [ ] 否，原因：____________________________________________
