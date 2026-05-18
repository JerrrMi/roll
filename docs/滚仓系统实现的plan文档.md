# 滚仓系统实现的 Plan 文档

> 本文档基于 `docs/執行摘要.pdf`，用于指导后续在 Cursor 中分阶段实现一套精简、高效、专注 Binance COIN-M Futures API 的加密货币滚仓自动交易系统。本文档只描述实现计划、验收方式和使用方法，不包含代码。

## 1. 背景与目标

本系统的目标是把“25 倍初始杠杆、随盈利降低杠杆、浮盈再投入、仅在强单边趋势行情启动”的滚仓策略落地为可运行的 Python 自动交易系统。

核心目标如下：

- 交易所：Binance COIN-M Futures，优先使用 Testnet 完成所有接口与流程验收。
- 系统形态：Python 精简单体程序，无前端页面，无双端分离，无复杂微服务。
- 标的监测：至少同时监测 3 个候选加密货币标的，例如 DOGE、AVAX、SHIB、WIF、PEPE、TON、HYPE 等。
- 交易约束：任何时刻不允许同时交易 2 个及以上标的。系统只能在一个标的上开仓、滚仓、平仓，结束后再回到监测循环。
- 交易动作：满足信号后自动交易，不需要用户二次确认。
- 策略重点：单边上涨或单边下跌趋势行情判断是系统基石，必须使用明确、可回测、可解释、可验收的数学模型。
- 运行环境：后续任何终端命令执行前都必须先执行 `conda activate roll-env`。

## 2. 关键工程需求

执行摘要中的策略要求可以转化为以下工程需求：

| 策略要求 | 工程实现要求 |
| --- | --- |
| 25 倍初始杠杆 | 支持为目标 COIN-M 合约设置杠杆，并在下单前验证当前杠杆状态 |
| 随盈利降低杠杆 | 持仓盈利达到分层阈值时，降低后续风险暴露，不盲目维持 25x |
| 浮盈再投入 | 使用账户权益与未实现盈亏重新计算可用风险预算，但必须受 Kelly 上限、最大仓位和交易所规则限制 |
| 只在强趋势启动 | 构建趋势评分模型，过滤震荡市和弱趋势 |
| 止损与回撤控制 | 下单后立即建立止损逻辑，并持续监控账户级、标的级和单笔交易风险 |
| 多标的监测、单标的交易 | 监测器可以扫描多个候选，但交易执行器必须由全局交易锁保护 |
| Binance API 可测试 | 每个实现阶段都要能通过 Binance Testnet 或 public API 验证 |

## 3. Binance COIN-M Futures API 范围

### 3.1 基础环境

后续实现阶段默认使用：

- Testnet REST Base URL：`https://testnet.binancefuture.com`
- COIN-M Futures REST Path：`/dapi/v1`
- 实盘 REST Base URL：`https://dapi.binance.com`
- 所有签名请求使用 Binance API Key、Secret 与 HMAC SHA256 签名。
- 所有时间戳使用毫秒级 Unix timestamp，并通过服务器时间校准本地时间偏移。

注意：COIN-M Futures 的可交易合约与 USD-M Futures 不完全一致，且 Testnet 与实盘可用合约也可能不完全一致。因此实现时不应硬编码“某个币一定可交易”，而应通过 `GET /dapi/v1/exchangeInfo` 动态筛选。

### 3.2 必须覆盖的 API 能力

| 能力 | API 验收点 |
| --- | --- |
| 连通性 | `GET /dapi/v1/ping` 成功 |
| 时间同步 | `GET /dapi/v1/time` 成功，并计算本地时间偏移 |
| 合约发现 | `GET /dapi/v1/exchangeInfo` 获取交易规则、价格精度、数量精度、最小下单量 |
| K 线数据 | `GET /dapi/v1/klines` 获取多周期行情 |
| 最新价格 | `GET /dapi/v1/ticker/price` 或订单簿价格接口 |
| 账户信息 | 签名接口获取账户权益、可用保证金、持仓与风险状态 |
| 杠杆设置 | 签名接口调整目标合约 leverage，并验收返回值 |
| 下单 | 签名接口创建 Testnet 订单，优先从小额市价单或限价单开始 |
| 查询订单 | 签名接口查询订单状态 |
| 撤单 | 签名接口撤销未成交订单 |
| 平仓 | 使用 reduce-only 或等效机制关闭持仓 |
| 持仓查询 | 持续校验当前是否已有持仓，保证单标的交易锁有效 |

### 3.3 Testnet 验收原则

每个涉及交易所交互的模块都必须先在 Testnet 验收：

