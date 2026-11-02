# 远程部署：WireGuard + VPS（只用手机）

让 iPhone 在**不连 Mac、不插 USB**的情况下执行 SimLocation：手机装官方 WireGuard
连到一台公网 VPS，VPS 上跑 pymobiledevice3 的 tunneld 适配器建立开发者隧道，再通过
只在 WireGuard 内监听的网页控制台操作。手机用 Safari 打开网页即可设定位 / 清除。

本目录是这套部署的代码、systemd 单元、配置模板、运维说明与调查记录。**凭据不入库**
（WireGuard 私钥、配对记录、访问令牌、auth key 只在 VPS 上），仓库里只放脱敏模板。

## 先读：结论与边界

- **纯蜂窝从零新建开发者通道：不可行。** iOS 配对服务在当前版本禁止通过 cellular /
  loopback 路径建立新连接（设备日志 `prohibited types: cellular loopback`）。凡是从
  远端主机连进手机的方案（VPS+WireGuard、Surge tailnet、官方 Tailscale）纯蜂窝下都
  建立不了新通道。
- **可行形态：任意 WiFi 下建立通道 → 切蜂窝后保持。** 保持期间 WireGuard 必须是手机
  当前的活动 VPN（iOS 同时只能一个 VPN，Surge 会被停用）。一次约 11 分钟锁屏保持测试
  通过；更长时间、弱信号、重启后的表现尚未实测。
- 完整证据与时间线见 `notes/`：
  - `cellular-pairing-diagnosis.md` — 纯蜂窝重连诊断（脱敏日志）
  - `wireguard-setup-notes.md` — 部署状态、分条件验证、keepalive 与保持测试、回滚
  - `surge-verification.md` — 为什么 Surge tailnet 入站走不通的对照

## 架构

```
iPhone (官方 WireGuard, 真 utun)  ──UDP 51820──►  VPS wg-simloc 10.0.0.1/30
       10.77.77.2                                        10.77.77.1
                                                            │
                                   network_tunneld.py（固定地址 tunneld 适配器）
                                   连 10.77.77.2:49152 建 RSD 信任隧道
                                            │
                                   RSD → DVT → LocationSimulation
                                            │
                                   simlocation web（仅 10.77.77.1:8765 监听）
                                   手机 Safari 打开，设/清定位
```

- 适配器复用 pymobiledevice3 原有 tunneld 的 HTTP API 与隧道生命周期，只把 Bonjour
  发现替换成固定的手机 WireGuard 地址，从不发起新配对。
- tunneld HTTP API 仅 `127.0.0.1:49151`；网页仅 `10.77.77.1:8765`（WireGuard 内）。

## 目录

| 路径 | 作用 |
|---|---|
| `network_tunneld.py` | tunneld 适配器：按固定地址连手机，维持 RSD 隧道；可选 `keepalive` 放宽断连判定 |
| `verify_device.py` | 只读验证：打开 RSD/DVT/LocationSimulation 通道，不发坐标 |
| `hold_probe.py` | 保持测试：在现有隧道上保持一组会话，SIGUSR1 做只读检查，被动记录 WireGuard 活动 |
| `test_network_tunneld.py` / `test_hold_probe.py` | 单元测试（含 Linux socket 选项、端点脱敏） |
| `systemd/` | `simlocation-network.service`（适配器）、`simlocation-web.service`（网页） |
| `examples/network.json.example` | 适配器配置模板（占位 UDID） |
| `setup/` | 一次性搭建脚本，**含环境相关路径/地址**，复用前按下文与脚本内容调整 |
| `diagnostics/` | 调查期的只读脚本（Surge 探测、IPv6 对照、配对网络诊断、WireGuard 验证等） |
| `notes/` | 过程与结论记录 |

## 前置

- 一台公网 VPS（本部署为 Ubuntu）。
- VPS 上独立 venv，`pymobiledevice3==11.49.2`（`setup/bootstrap_venv.py`、
  `setup/install_pmd3.py`）。适配器会检查版本，不匹配即拒绝启动。
- 该手机已有的 RemotePairing 记录（配对私钥），经用户同意迁入 VPS
  （`setup/provision_identity.py`）。该脚本**不在仓库留存任何密钥**，运行时从本机
  `~/.pymobiledevice3/` 读取并经 SSH 传入 VPS 私有目录。

## 部署步骤（在 VPS 上，root）

1. **venv + pymobiledevice3**：`setup/bootstrap_venv.py`、`setup/install_pmd3.py`
   建 `/opt/simlocation-venv`，固定 11.49.2，依赖锁定到 `/etc/simlocation/requirements.lock`。
2. **WireGuard**：先 `export SIMLOCATION_WG_ENDPOINT=<你的公网IP>:51820`，再运行
   `setup/setup_wireguard.py`。它只新建 `wg-simloc` 接口、挑一个不冲突的 `10.0.0.1/30`
   子网、生成密钥、写服务器与手机两份配置，不改默认路由 / DNS / NAT。手机配置
   （含私钥）与二维码交给手机导入，**不要提交或转发**。
