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

2.0 将 **Testnet** 与 **live（实盘）** 的配置、密钥与状态文件严格分离。请从示例复制为本地文件（**勿提交 Git**）：

```bash
conda activate roll-env
# Testnet
cp config/settings.testnet.example.yaml config/settings.testnet.yaml
cp config/secrets/testnet.env.example config/secrets/testnet.env

# live（仅在你需要准备实盘环境时复制；默认不启用自动交易）
cp config/settings.live.example.yaml config/settings.live.yaml
cp config/secrets/live.env.example config/secrets/live.env
```

Windows PowerShell：

```powershell
conda activate roll-env
Copy-Item config\settings.testnet.example.yaml config\settings.testnet.yaml
Copy-Item config\secrets\testnet.env.example config\secrets\testnet.env
Copy-Item config\settings.live.example.yaml config\settings.live.yaml
Copy-Item config\secrets\live.env.example config\secrets\live.env
```

| 环境 | 示例配置 | 本地配置 | 密钥文件 | 状态 JSON |
| --- | --- | --- | --- | --- |
| Testnet | `config/settings.testnet.example.yaml` | `config/settings.testnet.yaml` | `config/secrets/testnet.env` | `data/roll_state_testnet.json` |
| live | `config/settings.live.example.yaml` | `config/settings.live.yaml` | `config/secrets/live.env` | `data/roll_state_live.json` |

编辑本地 YAML：`candidates`、`strategy` 等。**勿将 API Secret 写入 YAML。**

仍可使用单一 `config/settings.yaml`（从 `config/settings.example.yaml` 复制），但新部署建议采用上表拆分方式。通用示例：

```bash
conda activate roll-env
cp config/settings.example.yaml config/settings.yaml
```

命令行通过 `--config` 指定环境，例如 `--config config/settings.testnet.yaml`。

### 隔离要点（防混用）

- Testnet 的 `binance.rest_base` 必须为 `https://testnet.binancefuture.com`；live 必须为 `https://dapi.binance.com`。
- `secrets.file` 与 `--secrets-file` 必须与环境一致（Testnet 用 `testnet.env`，live 用 `live.env`）。
- `state.path` 必须指向各自 JSON（`roll_state_testnet.json` / `roll_state_live.json`）。
- `strategy.live_trading_enabled` 在示例中**默认为 false**；仅在你完整审查配置、密钥权限与对账结果后，才可在 live 配置中改为 `true`。

## Binance API 密钥（本地文件，推荐）

在 Binance **Futures Testnet** 创建 COIN-M 可用的 Key（**不要**开启提现）。实盘 Key 须单独创建、**禁止提现**，并建议 IP 白名单。

```bash
conda activate roll-env
mkdir -p config/secrets
cp config/secrets/testnet.env.example config/secrets/testnet.env
# 编辑 testnet.env：BINANCE_API_KEY=...  BINANCE_API_SECRET=...
chmod 600 config/secrets/testnet.env   # Linux/macOS
```

`config/secrets/` 已加入 `.gitignore`；仅 `*.example` 可提交。

读取优先级：**`--secrets-file`** → 配置 **`secrets.file`** → 进程环境变量 `BINANCE_*`（兼容 fallback）。

详见 `config/env.example` 与 `config/secrets/*.env.example`。

## 安全开关（默认不下单）

| 行为 | 说明 |
| --- | --- |
| **dry-run（默认）** | `python -m main run-loop --config config/settings.testnet.yaml`：只拉行情、打印决策，**不发 signed 单**。 |
| **Testnet 真实挂单** | 需 **CLI** `--no-dry-run` **且** `strategy.testnet_signed_orders_enabled: true` **且** Testnet `rest_base` **且** Testnet 密钥/配置。 |
| **实盘自动交易（live）** | 须 **`environment: live`**、`rest_base=https://dapi.binance.com`、**`live_trading_enabled: true`**、CLI **`--no-dry-run`**、live 专用 **`secrets`/`state.path`**；启动时自动对账；**同一账户仅允许一个 live 进程**（见下文）。 |

自动交易若配置了 `strategy.public_rest_base`，则其必须与 `binance.rest_base` 相同；dry-run 可用实盘公共 REST 仅读行情时参见 Testnet 示例 YAML 注释。

## 常用命令（均需先 `conda activate roll-env`）

**Dry-run（Testnet 配置）：**

```bash
conda activate roll-env
python -m main run-loop --config config/settings.testnet.yaml --once
```

**Testnet signed 主循环（先对账）：**

```bash
conda activate roll-env
python -m main reconcile-state --config config/settings.testnet.yaml --secrets-file config/secrets/testnet.env
python -m main run-loop --config config/settings.testnet.yaml --secrets-file config/secrets/testnet.env --no-dry-run
```

（须在 `settings.testnet.yaml` 将 `testnet_signed_orders_enabled` 设为 `true`，且 Testnet 自动交易时不要使用与 Testnet 不一致的 `public_rest_base`。）

**live 对账（不下单，使用 live 配置与密钥）：**

```bash
conda activate roll-env
python -m main reconcile-state --config config/settings.live.yaml --secrets-file config/secrets/live.env
```

**live signed 单轮验收（极小资金；须已将对账通过且 `live_trading_enabled: true`）：**

```bash
conda activate roll-env
python -m main reconcile-state --config config/settings.live.yaml --secrets-file config/secrets/live.env
python -m main run-loop --config config/settings.live.yaml --secrets-file config/secrets/live.env --once --no-dry-run
```

**live signed 持续运行：** 将上式去掉 `--once`；**不要**同时再开一个前台 `run-loop --no-dry-run` 或第二个 `roll-live.service`——程序会在 `data/roll_state_live.json.lock` 上互斥，第二个进程会拒绝启动。