- Public API 模块先验收无密钥请求。
- Signed API 模块使用 Testnet API Key 验收签名、时间偏移和权限。
- 下单模块先使用最小可交易数量。
- 平仓模块必须在 Testnet 中完整完成“开仓 -> 查询持仓 -> 平仓 -> 确认无持仓”闭环。
- 任何失败都必须记录 Binance 返回的 `code`、`msg`、请求 endpoint、symbol 和本地参数，但不得记录 API Secret。

## 4. 标的池与单标的交易规则

### 4.1 候选标的

配置层使用“人类可读资产代码”，例如：

```text
DOGE, AVAX, SHIB, WIF, PEPE, TON, HYPE
```

运行时通过 `exchangeInfo` 映射到 COIN-M 合约 symbol，例如 `DOGEUSD_PERP`、`AVAXUSD_PERP` 等。对于 `SHIB`、`PEPE` 等可能存在 `1000SHIB` 或类似合约命名的标的，不允许代码猜测，必须以交易所返回为准。

### 4.2 标的筛选

系统启动时执行以下筛选：

1. 从 `exchangeInfo` 获取所有 COIN-M 合约。
2. 只保留状态为可交易的永续或配置允许的交割合约。
3. 只保留能匹配候选资产代码的合约。
4. 校验价格精度、数量精度、最小下单量、最小名义价值、最大杠杆等规则。
5. 至少得到 3 个可监测标的，否则系统不得进入自动交易循环。

如果用户指定的候选资产在 COIN-M Testnet 不足 3 个，系统应输出清晰报告，例如“DOGE 可用，AVAX 可用，TON 不可用，HYPE 不可用”，并提示调整候选池或切换到 Binance 支持的其他 COIN-M 标的。

### 4.3 单标的交易锁

系统必须实现全局交易锁：

- `IDLE`：无持仓，允许扫描所有候选标的。
- `ENTERING`：正在为某个标的下单，不允许其他标的触发交易。
- `IN_POSITION`：已有持仓，只允许管理当前标的。
- `EXITING`：正在平仓，不允许新开仓。
- `COOLDOWN`：刚平仓后的冷却期，不允许立即反手或切换追单。

交易锁必须由真实持仓查询兜底校验。即使本地状态文件损坏或进程重启，也必须先查询 Binance 当前持仓，再决定是否恢复管理已有持仓或进入监测状态。

## 5. 趋势行情数学模型设计

趋势判断是本系统最重要的部分。模型目标不是预测所有涨跌，而是只在“单边趋势强、震荡概率低、风险收益比足够”的时候给出交易信号。

### 5.1 数据周期

建议至少使用以下周期：

- `15m`：捕捉入场时机和短期突破。
- `1h`：判断主要交易趋势。
- `4h`：过滤大方向，避免逆大周期趋势开仓。

如果系统先做 MVP，可以先实现 `1h + 4h`，再加入 `15m` 优化入场。

### 5.2 核心特征

对每个 symbol、每个周期计算以下特征：

1. 对数收益趋势斜率  
   对最近 N 根 K 线的 `ln(close)` 做线性回归，得到斜率 `beta`。再用波动率归一化：

   ```text
   slope_score = beta / std(log_return)
   ```

   多头要求 `slope_score > threshold`，空头要求 `slope_score < -threshold`。

2. 趋势显著性  
   使用线性回归的 t 值或 R² 判断趋势是否足够稳定。单纯斜率很大但拟合很差，通常意味着剧烈震荡，不应交易。

3. ADX 与方向性指标  
   使用 ADX 过滤趋势强度：

   ```text
   trend_strength = ADX
   long_direction = +DI > -DI
   short_direction = -DI > +DI
   ```

   建议初始阈值：`ADX >= 25`。高波动山寨币可以通过回测调整为 `20-30`。

4. EMA 排列  
   多头趋势要求短 EMA 高于长 EMA，例如 `EMA20 > EMA50 > EMA100`。空头反向。

5. Donchian 突破  
   多头要求价格接近或突破最近 N 根 K 线高点，空头要求价格接近或跌破最近 N 根 K 线低点。

6. 震荡过滤  
   使用 Choppiness Index、布林带宽度收缩后扩张、或者 ATR 占价格比例过滤横盘。若价格上下穿越 EMA 频繁，判定为震荡，不开仓。

7. 成交量确认  
   突破方向上的成交量应高于近 N 根均量，例如 `volume > SMA(volume, 20) * 1.2`。如果 Testnet 成交量质量不足，回测与实盘 dry-run 阶段必须使用实盘 public market data 验证。

### 5.3 综合趋势评分

每个周期输出一个方向评分：

