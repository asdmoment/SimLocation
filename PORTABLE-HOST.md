# 随身主机部署指南

让 SimLocation 脱离笔记本，跑在一块可以装进口袋的小板子上，从而在外出时也能改设备定位。

## 为什么必须带一台主机

先说清楚这个方案存在的理由，免得走弯路。

iOS 17 之后，`LocationSimulation` 依附于 CoreDevice 的 RemoteXPC 隧道，而设备侧的 `remoted` 服务**只在 USB 虚拟网卡的 link-local 地址上监听并广播**。在 macOS 上可以直接观察到：

```
$ dns-sd -L ncm _remoted._tcp local
ncm._remoted._tcp.local. can be reached at iPhone15-Pro.local.:55109 (interface 34)

$ ifconfig anri1          # interface 34
anri1: inet6 fe80::fcae:76ff:fe72:f8b7%anri1    ← 只有 link-local
```

这带来三个无法绕开的后果：

1. **VPN 到不了。** 即使手机和电脑同在一个 Tailscale/WireGuard 网络里、`tailscale ping` 完全正常，手机的 tailnet 地址上这些端口全是关闭的——包不是送不到，是没有进程在那一侧监听。iOS 无 root，也没法自己做端口转发把它暴露出来。
2. **发现机制是链路层的。** Wi-Fi 模式依赖 bonjour 多播找 `_remotepairing._tcp`，而 overlay 网络是三层的，不转发多播。
3. **会话不能预设。** DVT 连接一断，定位立刻回真实值——`--_hold-session` 这个后台进程存在的全部理由就是这个。所以"出门前在家里设好"不成立。

结论：改定位的那一刻，必须有一台能跑 `tunneld` 的主机通过 USB（或同一二层网络）连着设备。这个方案做的事情就是把那台主机缩小到能揣兜里。

顺带一提，这套限制是 Apple 有意设计的：用网络拓扑当权限边界，把"操作者在设备旁边"这件事交给 L2 可达性去证明，而不是靠账号和凭据。好处是不需要任何身份体系就能挡住远程攻击；代价就是你即使已经配对过、拥有完整信任关系，也依然被排除在外——"在场"被定义成了物理邻近，你的凭据不参与这个判断。下面所有的麻烦，本质上都是在为这个定义买单。

## 硬件

| 项目 | 建议 | 说明 |
|------|------|------|
| 主板 | Raspberry Pi Zero 2 W | 四核 A53 / 512MB，跑得动 pymobiledevice3；体积最小。只有 2.4 GHz Wi-Fi，见网络一节 |
| 存储 | 16GB+ microSD | Class 10 以上，最好带 A1/A2 标识，低速卡会让 Python 启动很慢 |
| USB 线 | micro-USB(OTG) 转 USB-A 母口 + 数据线 | Zero 2 W 的中间那个口才是 OTG 数据口，靠边的是纯供电口 |
| 电源 | 充电宝，2.5A 以上 | 见下方供电说明 |

### 选板

只跑 SimLocation 的话 512MB 够用：tunneld、后台会话和地图服务三个 Python 进程加系统底座，估算在 250–300MB。要是这块板以后还想顺手跑别的 Linux 小程序（静态二进制、Docker、几个常驻服务），内存就是第一道门槛，直接上 1–2GB 的型号。下面四块板都是 aarch64，同一份二进制都能跑，差别只在内存、性能和体积。

| 板卡 | 内存 | 官方价 | Wi-Fi | USB host 口 | 典型电流 / 推荐电源 | 适合 |
|------|------|--------|-------|-------------|---------------------|------|
| Zero 2 W | 512MB | $15 | 仅 2.4 GHz | 1× micro-USB OTG | 350mA / 2A | 只做随身定位盒 |
| 3A+ | 512MB | $25 | 双频 | 1× USB-A | 350mA / 2.5A | 想避开 2.4 GHz 热点设置、仍要小体积 |
| Pi 4 B 2GB | 2GB | $55 | 双频 | 4× USB-A，合计 1.2A | 600mA / 3A | 兼顾其他 Linux 小程序，预算优先 |
| Pi 5 2GB | 2GB | $65 | 双频 | 4× USB-A，合计 1.6A | 800mA / 5A | 兼顾其他 Linux 小程序，性能优先，可接 NVMe |

