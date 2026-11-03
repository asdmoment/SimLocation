# 远程部署：WireGuard + 多台 VPS（只用手机）

让 iPhone 在**不连 Mac、不插 USB**的情况下使用 SimLocation：

1. 手机用官方 WireGuard App 连到若干台 VPS。
2. 每台 VPS 上的 tunneld 适配器经 RemotePairing 建立开发者隧道。
3. 手机用 Safari 打开只在 WireGuard 内监听的网页控制台，设定位或清除定位。

本目录包含部署工具、systemd 单元、模板、运维说明和调查记录。**仓库里不放任何凭据**：手机私钥只存在于手机上，配对记录只存在于各台 VPS 的服务账户目录里，控制台密码在服务器上只有 scrypt 哈希。

## 先读：结论与边界（iOS 27.2 实测）

- **纯蜂窝下无法从零新建开发者通道。** 手机的配对服务禁止经 cellular / loopback 建立新连接（`prohibited types: cellular loopback`）。
- **可行用法：连任意 Wi-Fi 时建立通道，之后切到蜂窝继续保持。** 保持期间 WireGuard 必须是手机当前的活动 VPN；iOS 同一时间只能开一个 VPN，所以 Surge 会被停用。已测过蜂窝下约 10.5 分钟、锁屏约 11 分钟。
- **同一个配对身份可以同时被多台 VPS 使用**（两条并发隧道互不影响），所以手机导入**一个**配置就能同时连上全部 VPS，任意一台都可以使用。
- 手机重启后，第一次设定位会自动重新挂载开发者镜像，需要先解锁手机一次，而且这台 VPS 要能访问 GitHub 和 Apple 签名服务器。挂载状态属于整台手机，任一台挂上即可。
- 证据与对照见 `notes/`：
  - `prior-art.md`：与 JitStreamer-EB、omarchy、ios-location-server 等的对照
  - `cellular-pairing-diagnosis.md`：纯蜂窝诊断
  - `wireguard-setup-notes.md`：首台部署记录
  - `surge-verification.md`：Surge 入站走不通的原因

## 架构

```
iPhone 官方 WireGuard（一个隧道，每台 VPS 一个 peer）  10.77.77.2
  ├─ UDP 51820 ─► VPS A  wg-simloc 10.77.77.1
  ├─ UDP 51820 ─► VPS B  wg-simloc 10.77.77.11
  └─ …

每台 VPS：
  simlocation-firewall   nftables 出站过滤，只有 root 和下面两个账户能访问手机、RSD 接口、tunneld
  wg-quick@wg-simloc     只路由手机的 /32，不改默认路由、DNS、NAT
  simlocation-network    账户 simlocation-tunnel，只有 CAP_NET_ADMIN
                         连 10.77.77.2:49152（拒绝时依次试到 49167），维持 RSD 隧道
                         tunneld API 只开在 127.0.0.1:49151
  simlocation-web        账户 simlocation，没有任何 capability
                         只监听本机的 WireGuard 地址 :8765，密码登录
                         定位会话是它的子进程，重启控制台不会结束会话
```

## 目录

| 路径 | 作用 |
|---|---|
| `fleet.py` | 在 Mac 上运行：部署、查看状态、轮换手机密钥、设置控制台密码、同步收藏 |
| `fleet_remote.py` | fleet.py 经 `ssh 主机 python3 -` 发到 VPS 上以 root 执行的那一半（只用标准库） |
| `network_tunneld.py` | tunneld 适配器：按固定地址连手机并维持隧道；`--self-test` 检查 TUN 权限 |
| `requirements.lock` | VPS venv 的锁定依赖（pymobiledevice3 11.49.2） |
| `systemd/` | 三个单元：firewall、network、web |
| `examples/` | `fleet.json.example`（清单模板）、`network.json.example` |
| `verify_device.py` / `hold_probe.py` | 只读验证、保持测试 |
| `diagnostics/` | 调查期间用过的只读脚本（设备和主机由环境变量指定） |
| `notes/` | 过程与结论 |
| `test_*.py` | 单元测试 |

## 部署（在 Mac 上）

前置条件：

- 能以 root 身份免密 SSH 到各台 VPS（Ubuntu/Debian）。
- 本机有这台手机的 RemotePairing 配对记录（`~/.pymobiledevice3/remote_<UDID>.plist`）。
- 至少一台主机已经验证能用这份记录连上手机，作为 `pair_reference`。

