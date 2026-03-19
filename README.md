# SimLocation

`SimLocation` 是一个本地命令行工具，用来在 macOS 上通过 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 给已连接的 iPhone 或 iPad 设置模拟定位。

它更适合已经在用 `pymobiledevice3` 和 `tunneld` 的人：想快速切换到一组坐标，保持定位一段时间，或者在测试完之后手动清掉。

## 适合做什么

- 调试依赖定位的 App
- 复现和排查基于经纬度触发的功能
- 演示特定地点的界面或流程
- 做一些需要反复切换坐标的本地测试

## 应用场景

常见用法很直接：把设备连到 Mac，确认 `tunneld` 可用，然后用命令传入一组经纬度开始模拟定位。工具会保持会话，直到你执行清除。

如果你平时总是用同一组私人测试坐标，也可以只在本机环境里设置：`SIMLOCATION_DEFAULT_LAT` 和 `SIMLOCATION_DEFAULT_LON`。这是可选的本地默认值，不建议写进仓库。

## 安装依赖

本项目依赖 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3)，一个纯 Python 实现的 iOS 设备通信库。SimLocation 通过它的 Python API 与设备建立 DVT 会话来实现定位模拟。

安装方式（推荐使用虚拟环境）：

```bash
pip install pymobiledevice3 requests
```

目前兼容 `pymobiledevice3` v8.x 和 v9.x。如果你从旧版本升级后遇到 `ModuleNotFoundError`，请确认升级后的版本已正确安装在当前 Python 环境中。

## 设备准备

在使用之前，需要确保目标 iOS 设备已开启**开发者模式**：

1. 在 iPhone 或 iPad 上进入 **设置 → 隐私与安全性 → 开发者模式**
2. 打开开发者模式开关，按提示重启设备
3. 重启后确认启用开发者模式

> iOS 16 及以上版本需要手动开启开发者模式，否则无法建立开发者服务连接。

## 连接设备

设备开启开发者模式后，需要通过 `tunneld` 建立通信隧道。

**1. 启动 tunneld**

```bash
$ sudo pymobiledevice3 remote tunneld
```

`tunneld` 需要 `sudo` 权限，启动后会在前台运行，保持终端窗口不要关闭。

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
- 检查设备是否已解锁并信任了当前 Mac

## 基本准备

使用前默认你已经具备这些条件：

- 运行环境是 `macOS`
- 有一台已开启开发者模式的 `iPhone` 或 `iPad`
- 已安装 `pymobiledevice3`（`pip install pymobiledevice3`）
- `tunneld` 正常运行，设备已完成配对（详见上方「连接设备」）
- `python3` 已安装 `requests` 和 `pymobiledevice3`，或者你通过 `SIMLOCATION_PYTHON` 指定了对应解释器

入口脚本是 `bin/simlocation`。如果设置了 `SIMLOCATION_PYTHON`，它会优先使用这个 Python；否则会尝试直接使用当前的 `python3`。

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

如果你只想在自己电脑上保留一组常用默认值，可以设置：

```bash
$ export SIMLOCATION_DEFAULT_LAT=<你的纬度>
$ export SIMLOCATION_DEFAULT_LON=<你的经度>
```

设置后，执行 `simlocation` 时可以不再重复输入经纬度；如果没有提供命令行坐标，程序会读取这两个环境变量。

运行时文件默认放在 `var/` 下，包括：

- `var/devices.json` — 设备别名和默认设备配置
- `var/<UDID>.log` — 每台设备的会话日志
- `var/<UDID>.pid` — 每台设备的后台进程 PID
- `var/<UDID>.state.json` — 每台设备的会话状态

如果你想改位置，可以设置 `SIMLOCATION_VAR_DIR`。

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
# 1. 启动 tunneld（另一个终端）
sudo pymobiledevice3 remote tunneld

# 2. 查看已连接设备
simlocation device list

# 3. 给设备注册别名
simlocation device add myphone

# 4. 设为默认设备
simlocation device default myphone

# 5. 设置模拟定位（自动使用默认设备）
simlocation set 39.9042 116.4074

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
$ export SIMLOCATION_AMAP_KEY=你的Key
```

建议将上面这行加入 `~/.zshrc` 或 `~/.bashrc` 以便长期使用。

## 局限性

这个项目面向 `macOS` 上配合 `iPhone` 或 `iPad` 的本地定位测试。

它不是多平台方案，也不打算覆盖 Windows、Linux、Android，或者更通用的设备管理流程。如果你的需求超出这类本地测试场景，可能需要自己扩展，或者换别的工具。

## 致谢

本项目基于 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 实现设备通信和定位模拟功能。`pymobiledevice3` 由 [doronz88](https://github.com/doronz88) 开发维护，是一个纯 Python 实现的 iDevice 通信库。

## 许可证

本项目以 [GNU General Public License v3.0 (GPL-3.0)](LICENSE) 发布。

由于本项目以库的形式使用了 `pymobiledevice3`（GPL-3.0-or-later），根据 GPL 的传染性条款，本项目同样采用 GPL-3.0 许可。详见 [LICENSE](LICENSE) 文件。
