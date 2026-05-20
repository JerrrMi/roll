下面是按 **Plan 3.0（含继承自 1.0 的策略设计）** 与 **当前仓库代码** 整理的缺口对照表，并附建议开发优先级。

状态图例：**✅ 已实现** · **⚠️ 部分** · **❌ 未实现**

---

## 总览：按域汇总

| 域                | Plan 要求项 | ✅    | ⚠️    | ❌    |
| ----------------- | ----------- | ---- | ---- | ---- |
| A. 核心滚仓策略   | 8           | 8    | 0    | 0    |
| B. 出场与持仓管理 | 7           | 7    | 0    | 0    |
| C. 风控与熔断     | 8           | 8    | 0    | 0    |
| D. USD-M 基础设施 | 10          | 10   | 0    | 0    |
| E. 交易所账户模式 | 4           | 4    | 0    | 0    |
| F. 回测与参数校准 | 5           | 5    | 0    | 0    |
| G. 运维与状态机   | 6           | 6    | 0    | 0    |

**结论**：3.0 **迁移层、P1 风险闭环、P2 策略完善与 P3 工程质量已落地**。

---

## 详细对照表

### A. 核心滚仓策略（产品差异化）

| ID   | Plan 设计                                                  | 当前代码 | 缺口说明                                                     | 建议优先级 | 工作量 | 主要落点                                               |
| ---- | ---------------------------------------------------------- | -------- | ------------------------------------------------------------ | ---------- | ------ | ------------------------------------------------------ |
| A1   | **浮盈再投入 / 加仓**（USDT 权益含 uPnL；每轮 2–3 次上限） | ✅        | 持仓时 `allow_scan_candidates()` 为 false，无任何二次 MARKET 加仓路径 | **P0**     | L      | `usdm_auto_trade.py`, `risk.py`, `position_manager.py` |
| A2   | **盈利后分层降杠杆**（25x→5x）                             | ✅        | `position_roll.target_leverage_for_profit` + 持仓期 `set_leverage`；加仓 sizing 用 `effective_leverage` | —          | —      | `position_roll.py`, `usdm_auto_trade.py`               |
| A3   | 多周期趋势评分 15m/1h/4h                                   | ✅        | `TrendModel` 已实现                                          | —          | —      | `trend_model.py`                                       |
| A4   | long/short/no_trade + 拒绝原因                             | ✅        | 已实现                                                       | —          | —      | `trend_model.py`                                       |
| A5   | 多标的扫描、单标的交易                                     | ✅        | `PositionManager` + 对账锁                                   | —          | —      | `position_manager.py`                                  |
| A6   | 开仓数量由 **止损距离反推**（非保证金倒推）                | ✅        | `compute_position_quantity`                                  | —          | —      | `risk.py`                                              |
| A7   | 加仓时趋势仍须 **强**（同向阈值）                          | ✅        | 依赖 A1                                                      | **P0**     | S      | 复用 `TrendModel.evaluate`                             |
| A8   | 加仓后仍受 Kelly / max_position / 单笔亏损约束             | ✅        | 依赖 A1；`evaluate_open` 仅用于首次开仓                      | **P0**     | M      | `risk.py` 需支持「增量 sizing」                        |

---

### B. 出场与持仓管理

