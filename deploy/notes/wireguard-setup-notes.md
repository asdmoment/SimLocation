# SimLocation VPS 部署状态

更新：2026-11-02，Asia/Singapore。

## 已完成

- vps 安装 WireGuard；接口 `wg-simloc`，UDP 51820，服务器 `10.77.77.1/30`，手机 `10.77.77.2/32`。
- 手机仅将 `10.77.77.1/32` 路由到隧道；未修改默认路由、DNS、NAT。
- 手机已成功握手。此前经 VPS 转发，Mac 上完成了配对验证、TLS-PSK、RSD、DVT、LocationSimulation 通道验证；未发送定位坐标。
- VPS 独立 Python 环境 `/opt/simlocation-venv`，pymobiledevice3 固定为 11.49.2，依赖清单 `/etc/simlocation/requirements.lock`。
- SimLocation v3.5.0部署到 `/opt/simlocation`；本地原项目未修改。
- 原项目 133 项 Python 测试、适配器 11 项测试在 VPS 上通过。
- 经用户同意，RemotePairing 记录经 SSH 传入 VPS，并保留原主机身份；私钥文件位于 `/var/lib/simlocation-tunnel/pairing/device.plist`，root 所有、权限 600。
- `simlocation-network.service` 和 `simlocation-web.service` 已启动，尚未启用开机自启。
- 隧道 API 仅监听 `127.0.0.1:49151`；网页仅监听 `10.77.77.1:8765`。UFW 新增规则只允许手机在 `wg-simloc` 上访问网页。
- 网页运行于独立非特权账户，读不到配对私钥；无令牌返回 403，有令牌返回 200。访问日志关闭以避免泄露令牌。

## 分条件验证记录

- 20:24（Asia/Singapore）：VPS 独立完成配对、内层隧道及 RSD / DVT / LocationSimulation 通道测试，没有 Mac 转发参与。此前按用户简短回执记为蜂窝；用户后来补充已连接另一张 Wi-Fi，早期具体切换时间不详，因此早期记录不单独作为纯蜂窝验收证据。
- VPS 原有 `net.ipv6.conf.all.disable_ipv6=1` 和 `default.disable_ipv6=1`，导致 TUN 的 IPv6 地址设置报 errno 13。适配器现在只为新建的 `simloc-rsd` 接口开启 IPv6；全局设置保持原值。失败时关闭并清理接口，新增两项回归测试。
- 20:25：用户完全关闭 Wi-Fi 开关后，已有通道仍通过 RSD / DVT / LocationSimulation 验证。
- 随后主动重启 VPS 隧道服务，手机 49152 拒绝连接。说明本次对照中“已建立连接继续使用”与“关闭 Wi-Fi 后重新连接”的行为不同。
- 20:26 自动恢复；20:27 网页账户 `simlocation` 再次通过只读验证；该时刻具体接入网络状态未可靠对应。
- 随后再次重启隧道服务，5 秒后的验证仍未发现隧道；自启命令因验证未通过而没有执行。重连稳定性仍待验收。
- Wi-Fi 开关与监听状态存在相关线索，但必要条件尚未被严格确认；不要把一次拒绝或一次恢复写成确定的系统规律。

- 20:32：用户明确确认手机连接与 Mac 不同的 Wi-Fi；VPS 独立只读验证通过，主动重启服务后约 2 秒恢复，非特权账户通道验证再次通过。
- 20:34–20:35：用户通过控制中心断开 Wi-Fi、保留设置中的无线开关，并明确确认状态栏为蜂窝。原有隧道仍通过全部只读验证；主动重启隧道服务后观察 65 秒未恢复，日志显示手机配对端口拒绝连接。纯蜂窝从零重连未通过验收。

- 纯蜂窝失败后的补充测试：通过官方库对 62078 进行关闭自动配对的只读连接，同样得到 ConnectionRefusedError；未导出其他类型的配对私钥。

- 20:39：用户启用个人热点、无其他设备连接，在蜂窝 WireGuard 条件下再次重启服务；观察 40 秒未恢复，热点假设未获支持。下一步拟通过既有隧道采集配对服务单进程诊断日志，对照切网时刻。

服务每 5–30 秒自动重试，不会自动重新配对。尚未进行真实坐标设置、清除、路线回放、手机重启后恢复或长期锁屏测试。单次成功不等于长期稳定性验收。

## 手机入口

- 开启官方 WireGuard 的 SimLocation 隧道；系统会停用 Surge。
- 已确认异网 Wi-Fi 下连接和重连成功；切换蜂窝后已建立通道可用，但从零重连在 65 秒观察中失败。当前部署还不满足任意时刻纯蜂窝独立恢复的要求。
- 私密入口保存在同目录 `SimLocation-Console.txt` 和 `SimLocation-Console-QR.png`，含访问令牌，请勿分享。
- WireGuard 手机配置和二维码也含私钥，请勿公开。
- 网页 HTTP 流量在 WireGuard 内传输，没有开放公网网页端口。

## 运维（在 vps 上执行）

```sh
systemctl status simlocation-network simlocation-web wg-quick@wg-simloc
journalctl -u simlocation-network -n 30 --no-pager
/opt/simlocation-venv/bin/python /opt/simlocation/deploy/verify_device.py
```

验证脚本只打开 RSD/DVT/定位服务通道，不发送设置或清除定位指令。

