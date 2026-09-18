# SimLocation

`SimLocation` 是一个跨平台命令行工具，通过 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 给已连接的 iPhone 或 iPad 设置模拟定位。支持 macOS、Windows 和 Linux。

它更适合已经在用 `pymobiledevice3` 和 `tunneld` 的人：想快速切换到一组坐标，保持定位一段时间，或者在测试完之后手动清掉。

## 适合做什么

- 调试依赖定位的 App
- 复现和排查基于经纬度触发的功能
- 演示特定地点的界面或流程
- 做一些需要反复切换坐标的本地测试

## 应用场景

常见用法很直接：把设备连到电脑，确认 `tunneld` 可用，然后用命令传入一组经纬度开始模拟定位。工具会保持会话，直到你执行清除。

如果你平时总是用同一组私人测试坐标，也可以只在本机环境里设置：`SIMLOCATION_DEFAULT_LAT` 和 `SIMLOCATION_DEFAULT_LON`。这是可选的本地默认值，不建议写进仓库。

## 安装依赖

本项目依赖 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3)，一个纯 Python 实现的 iOS 设备通信库。SimLocation 通过它的 Python API 与设备建立 DVT 会话来实现定位模拟。

安装方式（推荐使用虚拟环境）：

```bash
pip install pymobiledevice3 requests
```

代码保留了 `pymobiledevice3` v8.x 和 v9.x 的 DVT 导入兼容路径，已在 Linux + `pymobiledevice3` 11.15.4 上完成真机验证。如果升级后遇到 `ModuleNotFoundError`，请确认依赖已安装在当前 Python 环境中。

如果通过 `pipx` 安装，系统 `python3` 可能无法导入该环境中的依赖。请将 `SIMLOCATION_PYTHON` 指向 pipx 环境的 Python，并确认该解释器同时安装了 `requests`。

## 设备准备

在使用之前，需要确保目标 iOS 设备已开启**开发者模式**。

### 开启开发者模式

1. 在 iPhone 或 iPad 上进入 **设置 → 隐私与安全性**
2. 找到 **开发者模式** 开关并打开
3. 按提示重启设备，重启后确认启用

> iOS 16 及以上版本需要手动开启开发者模式，否则无法建立开发者服务连接。

### 找不到"开发者模式"选项？

很多人在「隐私与安全性」里看不到"开发者模式"这个选项，这是正常的 — **iOS 默认隐藏这个开关**，需要触发一次开发者工具连接才会出现。

**方法一：通过 Xcode 触发（需要 Mac）**