价格是 2026 年 9 月的官方美元定价。Zero 和 Pi 3 系列用 LPDDR2 库存，价格未动，官方承诺供货到 2030 年 1 月。Pi 4/5 则被 LPDDR4 涨价推高，官方表态内存降价后会回调。

#### 内存涨价把「甜品点」抹平了

Pi 5 现在有 1/2/4/8/16GB 五档（1GB 是涨价期间新开的低配档）。The Pi Hut 2026 年 9 月的含税零售价：

| 容量 | 售价 | 比上一档贵 |
|------|------|-----------|
| 1GB | £43.20 | — |
| 2GB | £62.40 | +£19.20 |
| 4GB | £105.60 | +£43.20 |
| 8GB | £168.00 | +£62.40 |
| 16GB | £292.80 | +£124.80 |

把这五个点拟合一下，是一条几乎完美的直线：**裸板约 £24，每 GB 再加约 £19**。正常年份 DRAM 大概 £2–3/GB，"多花几十块升一档"才成立；现在内存按线性计价，**不存在性价比拐点**——每一档都在为内存付同样的单价，多买不打折。

所以「几个 G 是甜品点」这个问题在当前行情下没有答案，只能倒过来问「你真的要多少」。对本方案：三个 Python 进程加系统底座 250–300MB，**2GB 绰绰有余，1GB 也能跑**（Lite 系统约 200MB，剩下的留给页缓存）。4GB 要多付 £43、贵 69%，除非你已经想好了别的常驻服务，否则这笔钱买的是闲置内存。

也可以用任何有 USB host 口的 Linux 小板子（Orange Pi Zero 3、Radxa Zero 3W 等）。它们是 Zero 尺寸却有双频和更大内存，代价是系统镜像和内核支持不如树莓派官方省心。步骤相同。

### 供电这件事要认真对待

Pi Zero 2 W 有两个 micro-USB 口，**PWR IN** 进电，**USB** 做 OTG host。当 Pi 作为 host 时，iPhone 会从 Pi 的 5V 取电并开始充电，这份电最终来自你的充电宝。

- 充电宝要能稳定输出 **2.5A 以上**。Pi 本身负载约 2–3W，iPhone 会再拉 500mA–1A。
- 供电不足的典型症状是 Pi 在 iPhone 插入的瞬间重启，或者 `tunneld` 建立隧道到一半掉线。遇到这种情况先换电源，不要先怀疑软件。
- 更稳但更笨重的做法：用一个**带独立供电的 USB hub**，Pi 和 iPhone 分别从 hub 取电，彼此不抢。
- 充电宝要选支持**小电流常开**的型号。Zero 2 W 待机只有一百多毫安，不少充电宝会把这判定为「没有负载」，几十秒后自动断电，表现为 Pi 莫名关机。
- 反过来让 iPhone 15 给 Pi 供电（USB-C 反向充电 4.5W）在功率上勉强够，但 Zero 2 W 是 micro-USB，不支持 PD 的角色协商，做不到"供电方向和数据方向相反"。别在这上面浪费时间。

#### 用 Pi 5 时的额外注意

官方文档里有两句话决定了充电宝能不能用：「All models require a 5.1V supply」和「No Raspberry Pi models support USB-PPS」。

**充电宝上的 9V/12V 档对树莓派全系无效。** Pi 5 的 PD 协商只请求 5V，拿不到 5A 就退到 5V/3A，高压档永远不会被选中。所以一个标着「5V3A / 9V2.22A」的充电宝，对 Pi 5 来说就是个 15W 的 5V/3A 电源，那个 20W 是虚的。

官方的典型功耗表：