```text
score_tf = w1 * slope_z
         + w2 * adx_score
         + w3 * ema_stack_score
         + w4 * breakout_score
         + w5 * volume_score
         - w6 * chop_penalty
```

多周期合成：

```text
trend_score = 0.50 * score_4h + 0.35 * score_1h + 0.15 * score_15m
```

信号规则：

- 多头入场：`trend_score >= long_threshold`，且 `4h` 与 `1h` 方向一致。
- 空头入场：`trend_score <= -short_threshold`，且 `4h` 与 `1h` 方向一致。
- 不交易：方向冲突、ADX 不足、震荡惩罚过高、成交量不确认、距离清算价或止损价风险过大。

初始阈值建议：

```text
long_threshold = 0.70
short_threshold = 0.70
min_adx = 25
max_chop = 55
min_regression_r2 = 0.35
```

这些参数必须通过回测和 walk-forward 验证后再用于实盘。

### 5.4 模型验收标准

趋势模型实现后必须满足：

- 对每个候选 symbol 输出方向、分数、触发原因和拒绝原因。
- 能用历史 K 线复现最近若干次信号。
- 在震荡样本中大多数时间输出“不交易”。
- 在明显单边上涨或下跌样本中输出方向一致的高分信号。
- 每次信号必须可解释，例如“4h 斜率为正且显著，ADX=31，EMA 多头排列，1h 突破 20 周期高点，成交量确认”。

## 6. 滚仓、杠杆与仓位模型

### 6.1 初始入场

默认规则：

- 初始杠杆上限：25x。
- 实际风险资金比例由 Kelly 上限、账户风险上限和交易所规则共同决定。
- 不允许全仓。
- 单笔最大可亏损建议从账户权益的 `1%-3%` 起步，绝不应因为 25x 杠杆而把账户暴露在单笔灾难性亏损中。

### 6.2 Kelly 风险预算

执行摘要中建议使用半 Kelly 或更保守仓位。实现时使用：

```text
kelly_fraction = (p * (b + 1) - 1) / b
effective_fraction = clamp(kelly_fraction * kelly_multiplier, 0, max_position_fraction)
```

其中：

- `p`：滚动回测估计胜率。
- `b`：滚动回测估计盈亏比。
- `kelly_multiplier`：建议从 `0.25` 或 `0.50` 开始。
- `max_position_fraction`：单标的最大仓位上限，例如 `10%-20%`。

如果 `kelly_fraction <= 0`，系统不得开仓。

### 6.3 盈利后降低杠杆

示例规则：

| 持仓未实现收益 | 后续目标杠杆 |
| --- | --- |
| < 10% | 25x |
| 10%-20% | 20x |
| 20%-35% | 15x |
| 35%-50% | 10x |
| > 50% | 5x 或只使用追踪止损保护利润 |

实际实现要注意：Binance 调整 leverage 影响的是当前 symbol 的杠杆设置，不等同于自动调整已有仓位风险。降低风险暴露通常需要结合部分平仓、调整止损、减少后续加仓规模来实现。

### 6.4 浮盈再投入

浮盈再投入不是无上限加仓。允许的加仓必须满足：

- 当前趋势评分仍然强。
- 加仓后预估强平风险可接受。
- 加仓后单笔最大亏损仍不超过账户风险预算。
- 加仓后不违反交易所最小/最大数量、精度与保证金限制。
- 加仓次数不超过配置上限，例如每轮趋势最多加仓 2-3 次。

### 6.5 出场规则

至少实现以下出场条件：

- 固定止损：例如入场后价格逆向移动达到 `3%-10%`，根据波动率动态调整。
- ATR 止损：用 `k * ATR` 作为止损距离，避免不同币种波动差异过大。
- 追踪止损：盈利后逐步抬高多头止损或下移空头止损。
- 趋势反转：多周期趋势评分跌破退出阈值。
- 最大持仓时间：例如超过 48 小时或若干根 4h K 线后重新评估。
- 账户级熔断：触发最大回撤或异常风险时强制平仓并暂停。

## 7. 风控与熔断

系统必须把风控放在交易执行之前。

### 7.1 单笔风控

- 单笔最大亏损：建议 `1%-3%` 账户权益。
- 初始止损必须在下单前计算完成。
- 实际下单数量必须基于止损距离反推，而不是只基于可用保证金。
- 预估手续费、滑点、资金费率或持仓成本。

### 7.2 账户级风控

- 日内最大亏损达到阈值后停止开新仓。
- 总权益最大回撤达到阈值后停止策略。
- 连续亏损次数达到阈值后进入冷却。
- API 异常、订单状态不明、持仓与本地状态不一致时停止开新仓。

### 7.3 交易所规则风控

每次下单前都要校验：