VPS 自带的 python3 低于 3.13 或没有 TLS-PSK 时（例如 Ubuntu 24.04 的 3.12），部署会自动从 deadsnakes 安装 Python 3.14。

1. 把 `examples/fleet.json.example` 复制成 `deploy/fleet.json` 并填写。这个文件已被 git 忽略。

   | 字段 | 说明 |
   |---|---|
   | `phone.udid` / `alias` | 设备 UDID 和控制台里显示的别名 |
   | `phone.address` | 手机在 WireGuard 里的地址，`10.77.77.2` |
   | `phone.pair_record` | 本机配对记录的路径 |
   | `phone.pair_reference` | 已验证可用的主机。它的记录在密钥与本机记录一致后，会原样分发到其他主机 |
   | `phone.public_key` | 由 `fleet.py phone` 写入，不用手填 |
   | `phone.auto_set_host` | 手机连上 WireGuard、开发者通道建立后，由哪一台主机自动设到默认位置。省略时用 `pair_reference`，设成 `null` 则关闭。只能选一台，因为每台主机各自保持一个会话，多台同时设定位会互相覆盖 |
   | `defaults.default_location` | 控制台的默认坐标 `[纬度, 经度]` |
   | `defaults.*` | 其余可选项：`wg_port` 51820、`web_port` 8765、`mtu` 1280、`keepalive`、`tcp_keepalive` `30,30,40`、`web_user` |
   | `hosts.<名字>.ssh` | ssh 参数列表，例如 `["-J", "跳板", "主机"]` |
   | `hosts.<名字>.endpoint` | 手机拨号用的地址；`auto` 表示从 `ssh -G` 读取 |
   | `hosts.<名字>.address` | 这台主机的 WireGuard 地址，各台必须不同 |

2. `python3 deploy/fleet.py deploy [主机…]`

   这一步会：安装 WireGuard、nftables 和合适的 Python；创建两个服务账户；用锁定的依赖建 venv；安装代码；以 `simlocation-tunnel` 身份运行适配器自检；写入配置、单元和防火墙；预下载开发者镜像。

   可以反复执行，只会重启发生了变化的服务。隧道正在使用时不会重启 network 服务；确实需要重启时，让手机连上 Wi-Fi，再加 `--restart-network`。

3. `python3 deploy/fleet.py phone`

   在本机生成新的手机密钥，服务器上只保存公钥。输出到一个权限 0700 的私有目录：
   - `SimLocation-all.conf` / `.png`：一个隧道连全部 VPS，推荐导入。只有发往各主机的流量进隧道，其他 App 照常走 Wi-Fi 或蜂窝；Surge 停用期间，这些流量不受 Surge 保护
   - `SimLocation-strict.conf` / `.png`：与上面用同一把密钥，所有流量都进隧道。默认路由交给 `pair_reference` 指定的那台主机，它不转发任何来自手机的包，所以开着它时，除了 SimLocation，其他 App 都断网，不会绕过隧道直连。控制台的地图（Leaflet、瓦片和地址搜索）由 VPS 代为获取，经隧道送到手机，所以严格模式下照常能用。和 `SimLocation-all` 同一时间只开一个
   - 每台主机各自的配置，作为备用

   用 WireGuard App 扫码导入，导入后删除整个目录。

4. `python3 deploy/fleet.py password [主机…]`

   为每台主机生成一个随机密码，服务器上只保存哈希。输出 `simlocation-passwords.csv`：在 Mac 的「密码」App 中用「文件 → 导入密码」导入，就会通过 iCloud 同步到手机，导入后删除这个 CSV。重设密码会让该主机已登录的会话全部退出。

5. `python3 deploy/fleet.py status`：查看服务、WireGuard 握手、隧道、控制台、密码和定位会话的状态。

6. `python3 deploy/fleet.py favorites --from 主机 [目标…]`：把一台主机上的收藏复制到其他主机。每台主机的收藏是独立保存的。

## 手机用法