配对端口在 `/etc/simlocation/network.json` 中显式配置；手机重启后端口是否变化尚未验证。不要把端口拒绝直接认定为密钥失效。

## 停止与回滚

```sh
systemctl stop simlocation-web simlocation-network
systemctl stop wg-quick@wg-simloc
ufw delete allow in on wg-simloc from 10.77.77.2 to 10.77.77.1 port 8765 proto tcp
```

手机关闭 WireGuard 后可重新开启 Surge。此时服务尚未配置自启。删除私钥、环境和状态目录需单独确认；停止服务不会删除这些文件。

## 独立问题

VPS 原有 DNS-over-TLS 解析失败，本次未改其全局策略。Python 依赖安装使用临时 SSH 代理，安装后已关闭；运行时连接手机使用固定内网地址，不依赖此代理。

## 21:09 keepalive 调整与保持测试准备

- 适配器新增可选配置 `keepalive`（`idle` / `interval` / `count`），只替换外层隧道 socket 的 keepalive，并设置相同时长的 `TCP_USER_TIMEOUT`；不配置时保持 upstream 行为。
- `/etc/simlocation/network.json` 现为 `{"idle": 30, "interval": 30, "count": 40}`，对端约 20.5 分钟无响应才判定断开（原约 12 秒）。已重启 `simlocation-network`，日志确认生效。
- 新增 `deploy/hold_probe.py`：在现有隧道上保持一组 RSD / DVT / LocationSimulation 连接（不发送坐标），内层连接使用同样的 keepalive；收到 SIGUSR1 时在同一条 DVT 连接上执行只读 `machTimeInfo`，同时被动记录 WireGuard 握手、收发计数与端点漂移。日志写入 `/var/lib/simlocation-tunnel/hold-test.jsonl`（600），端点只保留前两段和 NAT 端口。
- VPS 上适配器与探针共 23 项测试通过（含 Linux 内核接受 socket 选项的测试）。
- 备份：`/opt/simlocation/deploy/*.bak-20261102-keepalive`、`/etc/simlocation/network.json.bak-20261102-keepalive`。

回滚：

```sh
cd /opt/simlocation/deploy
cp -p network_tunneld.py.bak-20261102-keepalive network_tunneld.py
cp -p test_network_tunneld.py.bak-20261102-keepalive test_network_tunneld.py
cp -p /etc/simlocation/network.json.bak-20261102-keepalive /etc/simlocation/network.json
systemctl restart simlocation-network
```

保持测试的运行方式（隧道建立后执行）：

```sh
systemd-run --unit=simloc-holdprobe --collect -p RuntimeMaxSec=4000 \
  /opt/simlocation-venv/bin/python -u /opt/simlocation/deploy/hold_probe.py --duration 3600
systemctl kill -s USR1 simloc-holdprobe     # 触发一次同连接检查
journalctl -u simloc-holdprobe --no-pager    # 或读 hold-test.jsonl
systemctl stop simloc-holdprobe              # 提前结束，不发清除或坐标
```

## 21:11–21:27 保持测试结果

- 21:11:46 在异网 Wi-Fi 下建立隧道（新 keepalive 生效），21:12:03 打开保持会话，初次检查通过。
- 21:12:41 手机切到蜂窝，WireGuard 端点随即漂移；21:12:49 同连接检查 0.6 秒通过，同时 49152 新连接被拒，确认处于蜂窝。
- 锁屏约 11 分钟（21:13:47 到 21:25:06，用户提前结束，没满计划的 15 分钟）：期间 WireGuard 每 2 分钟正常重新握手，端点未变，隧道服务没有断开记录；21:25:07 同连接检查 0.7 秒通过。
- 21:27:20 主动停止探针，全程未发送坐标或清除命令。结论只覆盖这一次约 11 分钟锁屏，不代表长时间、弱信号或手机重启后的表现。

## 后续直接证据

单进程日志已确认 `remotepairingdeviced` 的连接策略包含 `prohibited types: cellular loopback`。切换蜂窝时记录接口禁止并撤销监听 flow，随后同端口拒绝连接。完整的脱敏证据时间线见同目录 `cellular-pairing-diagnosis.md`。本次已结束临时诊断采集，保留服务和当前隧道；未启用开机自启。

## 21:30 网页：默认坐标一键 + 定点页清除

- 后端 `state()` 新增 `defaults`（读 `SIMLOCATION_DEFAULT_LAT/LON`）；`/etc/simlocation/web.env` 增默认坐标 31.230416 / 121.473701。
- 定点页补「设为默认位置」(use-default) 与「清除定位」(clear-location)。clear 的后端 action 与前端 onclick 本已存在，HTML 此前漏了按钮元素，一并修好。
- 端到端真机验证（经网页同一套 API）：set 默认坐标 → 设备 ready(31.230416,121.473701) → clear → stopped，均 job succeeded。
- 改动文件：本机仓库 web/console.html、web/console.js、bin/simlocation_web.py，已部署到 /opt/simlocation（未提交 git）。备份后缀 .bak-20261102-webdefault。

回滚：

```sh
cd /opt/simlocation
for f in web/console.html web/console.js bin/simlocation_web.py; do cp -p "$f.bak-20261102-webdefault" "$f"; done
cp -p /etc/simlocation/web.env.bak-20261102-webdefault /etc/simlocation/web.env
systemctl restart simlocation-web
```
