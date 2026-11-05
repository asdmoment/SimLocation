# 用 Surge 打开四台主机的控制台

手机开着 Surge 时，用 Surge 的一个 WireGuard 策略连四台 VPS，代替切到官方 WireGuard App。
只有发往四个控制台地址的流量进隧道，其他流量照原来的规则走。

## 配置（三处）

`[Proxy]` 里加一行：

```ini
SimLocation-WG = wireguard, section-name=simlocation-fleet
```

配置末尾加一段。`private-key` 是手机的 WireGuard 私钥，和官方 App 里 SimLocation-all 的
`[Interface] PrivateKey` 是同一把；服务器公钥和端点来自 `fleet.json`（`fleet.py status` 能核对）：

```ini
[WireGuard simlocation-fleet]
private-key = ENCRYPT_KEY
self-ip = 10.77.77.2
mtu = 1280
peer = (public-key = <vps-b 公钥>, allowed-ips = 10.77.77.1/32, endpoint = <IP>:51820, keepalive = 25)
peer = (public-key = <vps-d 公钥>, allowed-ips = 10.77.77.11/32, endpoint = <IP>:51820, keepalive = 25)
peer = (public-key = <vps-a 公钥>, allowed-ips = 10.77.77.21/32, endpoint = <IP>:51820, keepalive = 25)
peer = (public-key = <vps-c 公钥>, allowed-ips = 10.77.77.31/32, endpoint = <IP>:51820, keepalive = 25)
```

`[Rule]` 里加四条，**必须排在任何覆盖 `10.0.0.0/8` 的 DIRECT 规则之前**（例如 Sukka 的
`ip/lan.conf`），否则控制台会被判成 DIRECT 而打不开：

```ini
IP-CIDR,10.77.77.1/32,SimLocation-WG,no-resolve
IP-CIDR,10.77.77.11/32,SimLocation-WG,no-resolve
IP-CIDR,10.77.77.21/32,SimLocation-WG,no-resolve
IP-CIDR,10.77.77.31/32,SimLocation-WG,no-resolve
```

示例手机 18 Pro：这四条写在 `[Rule]` 的 `# >>> generated from rules/ …` 标记线以上；
暂存副本是 `~/path/to/surge-profile.conf`，私钥在那里是占位符。

几点约束：

- 不写 `dns-server`、`allow-peer-access`、`listen-port`、默认路由。控制台只用 IP 访问，
  服务器也不该能经这条隧道连手机。
- 一个策略四个 peer。不要拆成四个同私钥的策略，它们会互相抢握手。
- 四个端点 IP 要在 `tun-excluded-routes` 里，否则加密后的 UDP 会被 VIF 抓回来。
- Surge 模块不能新增 `[Proxy]`，模块里的规则也只能指向 DIRECT/REJECT，所以这三处只能写进配置本身。
- 同一把私钥和地址不要在两处同时用：官方 WireGuard App 和 Surge 二选一。

## 检查

1. 手机上打开 `http://10.77.77.21:8765`，Surge 请求列表里应显示策略 `SimLocation-WG`。
   显示 DIRECT 就是规则顺序不对。
2. 在 Mac 上 `python3 deploy/fleet.py status`：对应主机的「WireGuard 握手」应变成几秒前。
   策略只在有流量时握手，没打开过控制台就不会有握手。
3. 握手没有、规则也对：核对私钥是否和官方 App 里的一致（服务器只认 `fleet.json` 里的
   `phone.public_key`），以及当前网络是否放行 UDP 51820。

## 什么时候要改

| 变化 | 要改的 |
|---|---|
| `fleet.py phone` 轮换了手机密钥 | `private-key` 换成新私钥；官方 App 也要重新导入 |
| 新增或去掉一台主机 | 增删一个 `peer` 和一条规则；端点 IP 同步进 `tun-excluded-routes` |
| 主机换 IP | 改 `endpoint` 和 `tun-excluded-routes` |
| 主机重装、WireGuard 服务器密钥变了 | 改对应 peer 的 `public-key`（`fleet.py deploy` 后 `fleet.json` 里是新值） |

## 和中继（reflect relay）的关系

这条 WireGuard 除了打开控制台，还承载 vps-b 的中继连接：它公网的 8446 被云厂商挡了，手机包 `phone.json`
里它的 `server` 改成了 `10.77.77.1`（`FLEET-RELAY.md` 排错表里的做法），a-Shell 里的 `reflect_relay.py` 连
`10.77.77.1:8446` 走的就是上面那条 `IP-CIDR,10.77.77.1/32,SimLocation-WG`。所以删掉或改坏这条策略，vps-b
的中继就断；另外三台走公网，不受影响。每次 `relay-prepare` 重新生成手机包后，都要再改一次 `phone.json`。

设备通道本身由 `reflect_relay.py` 和服务器上的限时中继会话负责，手机上还要开着 Surge 模块 SimLocation Reflect
（`10.7.0.1 = reflect`），见 `REFLECT.md`。会话到期后手机端会一直显示 `RECONNECTING`，这是服务器入口已关闭，不是手机的问题。
