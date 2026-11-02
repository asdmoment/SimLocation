# 同类思路对照（截至 2026-11，按 iOS 27.2 实测校正）

这几个项目都在做同一件事：手机不插线，让另一台主机拿着配对凭据，经网络进入手机的开发者服务。区别在于各自走的是**哪条通道**，而这条通道在当前 iOS 上还能不能用，决定了一个方案是否可行。下文的「本项目实测」都来自同目录的笔记，以及 2026-11-03 新做的一次并发测试。

## 一句话结论

- **JitStreamer-EB 提供的是思路，没有提供代码。** 它最早把「手机拨 WireGuard 到服务器，服务器拿配对文件回连手机」做成了产品形态，本项目的整体结构与它相同。但它进手机走的是 lockdownd（TCP 62078），这条路在 iOS 的 WireGuard utun 上已经被系统策略挡住；仓库也已在 2025-03 归档。本项目没有使用它的任何代码或服务。
- 真正能在 iOS 27 上复用的通道是 **RemotePairing（TCP 49152 起）**。omarchy 的 `tailnet-tunnel.py` 与本项目各自独立走到了这条路上，结论基本一致。

## 逐个对照

| 项目 | 进手机的通道 | 在 iOS 27 + VPN 下 | 本项目借鉴 / 避开 |
|---|---|---|---|
| JitStreamer-EB（已归档） | 配对文件 → WireGuard → lockdownd 62078 → CoreDeviceProxy | 不通：作者在同系列项目 jkipsec 的 README 里写明，WireGuard utun 的策略会挡入站 lockdown（62078），后来 IPsec 也被挡；本项目实测 62078 同样被拒 | 借鉴整体形态；避开它的 `CorsLayer` 任意来源、默认开放 `/register` 这类设计，控制台改为同源校验、密码登录，不提供注册接口 |
| jkipsec | IPsec 隧道代替 WireGuard | 作者自述后来也被挡 | 只作为「换 VPN 协议绕不过系统策略」的佐证 |
| netmuxd | 网络版 usbmuxd，经 lockdown 连接网络设备 | 依赖 62078，跨子网经 VPN 不通 | 不用 |
| ios-location-server | lockdown TCP + CoreDeviceTunnelProxy + 用户态隧道，DVT 前先 `auto_mount`，带断线重连 | 依赖 lockdown，不适用于经 VPN 远程；作者在 iOS 18.5 上测得锁屏约 100 秒后深睡，模拟定位不保持 | 借鉴 `auto_mount`（手机重启后自动挂载开发者镜像，已实现）；断线重连尚未实现 |
| omarchy `tailnet-tunnel.py` | tailnet → RemotePairing 49152，TLS-PSK TCP 隧道 | 可用（作者测于 iOS 27.0.1）：62078 只认同子网来源；纯蜂窝下 49152–49167 全部拒绝；熄屏数小时后隧道和截图仍可用，但启动 App 失败；TCP 隧道需要 Python 3.13+ 且带 TLS-PSK | 借鉴 49152–49167 端口回退（已实现）、固定 host_identifier（已在用）；按它的提醒，Ubuntu 24.04 主机改装 deadsnakes 的 Python 3.14 |
| StikDebug / LiveContainer | 手机上的 App 经本机回环访问自己的开发者服务，主要用于 JIT | 本项目实测 iOS 27.2 的 RemotePairing 策略禁止 loopback 类型的新连接；它们走的 lockdown 路径是否受影响，本项目没有验证 | 与远程设定位无关 |
| pymobiledevice3 11.49.2 | 上面多数方案的底层库 | — | 直接依赖；用它的用户态隧道做了下面的并发测试 |

## iOS 27.2 实测（本项目）

1. `remotepairingdeviced` 的连接策略含 `prohibited types: cellular loopback`。
   - 连任何 Wi-Fi 都能新建通道，不必和 Mac 在同一个网络。
   - 纯蜂窝时监听被撤销，新连接全部被拒。
2. 已建立的通道切到蜂窝后仍能保持：蜂窝下测过约 10.5 分钟，锁屏测过约 11 分钟。前提是 WireGuard 保持为当前活动的 VPN。
3. 因为 loopback 被禁止，Surge 内置的 Tailscale/WireGuard 入站（最终在本机回环上重新拨号）走不通。iOS 同时只能有一个 VPN，开 WireGuard 时 Surge 会被停用。
4. 经 WireGuard 访问 lockdown 62078 被拒，开个人热点也一样。
5. 同一个配对身份可以被多台主机同时使用（2026-11-03 新测）：两个进程用同一份配对记录各建一条 RemotePairing 隧道，第二条建立后，第一条每 10 秒的 RSD 检查照常成功，两条都持续了 45 秒以上。所以手机只导入一个 WireGuard 配置，就能同时连上全部 VPS。
6. 开发者镜像是 Cryptex 个性化镜像。手机重启后，控制台第一次设定位时会自动挂载：从 DeveloperDiskImage 仓库下载镜像（部署时已预下载），再向 Apple 签名服务器取个性化签名。VPS 需要能解析并访问这些域名。挂载状态属于整台手机，任何一台 VPS 挂上，其他几台都能直接用。

## 本项目额外补上的部分

- 设备连接的 TCP keepalive：pymobiledevice3 默认约 12 秒判定断线，在蜂窝上过于敏感，所以隧道和定位会话都放宽到约 20.5 分钟（`30,30,40`）。
- 适配器以只有 `CAP_NET_ADMIN` 的独立账户运行，启动前自检；nftables 规定只有两个服务账户和 root 能访问手机与 tunneld；控制台没有任何 capability，重启时不会结束定位会话。
- 多主机：每台 VPS 都有完整的一套服务，手机一个配置连全部主机。任何一台宕机，其他主机照常可用。
