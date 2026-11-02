# Surge 跨网开发者连接复验

日期：2026-11-02（Asia/Singapore）

## 条件

- 用户确认手机仅使用蜂窝网络，USB 和 Wi-Fi 断开。
- 手机 Surge 6.100.0 build 4005，iOS 27.2；手机网络摘要确认主接口为蜂窝。
- Mac 的 Surge 经现有 Headscale tailnet 连接手机；手机 Surge MCP 管理接口可以正常收发。
- pymobiledevice3 11.49.2；已有 RemotePairing 记录存在，未导出或转移配对私钥。

## 实测

| 路径 | 结果 | 证据 |
|---|---|---|
| IPv4 → RemotePairing 49152 | 两种握手入口均被关闭，无协议回复 | 手机请求 84、87：127.0.0.1 连接成功、发送 203 字节、收到 0 字节、Closed by remote |
| IPv6 → RemotePairing 49152 | 排除规则阻挡后，同样被关闭 | 手机请求 93、94：::1 连接成功、发送 203 字节、收到 0 字节、Closed by remote |
| IPv4 → lockdownd 62078 | 系统拒绝本地连接 | 手机请求 88：Operation not permitted (POSIX:1) |
| IPv6 → lockdownd 62078 | 排除规则阻挡后，系统同样拒绝 | 手机请求 97：Operation not permitted (POSIX:1) |

测试只进行了握手和只读 QueryType；未设置定位、启动 DVT 会话或重新配对。

## 排除的干扰

- 首轮手机管理接口超时，链路恢复后重新进行了全部验证。
- Mac 的手机 tailnet IPv6 地址原本匹配 DIRECT 局域网规则，已用单地址临时路由做对照。
- 手机入站规则原本仅放行 Mac 的 IPv4，已用仅限 Mac IPv6 来源、::1 目的地址、49152/62078 两端口的临时规则做对照。
- 验证结束：Mac 临时规则列表为空；手机临时规则列表为空。配置文件未改。

## 结论边界

当前 Surge 普通入站路径未能完成这两类 Apple 服务的握手，不能据此宣称所有 Surge 版本、配置或未来实现都不可能。RemotePairing 的服务端关闭发生在收到首条握手消息后，尚不足以从客户端日志独立证明其内部拒绝原因必然是环回来源。

Surge 手册明确说明普通 tailnet 入站会重拨到本机 127.0.0.1 或 ::1；仅把 Surge 内的 Tailscale 改成 Surge 内的 WireGuard，并不自动改变这一入站模型。下一步用官方 WireGuard App 做点对点对照。WireGuard 启用期间会替换手机当前系统 VPN，故 Surge 会停用；点对点分流不等于两者能同时保持启用。

参考：[Surge Tailscale](https://manual.nssurge.com/policies/tailscale.html)、[Surge WireGuard](https://manual.nssurge.com/policies/wireguard.html)、[WireGuard Quick Start](https://www.wireguard.com/quickstart/)。
