# 四节点中继（reflect relay fleet）

和 WireGuard 那套一样，四台主机（`fleet.json` 里的全部主机）各自独立地连手机：
手机上的 a-Shell 同时开四条外层 mTLS 连接，每台主机用自己的 CA 和证书，任何一台
掉线、证书出错或被停掉，都不影响另外三台。单台的原理、限制和排错见 `REFLECT.md`。

配对记录和私钥不离开服务器，手机包里只有每台主机的客户端证书。

## 一次性准备 / 换证书

在 Mac 上，`deploy/` 目录里：

```bash
python3 fleet.py relay-prepare --out ~/vps-a/relay-YYYYMMDD --days 30
python3 fleet.py relay-deploy --credentials ~/vps-a/relay-YYYYMMDD     # 不写主机名 = 全部四台
python3 fleet.py relay-status
```

- `relay-prepare` 为每台主机生成独立 CA、服务器证书和手机客户端证书（1–90 天，默认 30），
  输出目录必须是新的，已有的目录不会被覆盖。CA 私钥用完即丢，不落盘。
- 输出目录里：`servers/<主机>/` 只发给那台主机；`Reflect-Relay-Fleet.zip` 是手机包；
  `manifest.json` 记录到期时间和各台公网 IP。整个目录都是私有的（0700/0600），不要进 git。
- `relay-deploy` 只把那台主机自己的服务器身份经 SSH 发过去，并更新中继代码。
  有中继会话或定位会话在跑时会拒绝；离到期不足 3 小时的包也会拒绝。
- 证书到期后手机端会出现 `NODE_STOPPED SSLCertVerificationError` 或一直 `RECONNECTING`，
  重新执行上面三步，并把新的手机包换到手机上。

## 手机

1. 把 `Reflect-Relay-Fleet.zip` 传到手机，解压，在 a-Shell 里 `pickFolder` 选解压出来的
   `Reflect-Relay-Fleet` 文件夹。旧的单台包（Reflect-Relay-Phone*）可以删掉。
2. Surge 保持开启，reflect 模块（`10.7.0.1 = reflect`）保持启用，连 Wi-Fi。
3. 运行：

   ```
   python3 reflect_relay.py phone --config phone.json
   ```

   每台主机的输出带前缀，例如 `[vps-a] RELAY_READY`。四行都出现说明四条外层连接都通了；
   这只表示传输层就绪，不代表设备通道已建立。
4. a-Shell 必须留在前台。只连其中几台：`--nodes vps-a vps-d`。

服务器上的中继会话没开时，对应主机会每 5 秒打印一次 `RECONNECTING`，这是正常的，
先开会话或先跑手机都可以。

## 正式使用：常驻

```bash
python3 fleet.py relay-start --persistent       # 不设到期；中继是开机自启、崩溃自动重启的 simloc-reflect.service
python3 fleet.py relay-status                   # relay.session = persistent
```

常驻会话只在 `relay-stop` 时回到 WireGuard 后端，主机重启后仍是中继后端。
证书到期前（`relay-status`/`manifest.json` 可查）要换：`relay-stop` → `relay-prepare --days 90` →
`relay-deploy` → `relay-start --persistent`，并换手机包。手机包默认不设时长，由服务器决定会话多长。

## 临时试验：限时会话

```bash
python3 fleet.py relay-start --seconds 3600     # 300..7200 秒，到期自动回退到 WireGuard 后端
python3 fleet.py relay-verify                   # 只读：RSD / DVT / LocationSimulation 能否建立
python3 fleet.py relay-stop                     # 提前结束；有定位会话时拒绝
python3 fleet.py relay-stop --force             # 强制断开，不会清除手机上的坐标
```

- 自动设置：常驻会话沿用 `fleet.py deploy` 写进 `web.env` 的开关，只有 `phone.auto_set_host`
  （默认 `pair_reference`，即 vps-b）打开。手机一跑 `reflect_relay.py`、隧道出现，那台就设成默认坐标；
  手动清除后一分钟内的重连不会再设。限时试验会话一律关闭自动设置。`relay-status` 的 `auto_set` 字段可查。
- 开会话期间该主机的开发者通道换成中继，
  控制台照常用 `http://10.77.77.x:8765`（经 Surge 的 `SimLocation-WG`，见 `SURGE.md`）。
- 同一时间只让一台主机持有定位会话。另外三台是热备：主用那台出问题时，
  先在它的控制台清除或 `relay-stop --force` 隔离它，再在备用主机上设置坐标。
- 会话到期由 systemd 计时器执行 `reflect_session.py disable`；主机在会话中重启，
  `simlocation-relay-recover.service` 会在开机时做同样的回退，回退完成后删除自己。

## 维护速查

| 情况 | 做法 |
|---|---|
| 手机全部 `RECONNECTING` | `relay-status` 看 `relay.session`；是 `disabled` 就 `relay-start` |
| 某一台 `NODE_STOPPED` | 证书过期或包不匹配：重新 `relay-prepare` + `relay-deploy`，换手机包 |
| `phone_port_refused` | 手机不在 Wi-Fi，或 reflect 模块没开 |
| 主机重装 / 新增主机 | 先 `fleet.py deploy`，再整套 `relay-prepare`（包按 `fleet.json` 的主机集合生成） |
| 某台公网 8446 不通（云厂商挡入站 TCP，vps-b 就是） | 手机包 `phone.json` 里把那台的 `server` 改成它的 WireGuard 地址（如 `10.77.77.1`），经 Surge 的 `SimLocation-WG` 连；服务器需要 `fleet_remote.py` 里的两条规则（wg-simloc 入站 8446、回包放行），`fleet.py deploy` 会带上。每次 `relay-prepare` 后都要重新改 |
| 某台 `RELAY_READY` 后立刻断开、反复重连，服务器日志什么都没有 | 多半是服务器 Python ≥ 3.13 严格校验客户端证书。2026-11-05 之前生成的包缺 AKI/SKI，vps-b（3.14）因此拒绝；重新 `relay-prepare` 即可 |
| 只更新代码不换证书 | 用同一个仍在有效期内的目录再跑一次 `relay-deploy` |
| 查看单台详情 | 主机上 `python3 /opt/simlocation/deploy/reflect_session.py status`，以及 `REFLECT.md` 的排错表 |

## 已知限制

- 蜂窝网络下 iOS 拒绝新的 RemotePairing 连接；已建立的会话从 Wi-Fi 切到蜂窝能否保持，尚未验证。
- a-Shell 退到后台或锁屏后能否存活，尚未验证。
- 停掉中继不会清除坐标；要清除请在控制台操作。