| ID   | Plan 设计                                            | 当前代码 | 缺口说明                                                     | 建议优先级 | 工作量 | 主要落点                                                   |
| ---- | ---------------------------------------------------- | -------- | ------------------------------------------------------------ | ---------- | ------ | ---------------------------------------------------------- |
| B1   | **固定比例止损**（`stop_adverse_fraction`，默认 5%） | ✅        | 固定底价 + STOP_MARKET closePosition                         | —          | —      | `risk.py`, `usdm_auto_trade.py`                            |
| B2   | **追踪止损**（盈利后抬高/下移 STOP）                 | ✅        | 示例 YAML 默认 `trail_stop_fraction: 0.02`；live/回测 `desired_protective_stop_price` | —          | —      | `strategy_loop.py`, `usdm_auto_trade.py`, `config/*.yaml` |
| B3   | **ATR 止损**（`k × ATR`）                            | ✅        | `wilder_atr_last` + 与固定/追踪取最保守价；live 拉 K 线、回测用 15m 切片 | —          | —      | `trend_model.py`, `usdm_auto_trade.py`, `backtest.py`      |
| B4   | **趋势反转退出**                                     | ✅        | 反向强信号 + 同向 `score` 跌破 `exit_threshold`（默认 0.35） | —          | —      | `should_exit_from_trend`, `trend_model.py`                 |
| B5   | **最大持仓时间**（如 48h / N 根 4h）                 | ✅        | `max_hold_hours`（默认 48h）；`should_exit_max_hold` + `position_opened_unix_ms` | —          | —      | `usdm_auto_trade.py`, `backtest.py`, `strategy_loop.py`    |
| B6   | 持仓期间维护 STOP（滚动改价）                        | ✅        | `ensure_or_roll_protective_stop`                             | —          | —      | `usdm_auto_trade.py`                                       |
| B7   | 平仓后 **COOLDOWN**（禁止立即反手）                  | ✅        | 写 `cooldown_until_unix_ms`；扫描前检查；不再立即 `finish_cooldown_to_idle()` | —          | —      | `usdm_auto_trade.py`, `state_store.py`                     |
| B8   | 持仓期间其他 symbol 只记录不下单                     | ✅        | `log_peer_symbol_signals_while_in_position`                  | —          | —      | `usdm_auto_trade.py`                                       |

---

### C. 风控与账户熔断

| ID   | Plan 设计                        | 当前代码 | 缺口说明                                                     | 建议优先级 | 工作量 | 主要落点                            |
| ---- | -------------------------------- | -------- | ------------------------------------------------------------ | ---------- | ------ | ----------------------------------- |
| C1   | 单笔最大亏损上限（~1–3% 权益）   | ✅        | `max_single_loss_fraction=0.02`，开仓 sizing 约束            | —          | —      | `risk.py`                           |
| C2   | Kelly 门槛（非正 Kelly 不开仓）  | ✅        | `evaluate_open`                                              | —          | —      | `risk.py`                           |
| C3   | max_position_fraction 上限       | ✅        | 开仓 sizing                                                  | —          | —      | `risk.py`                           |
| C4   | **最大回撤熔断**                 | ✅        | 每轮 `update_equity` + `account_risk` 持久化；触发后 `pause_opening_entries`（不强制平仓） | —          | —      | `usdm_auto_trade.py`, `risk.py`, `state_store.py` |
| C5   | **日内最大亏损熔断**             | ✅        | 同上                                                         | —          | —      | 同上                                |
| C6   | **连续亏损 + 冷却**              | ✅        | 平仓 `record_realized_pnl`；冷却写入 state + 扫描门控       | —          | —      | `usdm_auto_trade.py`, `state_store.py`         |
| C7   | 熔断时 **已有持仓仍须管理/保护** | ✅        | 熔断用 pause 非 halt；halt 时仍允许 EXITING；持仓分支与 halt 解耦 | —          | —      | `usdm_auto_trade.py`, `position_manager.py`       |
| C8   | 风控参数 **YAML 可配**           | ✅        | `risk:` 已写入 testnet/live/example YAML；`parse_risk_limits_settings` 共用 | —          | —      | `config/*.yaml`, `risk.py` |

---

### D. USD-M 基础设施（3.0 迁移）

| ID   | Plan 设计                               | 当前代码 | 缺口说明                                                     | 建议优先级 | 工作量 | 主要落点                    |
| ---- | --------------------------------------- | -------- | ------------------------------------------------------------ | ---------- | ------ | --------------------------- |
| D1   | product=usdm, /fapi/v1, fapi host       | ✅        | `binance_config`, `signed_guard`                             | —          | —      |                             |
| D2   | 拒绝 dapi / COIN-M 误配                 | ✅        | 启动 guard + 测试                                            | —          | —      |                             |
| D3   | USDT 权益读取                           | ✅        | `parse_usdt_account_snapshot`                                | —          | —      | `usdm_account.py`           |
| D4   | quantity = base asset，USDT 线性 PnL    | ✅        | `usdm_account`, `backtest`                                   | —          | —      |                             |
| D5   | LOT_SIZE / MIN_NOTIONAL / 保证金预检    | ✅        | `precheck_usdm_market_open`                                  | —          | —      |                             |
| D6   | Testnet/live signed 闭环                | ✅        | `usdm_auto_trade.run_live_strategy_iteration`                | —          | —      |                             |
| D7   | 对账 + 单标的锁恢复                     | ✅        | `reconcile_usdm_account`, `PositionManager`                  | —          | —      |                             |
| D8   | STOP_MARKET closePosition 保护单        | ✅        | `new_stop_market_close_position`                             | —          | —      |                             |
| D9   | dry-run / run-loop / systemd / 验收文档 | ✅        | README + `docs/live-go-live-acceptance.md`                   | —          | —      |                             |
| D10  | 清理误导性 COIN-M 命名                  | ✅        | 主名 `BinanceUsdmSignedClient`；`BinanceCoinM*` 为 deprecated 别名；注释已改 `/fapi` | —          | —      | `binance_client.py`, 各模块 import |