**停止与确认无持仓：** 在运行循环的终端 **Ctrl+C**；然后对当前环境执行 `reconcile-state`（带上对应的 `--config` 与 `--secrets-file`），检查 `nonzero_position_symbols=[]`。手动平仓请在对应 Binance **COIN-M** 环境（Testnet 或实盘）网页撤单并市价平仓。

**其他：** `python -m main trend-offline --symbol DOGEUSD_PERP`、`python -m main backtest --days 180`、`python -m main coinm-signed-smoke --symbol DOGEUSD_PERP`。

## 验收：不会混用状态与密钥

1. 打开 `config/settings.testnet.yaml` 与 `config/settings.live.yaml`，确认 `secrets.file` 与 `state.path` 两两不同。
2. 分别运行（仅对账、不下单）：
   ```bash
   conda activate roll-env
   python -m main reconcile-state --config config/settings.testnet.yaml --secrets-file config/secrets/testnet.env
   python -m main reconcile-state --config config/settings.live.yaml --secrets-file config/secrets/live.env
   ```
3. 确认生成/更新的是 `data/roll_state_testnet.json` 与 `data/roll_state_live.json`（两个文件，互不覆盖）。
4. 故意交叉（例如在 Testnet 命令加 `--secrets-file config/secrets/live.env`）时，应对账到错误环境或鉴权失败——**不要**在生产中这样运行；正常运维应始终让 `--config`、`secrets.file` 与 `--secrets-file` 同属一个环境。

## Ubuntu 云服务器：systemd 托管

单元文件模板在仓库 **`deploy/systemd/`**，安装到服务器后为 **`/etc/systemd/system/roll-testnet.service`** 与 **`/etc/systemd/system/roll-live.service`**。详细安装与路径说明见 [`deploy/systemd/README.md`](deploy/systemd/README.md)。

服务在**项目根目录**（`WorkingDirectory`，默认 `/opt/roll`）运行，使用 **`roll-env` 中的 Python**（`…/envs/roll-env/bin/python`，等价于 `conda activate roll-env`），并通过 **`EnvironmentFile`** 与 **`--secrets-file`** 加载对应密钥，通过 **`--config`** 使用对应环境的 YAML。

### 安装（首次）

```bash
conda activate roll-env
cd /opt/roll
pip install -e ".[dev]"
# 编辑 deploy/systemd/*.service 中的 User、WorkingDirectory、Python 路径后：
sudo cp deploy/systemd/roll-testnet.service /etc/systemd/system/
sudo cp deploy/systemd/roll-live.service /etc/systemd/system/
sudo systemctl daemon-reload
```

**live 默认不要开机自启**：安装后只用 `start`；除非你明确接受重启后自动恢复实盘进程，否则**不要**执行 `sudo systemctl enable roll-live`。

### 启动前对账（必做）

```bash
conda activate roll-env
cd /opt/roll
# Testnet
python -m main reconcile-state --config config/settings.testnet.yaml --secrets-file config/secrets/testnet.env
# live（满足 live 安全闸门后再执行）
python -m main reconcile-state --config config/settings.live.yaml --secrets-file config/secrets/live.env
```

### Testnet：启动 / 停止 / 重启 / 状态 / 日志

```bash
sudo systemctl start roll-testnet
sudo systemctl stop roll-testnet
sudo systemctl restart roll-testnet
sudo systemctl status roll-testnet
journalctl -u roll-testnet -n 200 --no-pager
journalctl -u roll-testnet -f
```

可选开机自启：`sudo systemctl enable roll-testnet`

### live：启动 / 停止 / 重启 / 状态 / 日志

```bash
sudo systemctl start roll-live
sudo systemctl stop roll-live
sudo systemctl restart roll-live
sudo systemctl status roll-live
journalctl -u roll-live -n 200 --no-pager
journalctl -u roll-live -f
```

**不要**与前台 `run-loop --config config/settings.live.yaml --no-dry-run` 同时运行。停止后应对账确认持仓（见上文「停止与确认无持仓」）。

### 禁用开机自启

```bash
sudo systemctl disable roll-testnet
sudo systemctl disable roll-live
```

## Live 上线前验收（2.0 Prompt 8）

在启用实盘 signed 自动交易或 `roll-live.service` 之前，按顺序完成 Testnet 闭环、live dry-run（≥24h）、live 对账、极小资金单轮 `--once` 与人工记录：

- **流程与命令**：[`docs/live-go-live-acceptance.md`](docs/live-go-live-acceptance.md)
- **可打印清单**：[`docs/checklists/live-go-live-checklist.md`](docs/checklists/live-go-live-checklist.md)
- **自动化脚本**（Linux/WSL）：[`scripts/acceptance/README.md`](scripts/acceptance/README.md)
- **试运行保守参数参考**：[`config/settings.live.minimal-funds.example.yaml`](config/settings.live.minimal-funds.example.yaml)
- **记录模板**：[`docs/templates/live-acceptance-record.template.md`](docs/templates/live-acceptance-record.template.md)

```bash
conda activate roll-env
cd /opt/roll
bash scripts/acceptance/preflight.sh
```

## 文档

- 1.0 设计与操作：`docs/滚仓系统实现的plan文档.md` §11–§12。
- 2.0 实盘与部署计划：`docs/滚仓系统实现的plan文档2.0版本.md`。
- **Live 最终验收**：`docs/live-go-live-acceptance.md`。
- systemd 安装细节：`deploy/systemd/README.md`。

## 测试

```bash
conda activate roll-env
pytest
```