| | 推荐 PSU | USB 外设总电流上限 | 裸板典型电流 |
|---|---|---|---|
| Pi 5 | 5.0A | 1.6A（**用 3A 电源时降到 600mA**） | 800mA |
| Zero 2 W | 2A | 仅受电源和接口限制 | 350mA |

关键在括号里那句：Pi 5 只有在检测到 5V/5A（25W）电源时才放开 1.6A 的 USB 供电，接任何其他电源都会把下游 USB 锁死在 **600mA**。iPhone 在数据连接下按 USB 2.0 标准取 500mA，刚好卡在限额内——能用，但没有余量，而且这 600mA 是**四个 USB 口共享**的，插第二个设备就会翻车。

功率本身不是瓶颈：800mA（板）+ 500mA（手机）≈ 6.5W，15W 电源绰绰有余。卡住你的是固件那道 600mA 的闸。真要放开，固件有个开关，可以先查当前状态：

```bash
vcgencmd get_config usb_max_current_enable
```

在 `/boot/firmware/config.txt` 里设 `usb_max_current_enable=1` 能强制解锁 1.6A。代价是绕过了那道保护——它本来是防止你用小电源带大负载导致掉压。只连一个 iPhone 的话总电流约 1.8A / 9W，15W 电源撑得住；但如果掉到 4.63V 以下，内核日志会开始报低压警告，那时就该换电源而不是继续加码。

顺带一提，Pi 5 待机 800mA 反而不容易触发充电宝的小电流自动断电，这是它相对 Zero 2 W 的一个意外优势。但代价是同一个充电宝的续航大约只有 Zero 的三分之一，而且 Pi 5 基本必须配散热片或风扇，体积和功耗都还要再往上走——揣兜里这件事就别指望了。

## 系统准备

用 Raspberry Pi Imager 刷 **Raspberry Pi OS Lite (64-bit)**。必须是 64 位：pymobiledevice3 的几个依赖只发布 aarch64 的 wheel，32 位系统装不上。在 Imager 的高级选项里预先配好：用户名、主机名、SSH 公钥登录、以及**手机个人热点的 Wi-Fi 名和密码**（这一步很关键，见下文网络部分）。记住这里设的用户名，后面 systemd 单元里要用；新镜像已经没有默认的 `pi` 用户。

首次开机后：

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip usbmuxd libimobiledevice-utils git \
    build-essential python3-dev libssl-dev
```

后三个是编译依赖。Bookworm 自带 Python 3.11，pymobiledevice3 依赖的 `sslpsk-pmd3` 没有 aarch64 wheel，会在 `pip install` 时现场编译；Trixie 自带 Python 3.13，这个依赖不再安装，编译工具装了也无妨。

`usbmuxd` 是 Linux 上与 iOS 设备通信的基础服务。Debian 的包不需要也不能 `systemctl enable`：它的 unit 没有 `[Install]` 段，由 udev 在检测到 Apple 设备时按需拉起，拔掉最后一台设备后自动退出。所以没插手机时它显示 inactive 是正常的，要验证就插上手机再看：

```bash
systemctl status usbmuxd        # 插着 iPhone 时应为 active (running)
```

## 安装

Raspberry Pi OS Bookworm 之后启用了 PEP 668，系统 Python 不允许直接 `pip install`，必须用虚拟环境：

```bash
sudo mkdir -p /opt/simlocation
sudo chown "$USER" /opt/simlocation
python3 -m venv /opt/simlocation/venv
/opt/simlocation/venv/bin/pip install -U pip
/opt/simlocation/venv/bin/pip install -U pymobiledevice3 requests

