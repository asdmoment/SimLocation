# 纯蜂窝重连诊断结论

日期：2026-11-02，Asia/Singapore。

## 范围

本机 iPhone 当前 iOS、官方 WireGuard、vps 上的 RemotePairing 连接。结论限定于本次实际测试的路径，不泛化为所有 VPN、所有 iOS 版本或所有手机端实现。

## 测试

1. 手机连接与 Mac 不同的 Wi-Fi，USB 断开；VPS 直接运行协议栈，Mac 不承担转发。
2. RSD、DVT、LocationSimulation 通道均通过只读验证；重启 VPS 隧道服务后约 2 秒恢复。
3. 手机明确切到蜂窝，保留无线开关和 WireGuard。原通道继续通过只读验证；主动断开后的重连观察 65 秒失败。
4. 手机自身热点开启、没有设备加入，40 秒内未恢复。
5. 再次在异网 Wi-Fi 建立连接，并通过既有隧道采集 `remotepairingdeviced` 单进程日志。保持服务和隧道运行，仅将手机切回蜂窝。

## 直接证据

诊断流内的相对时间：

- 0.1 秒：配对控制端口开放。
- 5.6 / 10.5 秒：配对进程记录连接策略 `prohibited types: cellular loopback`，当时 VPN 接口路径标记 `uses wifi`。
- 24.0 秒：切网时同一进程记录 `Interface <private> prohibited, cannot use interface option`、`unsatisfied (No network route)`，并移除一个监听 flow registration。
- 25.6 秒：相同 VPS 来源、相同手机地址和端口开始返回 `ConnectionRefusedError`。

这些证据支持：当前路径的重新建连受手机配对服务的蜂窝接口限制。现有隧道暂时仍可传输，不等于新连接可以建立。

原始诊断只采集指定进程，标识已脱敏，文件权限 600，保存在 VPS 的 `/var/lib/simlocation-tunnel/network-diagnostic.jsonl`；无其他应用日志，无新增私钥导出。

## 已排除/已修复

- RemotePairing 私钥和原主机身份在 VPS 上有效，已成功验证。
- VPS 原配置禁用 IPv6 导致内层 TUN 设置地址失败，已仅对 `simloc-rsd` 接口修复；全局 IPv6 设置未改。
- 网页账户已能直接使用开发者通道，且读不到配对私钥。
- 网页鉴权和接口绑定测试已通过。

## 补充：保留通道的寿命与 VPN 占位（20:50 后复核）

- 环回同样在禁止列表里。Surge tailnet 入站会重拨到 `127.0.0.1` / `::1`，这与 `surge-verification.md` 中 49152 收到首条握手后被关闭、62078 报 `Operation not permitted` 一致；之前"尚不足以证明是环回来源"的保留，现在有设备侧策略行作为直接证据。
- 20:41:16 在异网 Wi-Fi 建立的隧道，切蜂窝后继续可用（20:45 验证通过），20:53:26 断开，VPS 记录为隧道读取 `TimeoutError`。
- 断开原因不是 iOS 策略或休眠：Headscale 在 20:53:17 记录手机 Surge 节点重新上线，即手机切回了 Surge，系统随之停用 WireGuard。9 秒后 VPS 的 keepalive 判定对端失联。蜂窝下保持约 10.5 分钟，由人为切换结束，未观察到自然断开；不能当作寿命上限。
- 之后 VPS 的 ping、49152 的 SYN 均无回应，WireGuard 接收计数不变、握手不再更新，说明手机 WireGuard 已整体停用，和端口拒绝（RST）是两种不同状态。
- pymobiledevice3 11.49.2 给外层隧道 socket 设 keepalive 为空闲 3 秒、间隔 3 秒、3 次，约 12 秒无响应即断开；内核 TUN 模式下，RSD 之上的内层服务连接（DVT 等）也是同一设置。在蜂窝上，这会把短暂失联变成无法恢复的断开。

外部旁证（只作参考，不替代本机实测）：

- SideStore 在 iOS 27 RC 上的蜂窝刷新，要求用户建立 `TurnOffData` / `TurnOnData` 快捷指令，在本机连接期间临时关闭蜂窝数据（[SideStore #1505](https://github.com/SideStore/SideStore/issues/1505)）。这与蜂窝被禁止一致；它能成立是因为本机回环 VPN 不需要外网，远端 VPS 方案无法照搬。
- jkipsec README 记录 iOS 对 WireGuard `utun` 的入站策略会挡住 62078；系统 IKEv2 曾可绕开，作者补注 IPsec 现在也被挡（[jkipsec](https://github.com/jkcoxson/jkipsec)）。

据此，对本机这个 iOS 版本：凡是从远端主机连进手机的方案（VPS + WireGuard、Surge tailnet、官方 Tailscale），纯蜂窝下都无法建立新的开发者通道。WireGuard 剩下的唯一形态是"任意 Wi-Fi 下建立，蜂窝下保持"，并且保持期间 WireGuard 必须一直是手机的活动 VPN，Surge 不能开启。该形态在锁屏休眠下能否保持，尚待实测。

## 状态与边界

- 当前部署未达到“任意时刻纯蜂窝独立建立及恢复连接”的最终要求，尚未启用开机自启。
- 全程未发送设置坐标、清除定位或路线回放命令。
- 继续改变 VPS 防火墙或延长重试不能消除已经观测到的手机端接口策略。
- 后续若继续研究，应评估手机端建立/维持开发者通道的实现及其网络路径行为；是否能在此 iOS 版本满足最终要求仍待实证，不承诺换一个客户端即可解决。