1. 在 WireGuard App 里打开 `SimLocation-all`。打开期间 Surge 会被系统停用，用完关掉即可恢复。
2. Safari 打开任意一台主机的控制台，例如 `http://10.77.77.1:8765/`，用 iCloud 钥匙串自动填充密码登录。
3. 打开 WireGuard 后大约半分钟，`auto_set_host` 那台主机会自动把定位设到默认位置，不用打开浏览器。每次通道重新建立都会触发，只有一个例外：你在控制台手动清除过，而且之后通道断开不到 1 分钟（比如换了 Wi-Fi），这时不会把定位设回去。已经有会话在运行（包括正在重连）时，也不会触发。也可以用默认位置、收藏、地图选点或输入经纬度来设定位；「清除定位」恢复真实位置。设好以后会在后台一直保持，直到清除为止。
4. **同一时间只用一台主机的控制台。** 每台主机各自保持自己的定位会话，清除只结束本机那一个。要换主机，先在原来那台上清除。
5. 手机重启后：先解锁一次，连上 Wi-Fi 并打开 WireGuard，等待大约半分钟让通道建立；第一次设定位会多花一点时间来挂载开发者镜像。

## 运维（VPS 上）

```sh
systemctl status simlocation-firewall wg-quick@wg-simloc simlocation-network simlocation-web
journalctl -u simlocation-network -n 30 --no-pager
nft list table inet simlocation          # 计数器可以看出有没有别的账户被拦截
# 只读验证通道（不发坐标、不清除）
/opt/simlocation-venv/bin/python /opt/simlocation/deploy/verify_device.py
# 保持测试（蜂窝/休眠），需要先建好隧道
systemd-run --unit=simloc-holdprobe --collect -p RuntimeMaxSec=4000 \
  /opt/simlocation-venv/bin/python -u /opt/simlocation/deploy/hold_probe.py --duration 3600
systemctl kill -s USR1 simloc-holdprobe    # 触发一次同连接只读检查
systemctl stop simloc-holdprobe            # 结束，不发清除或坐标
```

代码保存在 `/opt/simlocation`，上一个版本保留在 `/opt/simlocation.prev`。配置在 `/etc/simlocation/`，均由 fleet 生成，改动请修改清单后重新部署。

## 已知问题

- 如果主机的 systemd-resolved 被设为强制 DNS-over-TLS，而 853 端口又不通，那么系统 DNS 会整体失效，这台主机就没法在手机重启后挂载开发者镜像。其他主机不受影响，修好之前可以先用别的主机挂载。
- 在 Ubuntu 上，apt 的 needrestart 会在装包后重启无关服务，所以部署时会暂停它。

## 安全边界

- **凭据**
  - 手机私钥在 Mac 上生成，扫码导入后删除；服务器只保存手机的公钥。
  - 控制台密码在服务器上只有 scrypt 哈希，明文只存在「密码」App 里。
  - 配对记录放在 `simlocation-tunnel` 的私有目录里（0600），控制台账户读不到。
- **网络**
  - tunneld 只监听 `127.0.0.1`，控制台只监听本机的 WireGuard 地址。
  - ufw 只允许手机访问控制台端口；nftables 只允许 root 和两个服务账户访问手机、RSD 接口和 tunneld。
  - nftables 的 forward 链丢弃一切进出 `wg-simloc` 的转发包，即使主机为别的服务开了 `ip_forward` 也一样。所以手机的流量不会经 VPS 转发出去，严格模式就是靠这一条实现「断网而不泄漏」的。
- **控制台鉴权**：密码登录；会话 cookie 设为 `HttpOnly; SameSite=Strict`；`/api/*` 还要求带 CSRF 头；Host 名单防 DNS 重绑定；按来源和全局两级限制失败次数。
- **部署工具本身**：在服务账户目录里写文件时，以该账户身份执行；root 读取账户目录时，每一级都不跟随符号链接；从参考主机取回的配对记录，必须解析出与本机一致的密钥，才会分发到其他主机。
- 隧道日志和 `hold_probe.py` 只记录 WireGuard 端点的前两段和 NAT 端口，不记录密钥。

## 验证

```sh
cd deploy
python3 -m unittest test_fleet test_network_tunneld test_hold_probe
python3 -m py_compile fleet.py fleet_remote.py network_tunneld.py hold_probe.py verify_device.py
```

> 关于这套机制：它把「谁能给这台手机设定位」，从「人在现场、同一网络、USB 信任」改成了「持有配对记录、能进 WireGuard、知道控制台密码」。主机越多，可用性越高，但长期持有配对私钥的机器也越多，**任何一台被攻破，都等于拿到了手机开发者服务的钥匙**（只在手机连着 WireGuard 时可用）。所以每台主机的 root、配对记录和控制台密码，是这条链路里最需要保护、也最需要审计的地方。不再使用的主机，应当从清单中去掉，并删除它的 `/var/lib/simlocation-tunnel`。