- symbol 是否仍处于可交易状态。
- 数量精度、价格精度、最小下单量是否满足。
- 账户保证金是否足够。
- 当前是否已有其他 symbol 持仓。
- 当前 symbol 是否存在未完成订单。
- 订单是否会导致双向持仓或超过单标的约束。

## 8. 系统最小架构

推荐采用精简模块化 Python 结构：

```text
roll/
  config/
    settings.example.yaml
  src/
    main.py
    binance_client.py
    market_data.py
    trend_model.py
    risk.py
    position_manager.py
    order_executor.py
    state_store.py
    logger.py
  tests/
    test_trend_model.py
    test_risk.py
    test_symbol_filter.py
  docs/
    滚仓系统实现的plan文档.md
```

说明：

- `binance_client.py`：封装 Binance COIN-M Futures public 与 signed API。
- `market_data.py`：负责 K 线、价格、订单簿基础数据。
- `trend_model.py`：实现趋势评分与信号解释。
- `risk.py`：实现 Kelly、仓位、止损、回撤与熔断。
- `position_manager.py`：维护交易锁、持仓状态和恢复逻辑。
- `order_executor.py`：负责下单、撤单、平仓和订单状态确认。
- `state_store.py`：用本地 JSON 或 SQLite 保存状态。MVP 推荐 SQLite，便于崩溃恢复。
- `logger.py`：结构化日志，记录每次决策、API 响应摘要和风险状态。

## 9. 数据、日志与状态持久化

### 9.1 状态持久化

至少保存：

- 当前交易锁状态。
- 当前持仓 symbol、方向、数量、入场均价、止损价、目标杠杆。
- 最近一次信号评分与拒绝原因。
- 最近订单 ID 与订单状态。
- 账户权益快照。
- 冷却期结束时间。

进程启动时必须：

1. 读取本地状态。
2. 查询 Binance 当前持仓和未完成订单。
3. 对比本地状态与交易所状态。
4. 若不一致，以交易所状态为准，并进入保守恢复流程。

### 9.2 日志要求

日志必须能回答这些问题：

- 系统为什么选择或拒绝某个标的？
- 趋势模型每个组成分数是多少？
- 下单前风险预算是多少？
- 下单参数如何由交易所规则修正？
- 当前为什么处于 `IDLE`、`IN_POSITION` 或 `COOLDOWN`？
- 如果 Binance API 报错，错误是什么、发生在哪个 endpoint？

不得记录：

- API Secret。
- 完整签名串。
- 可泄露账户安全的敏感配置。

## 10. Cursor 分阶段 Prompts 与验收

后续实现时建议按以下 prompts 顺序交给 Cursor 执行。每个阶段完成后都要求 Cursor 明确汇报“实现了什么”和“如何验收”。

### Prompt 1：项目骨架与环境约束

```text
请在当前项目中实现滚仓交易系统的 Python 项目骨架。要求：
1. 不实现真实交易逻辑，只创建最小模块结构、配置样例、README 使用骨架和基础测试框架。
2. 所有终端命令执行前必须先执行 conda activate roll-env。
3. 系统目标是 Binance COIN-M Futures，默认 Testnet。
4. 不创建前端，不做双端分离。
5. 完成后告诉我：创建了哪些文件、每个模块职责是什么、如何运行测试验收。
```

验收：

- 执行 `conda activate roll-env` 后可以运行测试命令。
- 项目结构清晰，尚未接入真实下单。
- README 明确说明默认 Testnet 和安全边界。

### Prompt 2：Binance COIN-M Public API 客户端

```text
请实现 Binance COIN-M Futures Public API 客户端，默认连接 Testnet。
要求：
1. 支持 ping、time、exchangeInfo、klines、ticker price。
2. 实现服务器时间偏移计算。
3. 从 exchangeInfo 动态解析 symbol、交易状态、精度、最小下单量和合约类型。
4. 实现候选资产到 COIN-M symbol 的筛选逻辑，至少要求得到 3 个可监测标的，否则给出清晰错误。
5. 所有终端命令执行前必须先执行 conda activate roll-env。
6. 完成后告诉我：实现了哪些 API、如何用 Binance Testnet 验收、哪些候选标的可用。
```

验收：

- Testnet `ping`、`time` 成功。
- `exchangeInfo` 能列出可交易 COIN-M 合约。
- 候选池能输出至少 3 个可监测 symbol，或明确说明不足 3 个的原因。
- K 线拉取能返回 `15m`、`1h`、`4h` 数据。

### Prompt 3：趋势模型与可解释信号

