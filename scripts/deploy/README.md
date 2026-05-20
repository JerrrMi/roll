# 云服务器部署脚本

配合 [`docs/cloud-server-live-deployment.md`](../../docs/cloud-server-live-deployment.md) 在 Ubuntu 上从零部署 Roll。

**约定：** 脚本内调用 `python` 前会自动 `conda activate roll-env`；交互式终端仍建议先手动激活。

## 脚本

| 脚本 | 作用 |
| --- | --- |
| `bootstrap-ubuntu.sh` | Miniconda（可选）、`roll-env`、`pip install`、复制 example 配置、权限、pytest |
| `install-systemd.sh` | 按当前用户/项目路径/Python 生成并安装 systemd 单元 |

## 典型顺序

```bash
cd /opt/roll
bash scripts/deploy/bootstrap-ubuntu.sh

# 编辑 config/secrets/*.env 与 settings.*.yaml 后
bash scripts/acceptance/preflight.sh
# … 完成 live-go-live 验收阶段 1–4 …

bash scripts/deploy/install-systemd.sh --live-only
export ROLL_ALLOW_SYSTEMD_START=1
bash scripts/acceptance/phase5-live-systemd-start.sh
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `ROLL_SKIP_CONDA_INSTALL` | `1` = 不下载 Miniconda（须已安装 conda） |
| `ROLL_SKIP_PYTEST` | `1` = bootstrap 跳过 pytest |
| `ROLL_PYTHON_VERSION` | `roll-env` Python 版本，默认 `3.12` |
| `ROLL_USER` | systemd `User=`，默认当前用户 |
| `ROLL_PROJECT` | 项目根 / `WorkingDirectory`，默认脚本所在仓库根 |
| `ROLL_HOME` | 用户 home，默认从 passwd 解析 |

## install-systemd 参数

```bash
bash scripts/deploy/install-systemd.sh              # 仅 roll-testnet
bash scripts/deploy/install-systemd.sh --live       # testnet + live
bash scripts/deploy/install-systemd.sh --live-only  # 仅 roll-live
```

安装后须 **对账再 start**；live 默认 **不要** `systemctl enable roll-live`。