3. **迁入配对记录**：`setup/provision_identity.py`（按你的 SSH 别名与本机路径调整脚本
   顶部）。它把配对记录写入 VPS `/var/lib/simlocation-tunnel/pairing/device.plist`
   (root, 600)，保留原主机标识，并建立非特权 `simlocation` 用户、`devices.json`、
   `web.env`（随机令牌）。
4. **放代码**：把本目录的 `network_tunneld.py` `verify_device.py` `hold_probe.py`
   及 SimLocation 主程序放到 `/opt/simlocation/`（主程序在 `bin/`，这些适配器在
   `deploy/`）。写 `/etc/simlocation/network.json`（见 `examples/`）。
5. **systemd**：装 `systemd/*.service` 到 `/etc/systemd/system/`，`daemon-reload`，
   先 `wg-quick@wg-simloc`，再 `simlocation-network`、`simlocation-web`。开机自启确认
   重连稳定后再 `enable`。
6. **UFW**：只放行手机在 `wg-simloc` 上访问 `10.77.77.1:8765/tcp`。

## 配置

### `/etc/simlocation/network.json`（模板见 `examples/network.json.example`）

| 字段 | 说明 |
|---|---|
| `udid` | 目标设备 UDID |
| `host` | 手机 WireGuard 地址，`10.77.77.2` |
| `port` | RemotePairing 端口，默认 `49152` |
| `pair_record` | 配对记录绝对路径，必须 600 |
| `keepalive` | 可选；`{idle, interval, count}` 秒。容忍对端静默约 `idle + interval*count` 秒才断开，并设同长 `TCP_USER_TIMEOUT`。蜂窝建议 `30/30/40`（约 20.5 分钟）。不设则沿用 pymobiledevice3 默认（约 12 秒，蜂窝上过敏感） |

### `/etc/simlocation/web.env`

- `SIMLOCATION_MAP_TOKEN=<令牌>` — 网页访问令牌（无令牌 403）。
- `SIMLOCATION_DEFAULT_LAT` / `SIMLOCATION_DEFAULT_LON` — 网页「设为默认位置」按钮使用的坐标。

## 运维（VPS）

```sh
systemctl status simlocation-network simlocation-web wg-quick@wg-simloc
journalctl -u simlocation-network -n 30 --no-pager
# 只读验证通道（不发坐标、不清除）
/opt/simlocation-venv/bin/python /opt/simlocation/deploy/verify_device.py
# 保持测试（蜂窝/休眠）：建起隧道后
systemd-run --unit=simloc-holdprobe --collect -p RuntimeMaxSec=4000 \
  /opt/simlocation-venv/bin/python -u /opt/simlocation/deploy/hold_probe.py --duration 3600
systemctl kill -s USR1 simloc-holdprobe    # 触发一次同连接只读检查
journalctl -u simloc-holdprobe --no-pager  # 或读 /var/lib/simlocation-tunnel/hold-test.jsonl
systemctl stop simloc-holdprobe            # 结束，不发清除或坐标
```

每次改动在 VPS 上留 `*.bak-<日期>-<标签>` 备份，回滚命令见 `notes/wireguard-setup-notes.md`。

## 手机用法（只用手机）

1. 打开官方 **WireGuard** 的 SimLocation 隧道（开着时 Surge 被系统停用，用完关掉恢复）。
   连哪个 WiFi 都行；蜂窝下只能保持已建立的通道，不能从零新建。
2. Safari 打开 `http://10.77.77.1:8765/?t=<令牌>`（令牌见 `web.env`，别转发）。
3. 「定点」页：
   - **设为默认位置** → 用 `web.env` 的默认坐标直接设定位。
   - **清除定位** → 恢复真实定位（等于 CLI 的 `simlocation --clear`）。
   - 地图选点或输入经纬度 → **设置定位**。
   设定位后后台保持会话，直到点「清除定位」。

## 安全边界

- 凭据不入库：WireGuard 私钥、配对记录、`web.env` 令牌、headscale auth key 只在 VPS；
  本目录模板为占位值。手机配置 / 二维码含私钥，令牌 URL / 二维码含令牌，均不提交、不转发。
- tunneld API 仅 `127.0.0.1`；网页仅 `wg-simloc` 的 `10.77.77.1:8765`，UFW 限手机源。
- 网页跑在非特权 `simlocation` 用户，读不到配对私钥；`/api/*` 仅接受 `X-SimLocation-Token`
  头，跨站表单 POST 无法带该头。
- `hold_probe.py` 日志对 WireGuard 端点只保留前两段 + NAT 端口，不写密钥。

## 验证

```sh
cd deploy
python3 -m unittest test_network_tunneld test_hold_probe
python3 -m py_compile network_tunneld.py hold_probe.py verify_device.py
```

> 关于机制本身：这套部署把"谁能给这台手机设定位"从"物理在场 + 同网 + USB 信任"改写成
> "持有配对记录 + 能进 WireGuard + 持有网页令牌"。便利来自把信任前移到 VPS，代价是多了
> 一处长期持有配对私钥的主机，和一个决定访问边界的令牌——它们现在是这条链路里最该被保护、
> 也最该被审计的东西。