```text
请实现趋势行情数学模型，这是本策略最重要的部分。
要求：
1. 使用多时间框架趋势评分：15m、1h、4h。
2. 至少包含对数价格回归斜率、斜率显著性或 R²、ADX/+DI/-DI、EMA 排列、Donchian 突破、震荡过滤、成交量确认。
3. 输出 long、short、no_trade 三类结果。
4. 每个结果必须包含可解释原因，包括每个因子的分数和拒绝原因。
5. 先使用历史 K 线数据离线计算，不要下单。
6. 所有终端命令执行前必须先执行 conda activate roll-env。
7. 完成后告诉我：模型公式、参数默认值、如何用历史样本验收趋势识别效果。
```

验收：

- 对每个候选 symbol 输出趋势评分报告。
- 明显震荡行情输出 `no_trade`。
- 明显单边行情输出方向一致信号。
- 测试覆盖核心指标计算和评分边界。

### Prompt 4：风险模型、Kelly 仓位与止损

```text
请实现风险管理模块。
要求：
1. 实现 Kelly、半 Kelly、四分之一 Kelly 风险预算。
2. 根据账户权益、止损距离、最大单笔亏损、最大仓位比例计算下单数量。
3. 实现固定止损、ATR 止损、追踪止损的计算逻辑。
4. 实现账户级最大回撤、日内亏损、连续亏损、冷却期熔断。
5. 不接入真实下单，只做纯计算和测试。
6. 所有终端命令执行前必须先执行 conda activate roll-env。
7. 完成后告诉我：每种风控规则如何生效、如何验收不会全仓或超风险开仓。
```

验收：

- Kelly 为负时不得开仓。
- 下单数量由最大可亏损反推。
- 任意情况下不超过最大仓位比例。
- 触发回撤、连续亏损或冷却时输出禁止交易原因。

### Prompt 5：Signed API、账户与下单闭环

```text
请实现 Binance COIN-M Futures Signed API 客户端，并只在 Testnet 中验收。
要求：
1. 支持账户信息、持仓查询、未完成订单查询、设置杠杆、创建订单、查询订单、撤单、平仓。
2. 正确实现 HMAC SHA256 签名和 timestamp/recvWindow。
3. 禁止记录 API Secret。
4. 下单验收只使用 Testnet 和最小可交易数量。
5. 必须完成开仓 -> 查询持仓 -> 平仓 -> 确认无持仓的闭环。
6. 所有终端命令执行前必须先执行 conda activate roll-env。
7. 完成后告诉我：每个 signed endpoint 的验收结果、下单和平仓闭环是否成功。
```

验收：

- Testnet API key 能成功访问账户。
- 能设置目标 symbol 杠杆。
- 能创建最小测试订单。
- 能查询订单与持仓。
- 能平仓并确认无持仓。

### Prompt 6：交易锁与持仓管理

```text
请实现全局单标的交易锁和持仓管理。
要求：
1. 状态包括 IDLE、ENTERING、IN_POSITION、EXITING、COOLDOWN。
2. 任意时刻最多允许一个 symbol 处于交易或持仓状态。
3. 启动时必须查询 Binance Testnet 当前持仓和未完成订单，并以交易所状态为准恢复。
4. 如果发现多个 symbol 已有持仓，系统必须停止自动交易并要求人工处理。
5. 所有终端命令执行前必须先执行 conda activate roll-env。
6. 完成后告诉我：如何证明系统不会同时交易两个标的、崩溃恢复如何验收。
```

验收：

- 模拟多个候选标的同时出信号时，只选择一个进入交易。
- 持仓期间其他标的信号只记录，不下单。
- 重启后能从 Binance 持仓恢复状态。
- 异常多持仓时进入安全停止。

### Prompt 7：策略主循环与 dry-run

```text
请实现策略主循环，先支持 dry-run，不进行真实下单。
要求：
1. 循环扫描至少 3 个候选标的。
2. 拉取行情，计算趋势评分，应用风控，选择最优单一标的。
3. dry-run 模式只打印将要执行的动作、仓位、止损、杠杆和拒绝原因。
4. 不需要用户确认，但 dry-run 不允许真实下单。
5. 所有终端命令执行前必须先执行 conda activate roll-env。
6. 完成后告诉我：循环如何运行、日志怎么看、如何验收单标的选择逻辑。
```

验收：

- dry-run 可连续运行多个扫描周期。
- 每轮输出候选标的评分排序。
- 只会选择一个标的作为潜在交易对象。
- 没有信号时明确输出 `no_trade` 原因。

### Prompt 8：Testnet 自动交易闭环