1. 在 Mac 上安装 [Xcode](https://apps.apple.com/app/xcode/id497799835)（App Store 免费下载）
2. 用 USB 线连接 iPhone 到 Mac
3. 打开 Xcode → Window → Devices and Simulators
4. 等待 Xcode 识别设备（首次可能需要几分钟下载调试支持文件）
5. 回到 iPhone 的 **设置 → 隐私与安全性**，"开发者模式"选项应该已经出现

> 不需要真的用 Xcode 写代码，只要让它识别一次设备就行。识别后可以关闭 Xcode。

**方法二：通过 pymobiledevice3 显示开发者模式入口（无需 Mac）**

如果你没有 Mac 或不想装 Xcode，可以用 pymobiledevice3 显示设置中的开关：

```bash
# 1. 先用 USB 连接设备，确保已点击"信任此电脑"
# 2. 执行以下命令（不需要 tunneld）
pymobiledevice3 amfi reveal-developer-mode
```

执行后进入「设置 → 隐私与安全性 → 开发者模式」开启开关，按提示重启。重启后解锁设备，在确认对话框中点击「打开」，按要求输入锁屏密码。

设备设有锁屏密码时，iOS 会拒绝 `amfi enable-developer-mode` 的远程启用请求；通过上述设置入口完成启用即可。

> 如果命令报错，请确认设备已通过 USB 连接并信任了当前电脑。Windows 需要先安装 iTunes。

## 连接设备

设备开启开发者模式后，需要通过 `tunneld` 建立通信隧道。

**1. 启动 tunneld**

```bash
# macOS / Linux（需要管理员权限）
$ sudo pymobiledevice3 remote tunneld

# Windows（以管理员身份运行命令提示符或 PowerShell）
> pymobiledevice3 remote tunneld
```

`tunneld` 需要管理员权限，启动后会在前台运行，保持终端窗口不要关闭。

**2. 用 USB 连接设备**

第一次连接时，设备会弹出「信任此电脑？」的对话框，点击「信任」并输入锁屏密码。

**3. 验证连接**

```bash
$ pymobiledevice3 usbmux list
```

如果能看到你的设备信息（UDID、设备名等），说明连接正常。

**排查连接问题**

如果没有设备显示：

- 拔掉 USB 线重新插入
- 在终端中重启 `tunneld`（Ctrl+C 停止后重新运行）
- 检查设备是否已解锁并信任了当前电脑
- Windows 用户需确认已安装 [iTunes](https://www.apple.com/itunes/) 或 Apple Devices（提供 USB 驱动）
- Linux 用户需确认 `usbmuxd` 服务正在运行（`sudo systemctl start usbmuxd`）
- Linux 上若 tunneld 能看到设备，但连接 RSD 报 `Network is unreachable`，请检查 IPv6 policy routing（`ip -6 rule`）和目标地址的路由。代理 TUN 的 `unreachable` 规则可能阻断 iPhone tunnel，需要为设备 tunnel 配置路由例外。
- 如果看到 `Failed to setupterm(kind='xterm-ghostty')` 警告，这是 pymobiledevice3 的依赖库不认识 Ghostty 终端，不影响功能。可通过 `export TERM=xterm-256color` 消除警告

## 基本准备

使用前默认你已经具备这些条件：

- 运行环境是 macOS、Windows 或 Linux
- 有一台已开启开发者模式的 `iPhone` 或 `iPad`
- 已安装 `pymobiledevice3`（`pip install pymobiledevice3`）
- `tunneld` 正常运行，设备已完成配对（详见上方「连接设备」）
- `python3` 已安装 `requests` 和 `pymobiledevice3`，或者你通过 `SIMLOCATION_PYTHON` 指定了对应解释器

**入口脚本：**

| 平台 | 入口 |
|------|------|
| macOS / Linux | `bin/simlocation` (shell wrapper) |
| Windows | `bin\simlocation.cmd` |

如果设置了 `SIMLOCATION_PYTHON`，它会优先使用这个 Python；否则会尝试使用 `python3`（Windows 上也会尝试 `python`）。

## 添加到 PATH（可选）

如果想在任何目录下直接运行 `simlocation` 命令，可以将 `bin/` 目录添加到系统 PATH：

**macOS / Linux**

```bash
# 添加到 shell 配置文件（~/.zshrc 或 ~/.bashrc）
export PATH="/path/to/SimLocation/bin:$PATH"

# 或者创建符号链接到已有的 PATH 目录
ln -s /path/to/SimLocation/bin/simlocation /usr/local/bin/simlocation
```

**Windows**

方法一：添加到用户 PATH 环境变量

1. 按 Win+R，输入 `sysdm.cpl`，回车
2. 切换到「高级」选项卡 → 「环境变量」
3. 在「用户变量」中找到 `Path`，点击「编辑」
4. 添加 SimLocation 的 `bin` 目录完整路径（如 `C:\SimLocation\bin`）

方法二：在 PowerShell 中快速设置（仅当前用户）

```powershell
# 永久添加到用户 PATH
$binPath = "C:\SimLocation\bin"
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($currentPath -notlike "*$binPath*") {
    [Environment]::SetEnvironmentVariable("Path", "$currentPath;$binPath", "User")
}
# 重新打开终端生效
```

设置后即可在任意目录下使用 `simlocation` 命令（Windows 上会自动匹配 `simlocation.cmd`）。

## 坐标用法

最直接的方式是每次显式传入坐标：

```bash
$ simlocation set <纬度> <经度>
```

> 为了向后兼容，`simlocation <纬度> <经度>`（不带 `set`）也同样有效。

纬度必须在 `-90` 到 `90` 之间，经度必须在 `-180` 到 `180` 之间。无效坐标、未知参数和多余参数会在连接设备前报错。

清除模拟定位：

```bash
$ simlocation clear
```

> 为了向后兼容，`simlocation --clear` 也同样有效。

如果你只想在自己电脑上保留一组常用默认值，可以设置：

```bash
# macOS / Linux
$ export SIMLOCATION_DEFAULT_LAT=<你的纬度>
$ export SIMLOCATION_DEFAULT_LON=<你的经度>
```

```cmd
:: Windows (CMD)
> set SIMLOCATION_DEFAULT_LAT=<你的纬度>
> set SIMLOCATION_DEFAULT_LON=<你的经度>
```

设置后，执行 `simlocation` 时可以不再重复输入经纬度；如果没有提供命令行坐标，程序会读取这两个环境变量。

运行时文件默认放在 `var/` 下，包括：

- `var/devices.json` — 设备别名和默认设备配置（首次运行自动创建，格式参见 `var/devices.example.json`）
- `var/<UDID>.log` — 每台设备的会话日志
- `var/<UDID>.pid` — 每台设备的后台进程 PID
- `var/<UDID>.state.json` — 每台设备的会话状态

如果你想改位置，可以设置 `SIMLOCATION_VAR_DIR`。

## 运动轨迹

在地图上依次选择途经点，以指定速度模拟移动：

```bash
simlocation route --speed 5
```

点击地图或搜索结果添加途经点，拖动标记调整位置。可以撤销、重新绘制，或点击「保存路线」下载 JSON；至少选择两个不同的位置后，点击「开始移动」。配置了高德 Key 时使用高德地图，坐标自动转换为 WGS-84。

也可以直接运行路线文件。仓库提供了一个[示例路线](examples/route.json)：

```bash
# 在仓库根目录运行示例
bin/simlocation route examples/route.json --speed 5

# 读取 GPX，循环移动
simlocation route walk.gpx --speed 8 --loop

# 指定设备、查看进度、结束模拟定位
simlocation route walk.gpx --device myphone --speed 5
simlocation status
simlocation clear --device myphone
```

`--speed` 的单位是 **km/h**，默认 `5`，支持大于 `0` 且不超过 `1000` 的数值。位置约每秒更新一次，按实际经过的时间计算距离。途经点之间直接连线，不会自动沿道路规划路线。

默认走完后保持在终点，直到执行 `clear`。`--loop` 会增加从终点返回起点的线段，连续循环。再次运行 `route` 或 `set` 会替换同一设备的当前会话；`clear --all` 也可以结束运动轨迹。

路线文件使用 WGS-84，JSON 中每个点的顺序是 `[纬度, 经度]`：

```json
{"points": [[1.2868, 103.8545], [1.2871, 103.8545], [1.2871, 103.8548]]}
```

GPX 支持单个连续的 `trkseg` 或 `rte`，按文件中的点顺序移动，速度由 `--speed` 决定；时间戳和海拔不参与回放。最多支持 10000 个点，文件不超过 4 MiB。

只编辑和保存路线、不连接设备时，使用 `simlocation route --pick-only`。

## 多设备管理

连接多台设备时，可以用别名来管理和指定目标设备。

**发现设备**

```bash
$ simlocation device list
```

列出当前 `tunneld` 能看到的所有已连接设备。

**注册别名**

```bash
$ simlocation device add <别名> [UDID]
```

给设备取一个好记的名字。省略 UDID 时会交互式选择。

**设置默认设备**

```bash
$ simlocation device default <别名或UDID>
```

设置后，`set`、`clear`、`map` 等命令不指定 `--device` 时会自动使用这台设备。

查看当前默认设备：

```bash
$ simlocation device default
```

**指定设备操作**

在 `set`、`clear`、`map` 等命令上通过 `--device`（`-d`）指定目标设备：

```bash
$ simlocation set --device <别名> <纬度> <经度>
$ simlocation clear --device <别名>
```

`--device`、`--connection`、`--debug` 和 `--log-file` 可以放在子命令前后，例如 `simlocation --device <别名> clear` 与上面的清除命令等效。

使用 `--connection rsd` 复用现有 tunnel 时，只会匹配目标设备的 UDID；目标设备没有可用 tunnel 时会重试并报错。

每轮连接 RSD 时会对同一地址最多探测 3 次，失败后间隔 1.5 秒重试，以等待新 tunnel 的路由就绪。持续不可达时会明确报错。

切换定位或清除定位前，会先确认旧后台会话已停止。旧会话停止失败时，本次操作会报错退出。新会话启动失败或超时后，会终止并回收本次创建的进程；回收失败会明确报错。

**查看所有会话状态**

```bash
$ simlocation status
```

显示所有设备的定位模拟状态。

**批量清除**

```bash
$ simlocation clear --all
```

一次性清除所有设备的模拟定位。

**删除别名**

```bash
$ simlocation device remove <别名>
```

**典型工作流**

```bash
# 1. 启动 tunneld（另一个终端，需要管理员权限）
# macOS/Linux: sudo pymobiledevice3 remote tunneld
# Windows: 以管理员身份运行 pymobiledevice3 remote tunneld

# 2. 查看已连接设备
simlocation device list

# 3. 给设备注册别名
simlocation device add myphone

# 4. 设为默认设备
simlocation device default myphone

# 5. 设置模拟定位（自动使用默认设备）
simlocation set <latitude> <longitude>

# 6. 用完清除
simlocation clear
```

## 地图选点

除了手动输入坐标，你还可以通过浏览器地图来选择位置：

```bash
$ simlocation map
```

运行后会在浏览器中打开地图页面。在地图上点击选择位置，确认后会自动设置虚拟定位。

默认使用 OpenStreetMap（无需任何配置）。如果配置了高德 Key，则自动切换到高德地图，中国地区显示更精细。

如果你只想获取坐标而不设置定位（比如用于脚本）：

```bash
$ simlocation map --pick-only
```

### 配置高德 Key（可选）

如果你需要更精细的中国地图体验，可以配置一个免费的高德 JS API Key：

1. 前往 [高德开放平台](https://console.amap.com/) 注册或登录
2. 进入「应用管理」→「我的应用」→ 创建新应用
3. 为应用添加一个 Key，服务平台选择「Web端(JS API)」
4. 复制 Key，设置环境变量：

```bash
# macOS / Linux
$ export SIMLOCATION_AMAP_KEY=你的Key
```

```cmd
:: Windows (CMD)
> set SIMLOCATION_AMAP_KEY=你的Key
```

建议将上面的设置持久化（macOS/Linux: 写入 `~/.zshrc` 或 `~/.bashrc`；Windows: 通过系统环境变量设置）以便长期使用。

## 平台支持

| 平台 | 状态 | 备注 |
|------|------|------|
| macOS | 完整支持 | 原生开发平台 |
| Windows | 支持 | 需安装 iTunes 或 Apple Devices 提供 USB 驱动 |
| Linux | 支持 | 需要 usbmuxd 服务运行 |

## 开发验证

无需安装设备通信依赖或连接设备，即可运行 regression tests：

```bash
python3 -B -m unittest discover -s tests -v
```

测试覆盖参数解析、USB 设备发现、目标设备 RSD 匹配、连接重试、后台进程生命周期，以及路线读取、插值、速度和循环。设备通信使用 mock；POSIX 平台另有真实本地子进程的超时回收检查。

地图编辑逻辑可用 `node tests/test_map_routes.js` 验证，无需安装 npm 依赖。

## 致谢

本项目基于 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 实现设备通信和定位模拟功能。`pymobiledevice3` 由 [doronz88](https://github.com/doronz88) 开发维护，是一个纯 Python 实现的 iDevice 通信库。

## 许可证

本项目以 [GNU General Public License v3.0 (GPL-3.0)](LICENSE) 发布。
