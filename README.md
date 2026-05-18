# 滚仓交易系统（Binance COIN-M Futures）

Python 单体策略框架：**监测多候选标的，任一时刻最多交易一个标的**。支持 dry-run、`run-loop` Testnet signed 闭环（显式开启）、回测与离线趋势验收。

**每一条终端命令在执行前都必须先激活 Conda 环境：**

```bash
conda activate roll-env
```

## 环境与安装

```bash
conda activate roll-env
pip install -e ".[dev]"
```

## 配置文件

```bash
conda activate roll-env
copy config\settings.example.yaml config\settings.yaml
```

（Linux/macOS：`cp config/settings.example.yaml config/settings.yaml`。）

编辑 `config/settings.yaml`：`candidates`、`binance.rest_base`、`strategy` 等。勿将 API Secret 写入 YAML 或提交 Git。

## Binance Testnet API Key（环境变量）

在 Binance **Futures Testnet** 创建 COIN-M 可用的 Key（**不要**开启提现）。在当前终端会话设置：

**PowerShell：**

```powershell
conda activate roll-env
$env:BINANCE_API_KEY = "你的_testnet_key"
$env:BINANCE_API_SECRET = "你的_testnet_secret"
```

**Bash：**

```bash
conda activate roll-env
export BINANCE_API_KEY="你的_testnet_key"
export BINANCE_API_SECRET="你的_testnet_secret"
```

详见 `config/env.example`。

## 安全开关（默认不下单）

| 行为 | 说明 |
| --- | --- |
| **dry-run（默认）** | `python -m main run-loop`：只拉行情、打印决策，**不发 signed 单**。 |
| **Testnet 真实挂单** | 需 **CLI** `--no-dry-run` **且** `strategy.testnet_signed_orders_enabled: true` **且** `binance.rest_base` 为官方 Testnet **且** 已设置 `BINANCE_*`。 |
| **实盘自动交易** | `strategy.live_trading_enabled` **默认为 false**；当前 **`run-loop --no-dry-run` 仅允许 Testnet**，不接实盘 REST。 |

自动交易若配置了 `strategy.public_rest_base`，则其必须与 `binance.rest_base` 相同；dry-run 可用实盘公共 REST 仅读行情时参见示例 YAML 注释。

## 常用命令（均需先 `conda activate roll-env`）

**Dry-run（推荐先做）：**

```bash
conda activate roll-env
python -m main run-loop --once
```

**Testnet signed 主循环（先对账）：**

```bash
conda activate roll-env
python -m main reconcile-state
python -m main run-loop --no-dry-run
```

（须在 `settings.yaml` 将 `testnet_signed_orders_enabled` 设为 `true`，且 Testnet 自动交易时不要使用与 Testnet 不一致的 `public_rest_base`。）

**停止与确认无持仓：** 在运行循环的终端 **Ctrl+C**；然后：

```bash
conda activate roll-env
python -m main reconcile-state
```

检查输出中 `nonzero_position_symbols=[]`。手动平仓请在 Binance Futures Testnet **COIN-M** 网页撤单并市价平仓。

**其他：** `python -m main trend-offline --symbol DOGEUSD_PERP`、`python -m main backtest --days 180`、`python -m main coinm-signed-smoke --symbol DOGEUSD_PERP`。

## 文档

- 设计与完整操作说明（含实盘切换清单、手动平仓步骤）：`docs/滚仓系统实现的plan文档.md` §11–§12。

## 测试

```bash
conda activate roll-env
pytest
```