git clone <你的仓库地址> /opt/simlocation/app
```

让启动器用这个虚拟环境里的解释器。写进 `/etc/environment`：`~/.bashrc` 只对交互式 shell 生效，`ssh 主机名 simlocation clear` 这种一条命令的用法读不到它。

```bash
echo 'SIMLOCATION_PYTHON=/opt/simlocation/venv/bin/python3' | sudo tee -a /etc/environment
sudo ln -s /opt/simlocation/app/bin/simlocation /usr/local/bin/simlocation
```

重新登录后 `simlocation --version` 应能直接运行。启动器会顺着解释器所在目录找到同一虚拟环境里的 `pymobiledevice3` 命令，不需要再设 `SIMLOCATION_PMD3`。

## 配对

配对记录是**按主机**存的，Mac 上信任过不代表 Pi 也信任。用 USB 连上 iPhone，然后：

```bash
sudo /opt/simlocation/venv/bin/pymobiledevice3 usbmux pair
```

手机上会弹出「信任此电脑」，点信任并输入密码。配对记录写在 `/var/lib/lockdown/`，之后不用重复。

确认设备被识别：

```bash
/opt/simlocation/venv/bin/pymobiledevice3 usbmux list
```

设备的开发者模式需要提前开启（设置 → 隐私与安全性 → 开发者模式），这一步在哪台主机上做都一样，开一次即可。

## tunneld 开机自启

`tunneld` 需要 root（要创建 tun 接口），并且必须在 `usbmuxd` 之后启动。

`/etc/systemd/system/tunneld.service`：

```ini
[Unit]
Description=pymobiledevice3 tunneld
After=network.target usbmuxd.service
Wants=usbmuxd.service

[Service]
Type=simple
ExecStart=/opt/simlocation/venv/bin/pymobiledevice3 remote tunneld
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tunneld
curl -s http://127.0.0.1:49151/     # 插着设备时应返回该 UDID 的隧道信息
```

## 网络：让手机能访问到 Pi

**推荐做法是手机开个人热点，Pi 连上去。** 这样手机既是 Pi 的网关，也能直接访问 Pi，整条链路不依赖任何外部网络，也不占用 iOS 的 VPN 槽位。

热点网段固定是 `172.20.10.0/28`，手机是 `172.20.10.1`，Pi 通常拿到 `172.20.10.2` 或 `.3`。在 Imager 里预置过 Wi-Fi 的话，Pi 一开机就会自动连上。

**Zero 2 W 只有 2.4 GHz，而 iPhone 12 起的个人热点默认走 5 GHz。** 要在「设置 → 个人热点」里打开「最大化兼容性」，否则 Pi 永远搜不到这个热点。3A+、Pi 4、Pi 5 是双频板，没有这一步。

> 个人热点在无客户端时会自动关闭，但 Pi 一直连着就不会断。

**Tailscale 作为可选增强。** 好处是换网络时地址不变（`100.64.0.x`），也方便你在家里从 Mac 上管理这块板子：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

注意 iOS 一次只能激活一个 VPN。如果你手机上跑着 Surge，需要让 Surge 通过 WireGuard 出站接入 tailnet，而不是同时开 Tailscale 客户端。热点直连方案没有这个问题，这也是把它作为主路径的原因。

## 上电即用

配合固定端口和固定令牌，可以让 Pi 一开机就提供地图选点服务，手机加个书签就能用，连 SSH 都省了。

`/etc/systemd/system/simlocation-map.service`：

```ini
[Unit]
Description=SimLocation remote map picker
After=tunneld.service
Wants=tunneld.service

[Service]
Type=simple
User=换成你在 Imager 里设置的用户名
KillMode=process
SuccessExitStatus=1
Environment=SIMLOCATION_PYTHON=/opt/simlocation/venv/bin/python3
Environment=SIMLOCATION_MAP_LISTEN=0.0.0.0
Environment=SIMLOCATION_MAP_PORT=8765
Environment=SIMLOCATION_MAP_TIMEOUT_SECONDS=3600
Environment=SIMLOCATION_MAP_TOKEN=换成你自己的随机串
ExecStart=/opt/simlocation/app/bin/simlocation map --remote
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

