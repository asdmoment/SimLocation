# SimLocation

`SimLocation` 是一个带网页控制台的跨平台命令行工具，通过 [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) 给已连接的 iPhone 或 iPad 设置模拟定位。支持 macOS、Windows 和 Linux，非常适合已经在用 `pymobiledevice3` 和 `tunneld` 的开发者，可用于快速切换坐标、保持定位或做本地自动化测试。

---

## 应用场景

* App 定位调试：调试依赖地理位置的 iOS App。
* 功能排查：复现和排查基于经纬度触发的功能。
* 特定演示：演示特定地点的界面或业务流程。
* 路线与轨迹模拟：模拟跑步、骑行等位置持续变化的场景。

---

## 环境要求与设备准备

使用前请确保你的系统环境（macOS / Windows / Linux）已安装 Python 3。

### 1. 安装依赖库

```bash
pip install pymobiledevice3 requests

```

> 注意：代码保留了 `pymobiledevice3` v8.x 和 v9.x 的 API 兼容分支；网页控制台已在 Linux、`pymobiledevice3` 11.15.4 与 iPhone（iOS 27.0）上完成真机 DVT 测试。若升级后遇到 `ModuleNotFoundError`，请确认已安装在当前使用的 Python 环境中。

### 2. 开启 iOS 开发者模式

iOS 16 及以上版本需要开启开发者模式才能建立调试连接。

1. 进入 iPhone / iPad 的 设置 → 隐私与安全性 → 开发者模式。
2. 打开开关，并按提示重启设备，重启后确认启用。

iOS 默认隐藏此选项，需要触发一次开发者工具连接：

* 方法一：通过 Xcode 触发（需要 Mac）
1. 打开 Mac 上的 Xcode，通过 USB 连接 iPhone。
2. 进入 `Window → Devices and Simulators`，等待 Xcode 识别设备即可出现选项。


* 方法二：通过 pymobiledevice3 触发（无需 Mac）
1. 通过 USB 连接设备并点击“信任此电脑”。
2. 执行命令：`pymobiledevice3 amfi enable-developer-mode`
3. 重启设备并在弹窗中点击“打开”。

> 如保留锁屏密码，在手机上**手动开启 Developer Mode 即可。** Apple 的自动启用流程只支持未设置锁屏密码的设备。

1. 通过 USB 连接设备并点击“信任此电脑”。
2. 如果设置中没有 Developer Mode，运行以下命令显示入口：
   ```bash
   pymobiledevice3 amfi reveal-developer-mode
   ```
3. 在手机上打开 **设置 → 隐私与安全性 → 开发者模式**，开启开关并按提示重启。
4. 重启后点击“打开”，按提示输入锁屏密码。
5. 在电脑上确认状态：
   ```bash
   pymobiledevice3 amfi developer-mode-status
   ```

返回 `true` 即表示启用成功。


### 3. 启动 tunneld 并连接设备

1. 启动 tunneld（保持后台运行）：
```bash
# macOS / Linux (需要管理员权限)
sudo pymobiledevice3 remote tunneld

# Windows (管理员权限终端)
pymobiledevice3 remote tunneld

```


2. 连接并信任电脑：通过 USB 连接设备，解锁屏幕并点击“信任此电脑”。
3. 验证连接：
```bash
pymobiledevice3 usbmux list

```


*能看到设备 UDID 和名称即表示连接成功。*