---

### E. 交易所账户模式（Plan §7.3）

| ID   | Plan 设计                               | 当前代码 | 缺口说明                                                     | 建议优先级 | 工作量 | 主要落点                         |
| ---- | --------------------------------------- | -------- | ------------------------------------------------------------ | ---------- | ------ | -------------------------------- |
| E1   | **逐仓/全仓** 须在配置或文档明确        | ✅        | `binance.margin_type` + README「保证金模式」；默认 ISOLATED  | —          | —      | `config/*.yaml`, `README.md`       |
| E2   | 启动前校验/设置 margin type             | ✅        | `account_modes.ensure_symbol_margin_type` + `change_margin_type` | —          | —      | `account_modes.py`, `binance_client.py` |
| E3   | **单向净持仓** 为目标；hedge 异常应拒绝 | ✅        | hedge → `set_halt_for_manual_review` + `[live.alert]` 日志   | —          | —      | `usdm_auto_trade.py`             |
| E4   | 双向模式下正确平指定腿；**开仓前拒绝 hedge 模式** | ✅        | 开仓前 `ensure_one_way_position_mode_before_open`；hedge → halt + `[live.alert]`；平仓仍用 `positionSide` 逻辑 | —          | —      | `account_modes.py`, `usdm_auto_trade.py` |

---

### F. 回测与参数校准（Plan §7.4）

| ID   | Plan 设计                           | 当前代码 | 缺口说明                               | 建议优先级 | 工作量 | 主要落点                 |
| ---- | ----------------------------------- | -------- | -------------------------------------- | ---------- | ------ | ------------------------ |
| F1   | USD-M K 线 + USDT PnL 回测          | ✅        | `backtest.py`                          | —          | —      |                          |
| F2   | 手续费 + 滑点                       | ✅        | 回测有；live 发单前 `[live][cost_est]` 估算日志（`estimated_taker_fee_bps` / `estimated_slippage_bps`） | —          | —      | `cost_estimate.py`, `usdm_auto_trade.py` |
| F3   | **资金费率** 纳入持仓成本           | ✅        | 回测默认拉 `/fapi/v1/fundingRate` 持仓期扣减；`--no-funding` 可关 | —          | —      | `backtest.py`, `binance_client.py` |
| F4   | 参数扫描（stop/kelly/threshold 等） | ✅        | `DEFAULT_SENSITIVITY_GRID` + CLI       | —          | —      |                          |
| F5   | **USD-M 重校准后的推荐默认参数**    | ✅        | example YAML：`trail/atr/max_hold`、`risk:`、`kelly` 等 USD-M 推荐集 | —          | —      | `config/*.yaml`, `README.md`       |

---

### G. 架构与模块完整性

| ID   | Plan 设计                              | 当前代码 | 缺口说明                                                 | 建议优先级 | 工作量 | 主要落点             |
| ---- | -------------------------------------- | -------- | -------------------------------------------------------- | ---------- | ------ | -------------------- |
| G1   | `order_executor.py` 负责下单/撤单/确认 | ✅        | 模块已 **正式废弃**；执行在 `usdm_auto_trade.py`；实例化触发 DeprecationWarning | —          | —      | `order_executor.py`, README 架构说明 |
| G2   | 异常时 pause 新开、保留持仓管理        | ✅        | 异常/circuit 均 `pause_opening_entries`；持仓管理独立分支     | —          | —      | `usdm_auto_trade.py` |
| G3   | 状态持久化（symbol/方向/止损/extreme） | ✅        | `state_store` + `live` leaf                              | —          | —      |                      |
| G4   | live 单进程互斥锁                      | ✅        | `process_lock.py`                                        | —          | —      |                      |

---

## 建议开发优先级（路线图）