用 `openssl rand -base64 24 | tr -d '/+='` 生成令牌填进去。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now simlocation-map
```

`KillMode=process` 是这个单元里最容易漏、漏了又最难看出来的一行。选点完成后 `simlocation map` 会拉起 `--_hold-session` 后台进程来维持定位，然后自己退出。systemd 默认的 `KillMode=control-group` 会在主进程退出时把 cgroup 里剩下的进程一起 SIGTERM，后台会话一死，定位立刻回到真实位置，看起来就是「设上了又马上掉」。改成 `process` 后 systemd 只管主进程，后台会话留下来继续跑。`SuccessExitStatus=1` 则是因为一小时没人选点时服务以 1 退出，不加这行日志里每小时多一条 failed。副作用是 `set` 失败时同样退出码 1，也会被记成正常退出，所以判断有没有设成功要看 journal 里的输出，别看单元状态。

`Restart=always` 是这里的关键：选点完成后进程会退出并设置定位，systemd 立刻拉起一个新的选点服务。因为端口和令牌都固定，手机上那个书签**永远有效**：

```
http://172.20.10.2:8765/?t=你的令牌
```

### 关于固定令牌的权衡

监听 `0.0.0.0` 意味着同网段的任何设备都能访问这个服务，所以 URL 本身就是凭据——没有正确令牌的请求一律 403。

默认行为是每次生成一次性令牌，安全性更高但无法书签化。固定令牌换来了"上电即用"，代价是这个串一旦泄露，在你换掉它之前一直有效。在个人热点这种只有你自己设备的网段里，这个权衡是划算的；如果 Pi 会接入公共 Wi-Fi，就去掉 `SIMLOCATION_MAP_TOKEN`，改回每次 SSH 上去手动启动。

## 日常使用

1. 充电宝给 Pi 供电，等待约 30 秒开机
2. USB 线连接 Pi 和 iPhone，手机保持解锁
3. 手机打开个人热点，Pi 自动连接
4. 手机浏览器打开书签，地图上选点，点「确认位置」
5. 打开地图 App 核对一下。选点页收到确认就关闭了，不会告诉你后面的 `set` 有没有成功；没生效就 `journalctl -u simlocation-map -n 30` 看原因

需要清除定位时，SSH 到 Pi 执行 `simlocation clear`，或者在手机上用 a-Shell / Termius 之类的客户端。`systemctl stop simlocation-map` 只会停掉选点页，后台会话和已设置的定位都会保留。

## 故障排查

Pi 上同样可以用内置的诊断命令，它是只读的：

```bash
simlocation doctor
```

| 症状 | 排查方向 |
|------|----------|
| 插上 iPhone 时 Pi 重启 | 供电不足，换更强的充电宝或用带供电的 hub |
| `usbmux list` 看不到设备 | 确认用的是 OTG 口而非纯供电口；`systemctl status usbmuxd`；换一根确定能传数据的线 |
| 提示未配对 | 重新执行 `pymobiledevice3 usbmux pair`，注意手机要解锁 |
| tunneld 返回空 | `journalctl -u tunneld -n 50`；确认服务以 root 运行 |
| Pi 搜不到手机热点 | Zero 2 W 只支持 2.4 GHz，打开热点的「最大化兼容性」 |
| 手机打不开地图页面 | 确认 Pi 已连上热点（`ip addr show wlan0`），确认端口和令牌与书签一致 |
| 地图页面空白 | 页面的地图瓦片从公网 CDN 加载，确认手机本身有蜂窝网络 |
| 定位设上了又马上回到真实位置 | 单元文件缺 `KillMode=process`，systemd 把后台会话一起杀了；`journalctl -u simlocation-map` 里能看到 |
| 定位用了一阵才失效 | 查 `journalctl -u tunneld`，通常是 USB 接触不良导致隧道断开 |
| `pip install` 编译报错 | 确认是 64 位系统，且装了 `build-essential python3-dev libssl-dev` |
| Pi 运行几十秒后自动断电 | 充电宝小电流保护，换支持常开的型号 |

## 这个方案的边界

说实话一点：它没有真正做到"什么都不用带"，只是把要带的东西从笔记本换成了一块板子、一根线和一个充电宝。如果你要的是完全不带任何附加设备，那只有设备侧方案（越狱生态的定位注入）能做到，代价是另一套完全不同的风险和维护成本。