```text
请把策略主循环接入 Binance COIN-M Futures Testnet 自动交易。
要求：
1. 只允许 Testnet。
2. 信号触发后自动设置杠杆、计算仓位、下单、设置或管理止损、监控持仓、平仓。
3. 严格遵守单标的交易锁。
4. 交易全过程不需要用户确认。
5. 必须支持异常时停止开新仓，并尽可能保护已有持仓。
6. 所有终端命令执行前必须先执行 conda activate roll-env。
7. 完成后告诉我：自动交易闭环实现了什么、如何在 Testnet 完整验收。
```

验收：

- Testnet 完成一次自动开仓和平仓。
- 持仓期间不会开第二个标的。
- 止损或退出条件能触发平仓。
- 异常日志可追溯。

### Prompt 9：回测与参数校准

```text
请实现历史回测和参数校准工具。
要求：
1. 使用 Binance K 线数据回测趋势模型、滚仓、止损、手续费和滑点。
2. 输出胜率、盈亏比、最大回撤、年化收益、Sharpe、单标的表现和总体表现。
3. 对趋势阈值、ADX、止损距离、Kelly multiplier、最大仓位比例做敏感性分析。
4. 回测结果用于估计 Kelly 参数 p 和 b。
5. 所有终端命令执行前必须先执行 conda activate roll-env。
6. 完成后告诉我：回测覆盖哪些标的、哪些参数最敏感、哪些参数建议用于 Testnet。
```

验收：

- 至少 3 个标的完成回测。
- 输出 p、b 和 Kelly 建议风险比例。
- 能识别震荡期回撤风险。
- 参数报告能解释为什么选择默认阈值。

### Prompt 10：使用文档、实盘前检查与安全开关

```text
请完善系统使用文档和安全开关。
要求：
1. 写清楚 conda activate roll-env、配置 Testnet API key、启动 dry-run、启动 Testnet 自动交易的方法。
2. 写清楚如何停止系统、如何确认无持仓、如何手动平仓。
3. 写清楚从 Testnet 切换到 live 前必须完成的检查清单。
4. 默认不得启用 live trading，live 必须由显式配置开启。
5. 所有终端命令执行前必须先执行 conda activate roll-env。
6. 完成后告诉我：最终用户如何使用系统、哪些安全开关已经具备。
```

验收：

- 新用户可按 README 完成 dry-run。
- Testnet 自动交易步骤完整。
- live trading 默认关闭（配置项 `strategy.live_trading_enabled` 默认为 false；程序当前亦不接实盘 signed `run-loop`）。
- 文档明确风险和手动应急流程。

## 11. 系统使用方法

下文命令均以仓库根目录为当前目录。**每一条终端命令在执行前都必须先激活 Conda 环境**（Windows / Linux / macOS 相同）：

```bash
conda activate roll-env
```

随后在同一会话中执行 `python -m main …`。不要将 API Secret 写入仓库或 YAML；使用环境变量（见下）。

### 11.1 环境与配置文件

1. **Conda 环境**：确保已创建并激活 `roll-env`（与上文一致）。
2. **配置文件**：复制示例并编辑：

```bash
conda activate roll-env
copy config\settings.example.yaml config\settings.yaml
```

（Linux/macOS 使用 `cp config/settings.example.yaml config/settings.yaml`。）

3. **候选标的与策略参数**：在 `config/settings.yaml` 中维护 `candidates`、`strategy`（如 `loop_interval_sec`、`initial_leverage`、`stop_adverse_fraction`、`kelly_p` / `kelly_b` 等）。具体字段以 `config/settings.example.yaml` 为准。

### 11.2 配置 Binance Futures Testnet API Key

1. 在 Binance **Futures Testnet** 控制台创建 **COIN-M Futures** 可用的 API Key（权限遵循最小化原则，**不要**开启提现）。
2. 在**当前终端会话**中设置环境变量（Secret 勿入库）。

**PowerShell（Windows）示例：**

```powershell
conda activate roll-env
$env:BINANCE_API_KEY = "你的_testnet_key"
$env:BINANCE_API_SECRET = "你的_testnet_secret"
```

**Bash 示例：**

```bash
conda activate roll-env
export BINANCE_API_KEY="你的_testnet_key"
export BINANCE_API_SECRET="你的_testnet_secret"
```

程序读取 **`BINANCE_API_KEY`** 与 **`BINANCE_API_SECRET`**。不要求设置 `BINANCE_ENV` / `BINANCE_MARKET`（若本地有其他脚本依赖可自行保留，本入口以 `settings.yaml` 的 `binance.rest_base` 与 `environment` 为准）。

### 11.3 安全开关（默认不开真实/Testnet 挂单）

与自动挂单相关的默认策略如下：

