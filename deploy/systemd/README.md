# Ubuntu systemd 部署（USD-M / U 本位）

策略进程由 systemd 托管，在**项目根目录**（`WorkingDirectory`）运行，使用 **roll-env** 中的 Python，并加载对应环境的密钥与配置。

**当前系统标准：** Binance **USD-M / U 本位 USDT 永续**。live 配置须为 `product: usdm`、`rest_base: https://fapi.binance.com`、`api_prefix: /fapi/v1`。Testnet 为同一 Testnet host + `/fapi/v1`。**勿**使用 COIN-M 的 `dapi.binance.com` 或 `/dapi`。

## 文件位置

| 用途 | 仓库路径 | 安装到服务器 |
| --- | --- | --- |
| Testnet 单元 | `deploy/systemd/roll-testnet.service` | `/etc/systemd/system/roll-testnet.service` |
| live 单元 | `deploy/systemd/roll-live.service` | `/etc/systemd/system/roll-live.service` |

安装后 systemd 只认 `/etc/systemd/system/` 下的单元；仓库内文件是模板，需复制并改路径。

## 安装前准备

在 SSH 会话中（**每条命令前先** `conda activate roll-env`）：

```bash
cd /opt/roll   # 或你的项目目录
conda activate roll-env
pip install -e ".[dev]"
```

确认本地配置与密钥已就绪（见项目根目录 [`README.md`](../../README.md)）：

- Testnet：`config/settings.testnet.yaml`（`product: usdm`，Testnet host + `/fapi/v1`）、`config/secrets/testnet.env`
- live：`config/settings.live.yaml`（`rest_base: https://fapi.binance.com`）、`config/secrets/live.env`（且 `live_trading_enabled: true` 等闸门已满足）

```bash
chmod 700 config/secrets
chmod 600 config/secrets/testnet.env config/secrets/live.env
```

## 自定义路径

编辑复制前的 service 文件，至少核对：

| 项 | 说明 |
| --- | --- |
| `User` / `Group` | 运行用户，如 `ubuntu` |
| `WorkingDirectory` | 项目根目录，如 `/opt/roll` |
| `EnvironmentFile` | 绝对路径到 `testnet.env` 或 `live.env` |
| `ExecStart` 中的 Python | `…/miniconda3/envs/roll-env/bin/python`（与 `conda activate roll-env` 等价） |
| `ExecStart` 中 `--config` / `--secrets-file` | 相对 `WorkingDirectory` 的路径 |

查找 roll-env 的 Python：

```bash
conda activate roll-env
which python
# 示例：/home/ubuntu/miniconda3/envs/roll-env/bin/python
```

## 安装单元

```bash
cd /opt/roll
sudo cp deploy/systemd/roll-testnet.service /etc/systemd/system/
sudo cp deploy/systemd/roll-live.service /etc/systemd/system/
sudo systemctl daemon-reload
```

**live 默认不要开机自启**：安装后只用 `start`，不要执行 `sudo systemctl enable roll-live`，除非你明确需要重启后自动拉起 live。

Testnet 若需要开机自启，可在验收通过后执行：

```bash
sudo systemctl enable roll-testnet
```

## 启动前对账（必做）

systemd **不会**代替你完成首次对账。启动 signed 服务前：

**Testnet：**

```bash
conda activate roll-env
cd /opt/roll
python -m main reconcile-state \
  --config config/settings.testnet.yaml \
  --secrets-file config/secrets/testnet.env
```

**live：**

```bash
conda activate roll-env
cd /opt/roll
python -m main reconcile-state \
  --config config/settings.live.yaml \
  --secrets-file config/secrets/live.env
```

确认对账输出无非预期持仓/挂单后再 `systemctl start`。

## 常用 systemctl 命令

以下均需 `sudo`（或对应用户有 polkit 权限）。将 `roll-testnet` 换成 `roll-live` 即适用于实盘服务。

| 操作 | 命令 |
| --- | --- |
| 启动 | `sudo systemctl start roll-testnet` |
| 停止 | `sudo systemctl stop roll-testnet` |
| 重启 | `sudo systemctl restart roll-testnet` |
| 状态 | `sudo systemctl status roll-testnet` |
| 开机自启（Testnet 可选） | `sudo systemctl enable roll-testnet` |
| 取消开机自启 | `sudo systemctl disable roll-testnet` |
| **live 开机自启（默认不做）** | 仅在你明确接受风险时：`sudo systemctl enable roll-live` |

停止服务**不会**自动平仓；停止后应对账确认 **USD-M / U 本位** 交易所状态（见根目录 [`README.md`](../../README.md) 与 Plan 3.0 §11.7）。

## 查看日志（journalctl）

| 操作 | 命令 |
| --- | --- |
| 最近 200 行 | `journalctl -u roll-testnet -n 200 --no-pager` |
| 实时跟踪 | `journalctl -u roll-testnet -f` |
| 本次启动以来 | `journalctl -u roll-testnet -b` |
| live 日志 | 将单元名改为 `roll-live` |

## 环境隔离核对

- `roll-testnet`：`settings.testnet.yaml` + `testnet.env` + `roll_state_testnet.json`（USD-M Testnet）
- `roll-live`：`settings.live.yaml` + `live.env` + `roll_state_live.json`（USD-M live / `fapi.binance.com`）
- **禁止**同时运行 `roll-live.service` 与前台 `run-loop --config settings.live.yaml --no-dry-run`
- **禁止** live 配置使用 `dapi.binance.com` 或 `/dapi`（COIN-M 已废弃）

## 卸载

```bash
sudo systemctl stop roll-testnet roll-live
sudo systemctl disable roll-testnet roll-live 2>/dev/null || true
sudo rm -f /etc/systemd/system/roll-testnet.service /etc/systemd/system/roll-live.service
sudo systemctl daemon-reload
```
