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

目前兼容 `pymobiledevice3` v8.x 和 v9.x。如果你从旧版本升级后遇到 `ModuleNotFoundError`，请确认升级后的版本已正确安装在当前 Python 环境中。

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

**方法二：通过 pymobiledevice3 命令触发（无需 Mac）**

如果你没有 Mac 或不想装 Xcode，可以用 pymobiledevice3 直接启用：

```bash
# 1. 先用 USB 连接设备，确保已点击"信任此电脑"
# 2. 执行以下命令（不需要 tunneld）
pymobiledevice3 amfi enable-developer-mode
```

执行后手机会提示重启，重启后在弹出的确认对话框中点击"打开"即可。

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

清除模拟定位：

```bash
$ simlocation clear
```

> 为了向后兼容，`simlocation --clear` 也同样有效。

坐标会在入口处校验：必须是数字，纬度在 -90 到 90 之间，经度在 -180 到 180 之间；多余的参数会直接报错，不会被静默忽略。

### 通用选项

以下选项在所有子命令上都可用，写在子命令前后均可：

| 选项 | 作用 |
|------|------|
| `--device <别名或UDID>` / `-d` | 指定目标设备（见「多设备管理」） |
| `--connection auto\|rsd` | `auto`（默认）优先复用 tunneld 中可达的 RSD，全部不可达时取消它们并重建；`rsd` 只复用现有 RSD，既不取消也不创建 |
| `--debug` | 把操作日志追加写入 `var/simlocation.log`（可用 `--log-file` 改路径） |
| `--version` | 输出当前版本 |

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
- `var/<UDID>.pid` — 每台设备的后台进程 PID
- `var/<UDID>.state.json` — 每台设备的会话状态
- `var/simlocation.log` — 调试日志，仅在传入 `--debug` 时写入

如果你想改位置，可以设置 `SIMLOCATION_VAR_DIR`。

### tunnel 的获取方式

`set` 和 `clear` 只会使用 tunneld 中登记在目标设备 UDID 下的 tunnel，不会借用其他设备的地址。默认的 `auto` 模式先逐个探测这些 tunnel 是否可达，可达就直接复用。

如果登记的 tunnel 全部不可达（常见于设备重新插拔、切换网络之后），SimLocation 会先请求 tunneld 取消这些失效 tunnel，再重新建立。这一步是必需的：tunneld 收到 `/start-tunnel` 时只检查该 UDID 下有没有登记的 tunnel，不检查它是否还能用，所以不取消就会一直拿回同一个坏地址。

重建时按 usbmux（最多 10 秒）、Wi-Fi（最多 45 秒）的顺序逐个尝试，每次只请求一种传输方式。USB 连接的设备通常在 0.3 秒内就能拿到新 tunnel。已超时的请求不会重发，因为 tunnel 任务仍在 tunneld 内部继续跑，重发只会产生互相竞争的任务。

在手机热点或 Wi-Fi tunnel 建立较慢的环境中，后台 DVT 会话可能需要数十秒才能就绪。SimLocation 默认等待 60 秒；如需调整，可以设置：

```bash
export SIMLOCATION_START_TIMEOUT_SECONDS=90
```

该值必须是大于 0 的秒数，并且应大于两次 tunnel 请求的等待上限之和（10 + 45 = 55 秒）。真正超时后，SimLocation 会终止未就绪的后台进程，避免命令报错后又迟到地修改定位。

如果 tunneld 没有跑在默认的 `http://127.0.0.1:49151`，可以用 `SIMLOCATION_TUNNELD_URL` 指定。

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

**查看所有会话状态**

```bash
$ simlocation status
```

显示所有设备的定位模拟状态。如果某台设备的状态是 `ready` 但后台进程已经不在了，会显示为 `stale`，此时设备上的定位可能仍在生效，执行一次 `clear` 即可。

**只读诊断**

```bash
$ simlocation doctor
```

检查当前 Python、`pymobiledevice3` 模块和 CLI、tunneld、目标设备、每个 RSD 的可达性以及后台会话状态。目标设备的选择顺序与其他命令一致：`--device`、`SIMLOCATION_UDID`、默认设备、tunneld 中唯一的设备。`doctor` 不会启动或取消 tunnel，也不会设置或清除设备定位；即使找不到 `pymobiledevice3` CLI 也会继续给出其余检查结果。`[-]` 表示会阻止 SimLocation 工作的问题，`[!]` 表示建议处理但不一定阻断的警告。

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

### 远程选点（无头主机）

当 SimLocation 跑在没有显示器的主机上（树莓派、小型 Linux 盒子、远程服务器），本机浏览器打不开，需要让**另一台设备**来选点：

```bash
$ simlocation map --remote
```

`--remote` 等价于 `--listen 0.0.0.0 --no-browser`。启动后会打印所有可用地址，用手机浏览器打开其中任意一个即可：

```
[*] 请在手机浏览器打开下面任意一个地址，选点后点「确认位置」：
      http://172.20.10.3:8765/?t=GKd5iQlBU39cEW7Q-JgcLw
[*] 该地址包含一次性访问令牌，选点完成或超时后立即失效。
```

监听非 loopback 地址时会**自动生成一次性访问令牌**并附在 URL 上。没有令牌的请求一律返回 403，同网络的其他设备无法在你不知情的情况下改动定位。令牌随进程存在，选点完成或超时后即失效。

可以固定监听地址和端口，便于配合 systemd 或书签：

```bash
$ simlocation map --listen 0.0.0.0 --port 8765 --no-browser
```

远程选点通常需要更长的操作时间（SSH 进去启动、再掏出手机打开浏览器），默认的 300 秒可以调整：

```bash
$ export SIMLOCATION_MAP_TIMEOUT_SECONDS=900
```

> 完整的随身部署方案（树莓派 + USB 连接 + 手机热点）见 [PORTABLE-HOST.md](PORTABLE-HOST.md)。

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
| Windows | 支持 | 需安装 iTunes 或 Apple Devices 提供 USB 驱动；`clear` 会直接终止后台进程，再通过一次新的 DVT 连接补偿清除，因此需要 tunnel 可达 |
| Linux | 支持 | 需要 usbmuxd 服务运行 |

本项目面向配合 iPhone 或 iPad 的本地定位测试，暂时不考虑覆盖 Android 或更通用的设备管理流程。

## 致谢

本项目基于 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 实现设备通信和定位模拟功能。`pymobiledevice3` 由 [doronz88](https://github.com/doronz88) 开发维护，是一个纯 Python 实现的 iDevice 通信库。

## 许可证

本项目以 [GNU General Public License v3.0 (GPL-3.0)](LICENSE) 发布。