| 模式 | 默认行为 | 显式开启方式 |
| --- | --- | --- |
| dry-run | **默认**：只拉行情、打印决策，**不发 signed 单** | 使用 `python -m main run-loop`（不要加 `--no-dry-run`） |
| Testnet signed 自动交易 | **默认关闭**：即使加了 `--no-dry-run` 也会被拒绝 | `settings.yaml` 中 `strategy.testnet_signed_orders_enabled: true`，且 CLI 使用 `--no-dry-run`，且 REST 为官方 Testnet host |
| 实盘（live）signed 自动交易 | **默认关闭且当前未实现**：`run-loop --no-dry-run` **仅允许** Testnet host | `strategy.live_trading_enabled` 预留为 **false**；将来接入实盘时必须仍为显式 true + 代码侧校验；**当前版本请勿将实盘 Key 用于本程序的 signed 循环** |

### 11.4 启动 dry-run（推荐第一步）

dry-run **不会**向交易所下 signed 单，用于验证候选标的、趋势评分与风控日志。

**单轮验收（跑一轮即退出）：**

```bash
conda activate roll-env
python -m main run-loop --once
```

**持续循环：**

```bash
conda activate roll-env
python -m main run-loop
```

说明：

- `binance.rest_base` 仍为 Testnet 时，dry-run 可通过 `strategy.public_rest_base` 指向实盘公共 REST（例如 `https://dapi.binance.com`）仅用于 **exchangeInfo / K 线**，这在不下单的前提下常用于 Testnet 合约覆盖不足时的监测（详见示例配置注释）。

### 11.5 启动 Testnet 自动交易（signed 闭环）

仅在已完成 dry-run、并理解会真实发出 **Testnet** 委托时使用。

**硬性条件（缺一不可）：**

1. `conda activate roll-env`
2. `config/settings.yaml` 中 `environment: testnet`（或未写该项，默认按 testnet 处理 signed 路径）
3. `binance.rest_base` 为官方 Futures Testnet：`https://testnet.binancefuture.com`
4. `strategy.testnet_signed_orders_enabled: true`（显式打开 Testnet 挂单开关）
5. 已设置 `BINANCE_API_KEY` / `BINANCE_API_SECRET`（Testnet）
6. **若配置了 `strategy.public_rest_base`**：自动交易时其值必须与 `binance.rest_base` **完全一致**，否则程序会拒绝启动；通常做法是：**删除或注释** `public_rest_base`，让读写行情与下单同属 Testnet。
7. （强烈推荐）首次启动前先对账持仓与挂单：

```bash
conda activate roll-env
python -m main reconcile-state
```

**单轮自动迭代：**

```bash
conda activate roll-env
python -m main run-loop --once --no-dry-run
```

**持续自动循环：**

```bash
conda activate roll-env
python -m main run-loop --no-dry-run
```

其他常用子命令（均需先 `conda activate roll-env`）：`coinm-signed-smoke`（Signed 冒烟）、`trend-offline`（仅公有 K 线）、`backtest`（回测）。详见 `README.md`。

### 11.6 停止系统

| 场景 | 做法 |
| --- | --- |
| 停止策略进程 | 在运行 `run-loop` 的终端按 **Ctrl+C**（发送中断）。当前实现不会在该信号下自动市价平掉全部持仓；若不希望留下裸露头寸，应先切换为不下单模式或事先手动平仓（见下）。 |
| 阻止开新仓但保留已有持仓管理 | 异常时程序可能进入 **pause / halt**；详见运行日志与 `data/roll_state.json`。可使用 `python -m main run-loop --no-dry-run --clear-entry-pause` **仅清除 persisted 的「暂停开仓」标记**（不写交易所 API）；是否解除 halt 取决于对账与持仓状态。 |
| 启动前希望刷新本地与交易所对齐的状态 | `python -m main reconcile-state`（**仅允许** Testnet host；见命令说明）。 |

停止后务必执行下一节的「确认无持仓」。

### 11.7 如何确认无持仓、无挂单

1. **命令行对账（Testnet，推荐）**

```bash
conda activate roll-env
python -m main reconcile-state
```

关注输出中的：

- `nonzero_position_symbols=` → 应为空列表 `[]` 表示 **无持仓**
- `symbols_with_open_orders=` → 应为 `[]` 表示 **无未完成委托**（或有挂单时需手工处理）

若 `halt=true`，说明交易所快照触发安全停止（如多标的持仓、跨品种挂单等），需按打印的 `halt_reason` 人工处理后再重启策略。

2. **交易所网页**：登录 Binance **Futures Testnet**，在 **COIN-M** 仓位与当前委托页面目视确认仓位为 0、无挂单。

### 11.8 如何手动平仓 / 撤单

本仓库**不提供**单独的「一键市价平仓」CLI；应急时请使用交易所侧能力：