### P0 — 补齐「滚仓」本体（否则系统名实不符） ✅ 已完成

| 顺序 | 任务                               | 状态 | 验收标准                                                     |
| ---- | ---------------------------------- | ---- | ------------------------------------------------------------ |
| 1    | **A1 + A7 + A8**：持仓期间浮盈加仓 | ✅   | Testnet：同一 symbol 盈利后第 2 次 MARKET 加仓；超 max_add 拒绝 |
| 2    | 加仓状态持久化                     | ✅   | 重启后对账能恢复 add_count、均价、extreme                    |
| 3    | 回测同步加仓逻辑                   | ✅   | `backtest.py` 与 live 行为一致                               |

**为什么 P0**：Plan 1.0 核心就是「25x 起步 + 浮盈再投入」；当前只有单次开仓，产品定位差距最大。

---

### P1 — 风险闭环（live 可长期跑）✅ 已完成

| 顺序 | 任务                                                         | 状态 | 验收标准                                                     |
| ---- | ------------------------------------------------------------ | ---- | ------------------------------------------------------------ |
| 4    | **B7**：真实 COOLDOWN（如 3600s，写 `cooldown_until_unix_ms`） | ✅    | 平仓后 N 秒内不扫描新开仓                                    |
| 5    | **C4–C7**：账户熔断接入 live 循环                            | ✅    | 超回撤/日亏后 pause 新开；已有仓继续管 STOP                  |
| 6    | **B4**：趋势退出细化                                         | ✅    | 除反向信号外，支持「同向 score 跌破 exit_threshold」         |
| 7    | **E3**：hedge 检测 → halt + 明确告警                         | ✅    | 不对 hedge 仓静默跳过管理                                    |
| 8    | **A2**：盈利分层降杠杆（至少影响后续加仓 sizing）            | ✅    | 日志 `[live][deleverage]` + 加仓用降低后杠杆                 |

---

### P2 — 策略完善与可运维 ✅ 已完成

| 顺序 | 任务                                                         | 状态 | 验收标准                                                     |
| ---- | ------------------------------------------------------------ | ---- | ------------------------------------------------------------ |
| 9    | **B2**：默认或文档明确启用 `trail_stop_fraction`             | ✅    | 三份 example YAML 含 `trail_stop_fraction: 0.02`；回测/live STOP 合并追踪价 |
| 10   | **B3**：ATR 止损（与固定/追踪取最保守）                      | ✅    | `pytest tests/test_p2_strategy.py`；日志 `[live][atr_stop]` |
| 11   | **B5**：最大持仓时间退出                                     | ✅    | `max_hold_hours: 48`；超时日志 `[live][exit] reason=max_hold:...` |
| 12   | **C8**：`risk:` YAML 块补全至 testnet example                | ✅    | testnet/live/example 均含完整 `risk:` 块                     |
| 13   | **E1–E2**：margin type 检查/设置                             | ✅    | `binance.margin_type` + 开仓前 `ensure_symbol_margin_type`；不匹配抛错 |
| 14   | **F5**：USD-M 推荐参数写入 example YAML                      | ✅    | 见 `config/settings.*.example.yaml` 与 README「策略保护止损」 |
| 15   | **E4**：开仓前 hedge 模式检查                                | ✅    | hedge 账户下 `[live.alert]` + halt；one-way 日志 `[live][position_mode]` |

---

### P3 — 工程质量（不阻塞交易）✅ 已完成

| 顺序 | 任务                                    | 状态 | 验收标准                                                     |
| ---- | --------------------------------------- | ---- | ------------------------------------------------------------ |
| 15   | **D10**：COIN-M 别名/注释清理           | ✅    | 代码主用 `BinanceUsdm*`；`BinanceCoinM*` 为别名；无当前路径 `/dapi` 注释 |
| 16   | **G1**：`order_executor` 合并或正式废弃 | ✅    | `OrderExecutor()` 触发 DeprecationWarning；dry-run 日志指向 `usdm_auto_trade` |
| 17   | **F3**：回测资金费率                    | ✅    | `pytest tests/test_p3_engineering.py`；回测日志 `[bt funding]`；`--no-funding` |
| 18   | **F2**：live 侧手续费/滑点估算日志      | ✅    | 开/加/平仓前 `[live][cost_est]`；默认 fee_bps=5 slip_bps=2 |