* 无设备显示：重新插拔 USB，重启 `tunneld`。
* Windows 用户：需安装 [iTunes](https://www.apple.com/itunes/) 或 Apple Devices 以提供 USB 驱动。
* Linux 用户：需确保 `usbmuxd` 服务运行中 (`sudo systemctl start usbmuxd`)。
* 终端警告 `Failed to setupterm(kind='xterm-ghostty')`：运行 `export TERM=xterm-256color` 即可消除，不影响功能。

---

## 快速开始

### 入口脚本与 PATH 配置

| 平台 | 入口脚本路径 |
| --- | --- |
| macOS / Linux | `bin/simlocation` |
| Windows | `bin\simlocation.cmd` |

将 `bin/` 目录添加到 PATH（可选）：

* macOS / Linux：
```bash
export PATH="/path/to/SimLocation/bin:$PATH"
# 或创建软链接
ln -s /path/to/SimLocation/bin/simlocation /usr/local/bin/simlocation

```


* Windows (PowerShell)：
```powershell
$binPath = "C:\SimLocation\bin"
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($currentPath -notlike "*$binPath*") {
    [Environment]::SetEnvironmentVariable("Path", "$currentPath;$binPath", "User")
}

```



### 基础 CLI 定位命令

```bash
# 设置定位（格式：simlocation set <纬度> <经度>）
simlocation set 22.283900 114.158100

# 兼容旧语法：
simlocation 22.283900 114.158100

# 清除定位
simlocation clear
# 兼容旧语法：
simlocation --clear

```

> 校验说明：纬度范围 [-90, 90]，经度范围 [-180, 180]。非法或多余参数将直接报错。

---

## 核心功能指南

### 网页控制台 (Web Console)

图形化可视化界面，支持地图选点、路线绘制、设备监控等完整操作。

```bash
simlocation web

```

| 网页功能 | 对应 CLI 操作 |
| --- | --- |
| 坐标输入 / 搜索选点 / 地图选点 | `set`, `map` |
| 路线绘制、拖动、撤销、导入、调速及循环 | `route`, `--speed`, `--loop` |
| 导出坐标 / 路线 JSON | `map --pick-only`, `route --pick-only` |
| 清除单台或全部设备定位 | `clear`, `clear --all` |
| 设备列表 / 轨迹状态监视 | `device list`, `status` |
| 别名管理 / 默认设备配置 | `device add/remove/default` |
| 系统环境诊断 | `doctor` |

#### 远程访问控制台 (如手机/无头主机)

```bash
simlocation web --remote --port 8765

```

> 网页控制台在本机和远程访问时都需要 Access Token，请使用终端打印的完整 URL。默认令牌在服务重启后更换，可通过 `SIMLOCATION_MAP_TOKEN` 指定固定令牌。坐标和路线草稿保存在 `sessionStorage` 中，刷新后可恢复。

---

### 地图选点 (Map Picking)

在浏览器地图中直接点击选择目标位置：

```bash
# 本地选择并设置
simlocation map

# 仅获取选点坐标 JSON，不设置定位
simlocation map --pick-only

# 远程选点（适合树莓派/无头主机，支持手机浏览器操作）
simlocation map --remote

```

---

### 运动轨迹 (Route Simulation)

模拟路线移动，适合测试跑步、骑行等位置动态变化的 App。

```bash
# 在地图上交互式绘制路线并开始移动
simlocation route

# 加载路线文件移动
simlocation route my-route.json
simlocation route track.gpx --speed 12 --loop

# 手机远程绘制路线
simlocation route --remote --speed 12

```

#### 参数说明

* `--speed N`：移动速度（km/h，默认 5）。
* `--loop`：到达终点后循环回起点。
* `--pick-only`：仅导出路线 JSON。

#### 路线 JSON 格式

```json
{
  "points": [
    [22.283900, 114.158100],
    [22.284700, 114.159400],
    [22.285600, 114.160300]
  ]
}

```

参考用例可参看 [`examples/route.json`](examples/route.json)。

---

### 多设备管理

多设备连接时，可以通过注册别名进行针对性操作：

```bash
# 1. 查看已连接设备
simlocation device list

# 2. 为设备添加别名
simlocation device add myphone

# 3. 设置默认设备
simlocation device default myphone

# 4. 指定设备运行命令
simlocation set --device myphone 22.283900 114.158100
simlocation clear --device myphone

# 5. 查看所有设备的模拟状态
simlocation status

# 6. 一键清除所有设备定位
simlocation clear --all

# 7. 删除设备别名
simlocation device remove myphone

```

---

## 高级配置与环境诊断

### 通用 CLI 选项

所有子命令均可附加以下通用选项：

| 选项 | 说明 |
| --- | --- |
| `-d`, `--device <别名\|UDID>` | 指定目标设备 |
| `--connection auto\|rsd` | `auto`（默认）：自动选择或重建失效 RSD；`rsd`：仅复用现有 RSD |
| `--debug` | 追加日志至 `var/simlocation.log` |

使用 `simlocation --version` 查看软件版本。

### 环境变量一览

你可以在系统环境中设置以下变量来简化命令行输入：

| 环境变量 | 说明 |
| --- | --- |
| `SIMLOCATION_DEFAULT_LAT` | 默认纬度 |
| `SIMLOCATION_DEFAULT_LON` | 默认经度 |
| `SIMLOCATION_UDID` | 指定目标设备 UDID，等效于每条命令都带 `--device`（`--device` 优先级更高） |
| `SIMLOCATION_PYTHON` | 指定使用的 Python 解释器路径 |
| `SIMLOCATION_PMD3` | 指定 `pymobiledevice3` 可执行文件路径（默认优先使用与当前 Python 同目录的那个） |
| `SIMLOCATION_VAR_DIR` | 自定义运行时数据文件存放目录（默认 `var/`） |
| `SIMLOCATION_TUNNELD_URL` | 自定义 `tunneld` 服务地址（默认 `http://127.0.0.1:49151`） |
| `SIMLOCATION_START_TIMEOUT_SECONDS` | 会话启动超时时间（默认 60 秒） |
| `SIMLOCATION_MAP_LISTEN` | 网页服务监听地址（默认 `127.0.0.1`） |
| `SIMLOCATION_MAP_PORT` | 网页服务端口（默认自动分配） |
| `SIMLOCATION_MAP_TOKEN` | 固定访问令牌；网页控制台始终启用鉴权，独立地图选点仅在非 loopback 监听时启用 |
| `SIMLOCATION_MAP_TIMEOUT_SECONDS` | 地图选点服务超时时间（默认 300 秒） |
| `SIMLOCATION_AMAP_KEY` | [高德开放平台](https://console.amap.com/) JS API Key（配置后中国境内优先使用高德地图） |

---

### 系统诊断 (`doctor`)

出现环境异常或连接问题时，运行诊断命令：

```bash
simlocation doctor

```

该命令会检查 Python 环境、`pymobiledevice3`、`tunneld`、设备可达性以及后台会话状态。

* `[-]`：阻止工作的致命错误。
* `[!]`：不影响核心功能的警告提示。

---

### 运行机制与 Tunnel 策略

* 运行时文件管理（存储于 `var/` 或 `SIMLOCATION_VAR_DIR`）：
* `devices.json`：存储别名与默认设备配置。
* `<UDID>.pid`：后台定位进程 PID。
* `<UDID>.state.json`：设备会话状态。
* `simlocation.log`：调试日志。


* Tunnel 重建机制：
在 `auto` 模式下，SimLocation 会探测已有的 RSD Tunnel。若连接超时/失效（如插拔设备后），SimLocation 会主动请求 `tunneld` 清除坏连接并按 USB（10s）→ Wi-Fi（45s）顺序重新建立连接。

---

## 平台支持

| 平台 | 兼容性 | 备注 |
| --- | --- | --- |
| macOS | 完整支持 | 原生开发平台 |
| Windows | 支持 | 需安装 iTunes 或 Apple Devices 驱动；`clear` 会终止进程并通过新连接清除 |
| Linux | 支持 | 需要 `usbmuxd` 系统服务正常运行 |

> 注：本项目专注于配合 iPhone / iPad 的 iOS 本地定位测试，暂不覆盖 Android 平台。

---

## 致谢

* [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 由 [doronz88](https://github.com/doronz88) 开发与维护，提供底层设备通信与定位模拟支持。
* 运动轨迹与 Web 前端由 [hoicau](https://github.com/hoicau) 贡献。

## 许可证

本项目以 GNU General Public License v3.0 (GPL-3.0) 发布。