1. 登录 **Binance Futures Testnet** → **COIN-M** 合约。
2. **撤销**该标的下所有未成交委托（Stop / Limit 等），避免与手动平仓单冲突。
3. 在持仓面板对目标合约执行 **市价平仓**（或等价 Close Position）。
4. 回到终端再次运行：

```bash
conda activate roll-env
python -m main reconcile-state
```

确认 `nonzero_position_symbols=[]`。

实盘环境下的手动平仓流程相同（使用实盘站点与账户），但 **当前版本的 `run-loop --no-dry-run` 不支持实盘 REST**，实盘操作仅限交易所界面或你另行编写的工具。

### 11.9 从 Testnet 切换到 live 之前（必读）

**当前代码状态**：`python -m main run-loop --no-dry-run` **仅在** `binance.rest_base` 为官方 Testnet 时可下单；**切换到实盘 REST 并不会自动启用实盘自动交易**。将来若加入实盘 signed 循环，也必须同时满足配置开关（见 11.3）与代码审查。

在修改任何实盘参数之前，建议完成 **第 12 节检查清单**；以下为最小摘要：

- Testnet 上 dry-run / signed 闭环均已跑通；理解日志与 `reconcile-state` 含义。
- API Key **禁用提现**；密钥轮换与泄漏预案已准备好。
- 明确 COIN-M **实盘**合约规则与 Testnet 差异（精度、最小名义价值、杠杆上限等）。
- 已书面记下「Ctrl+C 停止后如何手动平仓」步骤（11.8）。
- `strategy.live_trading_enabled` 在未得到明确风控批准前保持 **false**。

## 12. 实盘（live）切换检查清单

在进入实盘或接入任何真实资金自动化之前，逐项确认（未完成则 **不得** 视为可上线）：

- [ ] **程序能力**：已确认当前仓库版本下，实盘 signed `run-loop` 是否已实施；若未实施，**仅能**使用交易所手动下单或使用经审计的其他工具。
- [ ] **配置**：`strategy.live_trading_enabled` 仅在书面批准后为 **true**；默认保持 **false**。
- [ ] **REST / 环境**：实盘 `binance.rest_base` 与密钥环境必须与 Testnet **彻底分离**，禁止混用 Key。
- [ ] **API Key**：最小权限；无提现；IP 白名单（若可用）；独立於 Testnet 的 Key。
- [ ] **密钥与日志**：日志与 issue 附件中**绝不**包含 Secret；`.env` 已加入 `.gitignore`。
- [ ] **交易锁**：Testnet 已验证单标的锁、多标的 / 异常挂单时 **halt** 行为符合预期。
- [ ] **恢复演练**：进程被杀、网络中断后重启，对账结果与持仓一致。
- [ ] **精度与规则**：实盘 `exchangeInfo` 下最小下单量、步长、名义价值已实测。
- [ ] **风控**：止损、追踪止损、回撤 / 日内熔断（若启用）在 Testnet 或仿真环境验收。
- [ ] **限频与容错**：API 429 / 维护时段的行为可接受，有人工介入预案。
- [ ] **应急**：已排练 **停止进程 → 网页撤单 → 市价平仓 → reconcile-state / 网页复查** 全流程。

## 13. 风险提示与非目标

### 13.1 风险提示

本系统涉及高杠杆合约交易。即使趋势模型、Kelly 仓位和止损全部实现，也无法消除以下风险：

- 极端行情跳空或插针导致止损成交价格远差于预期。
- Binance API 延迟、限频、维护或异常返回。
- Testnet 与实盘成交环境差异。
- COIN-M 合约流动性不足。
- 模型在历史有效但未来失效。
- 高杠杆下小幅逆向波动导致严重亏损。

因此，实盘应从极小资金开始，并默认关闭自动实盘交易。

### 13.2 非目标

本系统不做：

- 前端页面。
- 移动端或 Web 后台。
- 多交易所套利。
- 同时持有多个标的。
- 高频做市。
- 网格策略。
- 无限制马丁格尔加仓。
- 绕过 Binance 风控或限频。

## 14. 最终完成标准

当后续实现完成后，系统应满足：

- 可以在 `roll-env` 中运行。
- 可以连接 Binance COIN-M Futures Testnet。
- 可以动态发现至少 3 个可监测标的。
- 可以用数学趋势模型识别强单边行情并解释信号。
- 可以在任意时刻只交易一个标的。
- 可以完成 Testnet 自动开仓、持仓管理和平仓闭环。
- 可以通过风控限制仓位、止损、回撤和异常状态。
- 可以按使用文档完成 dry-run、Testnet 自动交易和实盘前检查。
