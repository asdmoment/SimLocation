# Changelog

## v3.4.0

- 修复 OSM 地图瓦片可能返回 403：瓦片请求使用官方主机并仅发送 origin 作为 Referer，满足 OSM 要求且不泄露页面访问令牌。
- 新增 `simlocation web` 常驻网页控制台：定点定位、运动轨迹、JSON／GPX 导入导出、多设备与别名管理、实时会话状态、清除定位及只读诊断均可在浏览器操作。支持手机布局和深色模式。
- 网页操作复用现有 CLI 会话与 tunnel 逻辑，通过串行后台任务返回结果和日志；地图继续共用 OSM／高德页面，CLI 和网页共用路线解析。设备配置改为原子写入。
- 常驻控制台的本机与远程访问均需令牌；支持原有监听选项和固定令牌，Ctrl+C 停止网页服务，已有定位会话继续保持。原有 `map`／`route` 一次性选取流程保留。
- 新增网页 API 测试，覆盖认证、输入校验、并发操作、失败恢复、状态和设备管理，并补充两个 provider 的嵌入编辑与坐标转换测试。
- 修复常驻网页的子进程回收：会话退出后及时回收，避免 zombie 被误判为存活，导致清除定位额外等待 8 秒并重复连接设备。新增真实子进程的停止与失败退出回归测试。
- 网页控制台已在 Linux、pymobiledevice3 11.15.4、iPhone 15 Pro（iOS 27.0）完成真机 DVT 测试：设置与替换定位、无效路线隔离、JSON 路线到终点保持、GPX 循环、关闭再打开网页、单台与批量清除，以及后台进程回收。
- `/favicon.ico` 返回 204 而非 403：浏览器每次加载都会无令牌请求它，原先会让一个正常工作的控制台在自己的日志里报错。
- README 补回 `SIMLOCATION_UDID`，并首次补上 `SIMLOCATION_PMD3`（此前只在 `CLAUDE.md` 里有）。
- 已在 macOS + pymobiledevice3 11.15.5 复验：定点、轨迹回放、网页控制台设置与清除均正常。注意 `remote tunneld` 在 11.x 仍需 root，免 root 的原生隧道只覆盖一次性的 `remote start-tunnel`。

## v3.3.0

- 新增运动轨迹：`simlocation route [文件]` 让设备沿一条路线持续移动，复用 `set` 已有的后台 DVT 会话。支持 `--speed`（km/h，默认 5）和 `--loop`；不加 `--loop` 时走到终点会保持在终点，不会跳回真实位置。
- 省略文件时打开地图绘制路线：依次点击添加途经点，可拖动、撤销、重画，面板实时显示途经点数、总里程和预计耗时；「保存路线」可导出 JSON 以后复用。也接受 JSON 路线文件和单段 GPX。
- 途经点之间走大圆路径，位置用球面线性插值计算——在 lat/lon 上走直线会偏离航线，高纬度尤其明显。正对地球两端的路段会被拒绝，因为它们没有唯一的大圆路径。
- 位置按**实际经过的时间**推算，而不是逐次累加步长：某次 DVT 调用变慢时，下一次更新会直接跳到该到的位置，轨迹不会整体落后。
- `status` / `device list` 显示进度、速度和圈数；轨迹与定点定位可以直接相互切换，都用 `clear` 或 `clear --all` 结束。
- 新增远程选点：`simlocation map --remote`（等价于 `--listen 0.0.0.0 --no-browser`）让无显示器的主机把选点页面发给同网络的手机。监听非 loopback 地址会自动生成一次性访问令牌，没有令牌的请求一律 403。`route` 支持同一组选项，在手机上画路线比在电脑上更顺手。新增 `SIMLOCATION_MAP_LISTEN`、`SIMLOCATION_MAP_PORT`、`SIMLOCATION_MAP_TOKEN`、`SIMLOCATION_MAP_TIMEOUT_SECONDS`；随身部署方案见 `PORTABLE-HOST.md`。
- 修复设备发现：`--no-color` 是 pymobiledevice3 的组级选项，9.12 起会直接拒绝 `usbmux list --no-color`，导致 tunneld 不可达时 USB 回退路径完全失效——而那正是它存在的意义。改为 `pymobiledevice3 --no-color usbmux list`，旧版本回退到不带该选项的形式。
- 状态文件改为原子写入（临时文件 + `replace`）：移动中的会话每秒重写一次，`status` 并发读取可能读到写了一半的 JSON。
- GPX 解析改用 `defusedxml`（pymobiledevice3 已依赖它）。标准库 ElementTree 不解析外部实体，但会展开内部实体，1 KB 的文件可以膨胀成几百 MB。
- 地图选点的请求体上限按模式区分：单点仍是 4 KiB，路线模式放宽到 4 MiB。
- 轨迹会话中途失败（设备拔出、DVT 出错）时会尽力清除一次再抛出，不把设备留在假位置上。
- 测试从 71 个增加到 111 个，另有 9 个浏览器侧测试（`tests/test_map_routes.cjs`，用桩替换 Leaflet/高德 SDK 跑真实地图页面）。已在真机（iPhone 15 Pro，pymobiledevice3 9.27.0）验证轨迹回放、循环圈数、终点保持、轨迹与定点互相切换、`clear` 确认和临时文件清理。
- 运动轨迹由 [@hoicau](https://github.com/hoicau) 在 PR #1 中贡献，本版在当前主线上重新实现并合入。

## v3.2.0

- 失效 tunnel 现在可以自动恢复，不再需要手动重启 tunneld：当目标设备在 tunneld 中登记的 tunnel 全部探测不通时，`auto` 模式会先请求 `/cancel` 取消它们，再重新建立。此前 tunneld 收到 `/start-tunnel` 只检查该 UDID 下有没有登记的 tunnel，不检查它是否还能用，所以会一直返回同一个坏地址。
- 重建 tunnel 改为按 usbmux（10 秒上限）、Wi-Fi（45 秒上限）顺序显式指定 `connection_type`，逐个尝试。此前不指定传输方式的单次请求会在 tunnel 已失效时把整个 45 秒耗在 bonjour 扫描里；显式请求 usbmux 时，USB 设备约 0.3 秒即可拿到新 tunnel。超时的请求仍然不会重发，改为回查 tunneld 快照。
- `doctor` 中「现有 RSD 全部不可达」由 error 降为 warn，并说明 `set`/`clear` 会自动取消并重建，退出码不再因此为 1。
- `device list` / `status` 的表格按终端显示宽度对齐，中文和日文别名不再挤掉后面的列。
- 删除 3.0 之前遗留的单设备默认路径 `var/simlocation.pid`、`var/simlocation.state.json`；`clear --all` 会跳过不是以 UDID 命名的状态文件。
- 测试从 60 个增加到 71 个；已在真机（iPhone 15 Pro，pymobiledevice3 9.27.0）验证 `doctor`、`set`、`status`、`clear` 以及失效 tunnel 的取消重建路径。

## v3.1.0

- 修复 `set --device <别名> <纬度> <经度>`、`clear --device <别名>` 等写法中 `--device` 被忽略的问题：共享选项现在写在子命令前后都生效，之前子命令会把 `--device`、`--debug`、`--connection` 重置为默认值，导致操作落到默认设备上。
- 坐标在入口处校验数值和范围，多余参数、未知设备别名会直接报错；交互式选设备在非终端或 Ctrl-C/EOF 时干净退出。
- 获取 RSD 时只使用登记在目标设备 UDID 下的 tunnel，不再回退到快照中其他设备的地址。
- `/start-tunnel` 改为单次长等待请求（45 秒），不再按 usbmux/usb/wifi 循环重试；tunneld 自身已按顺序尝试全部传输方式，重复请求会在 tunneld 内部产生并发的 tunnel 任务。
- `auto` 模式会逐个探测 tunneld 中该设备的所有 tunnel，任一可达即复用。
- 后台保持会话若在建立连接期间就收到停止请求，不再设置定位。
- `status`/`device list` 对进程已退出的 `ready` 会话显示 `stale`。
- `doctor` 在缺少 `pymobiledevice3` CLI 时继续完成其余检查；支持 `--device` 与 `SIMLOCATION_UDID`；列出每个 RSD 的可达性，探测过程不再在报告前额外输出。
- `pymobiledevice3` CLI 查找顺序改为：`SIMLOCATION_PMD3`、当前 Python 环境旁的 CLI（含 Windows `Scripts\`）、PATH。
- 地图选点服务移除通配 CORS 响应头，页面改为相对路径提交，限制请求体大小并校验坐标。
- 新增 `--version`，新增 `SIMLOCATION_TUNNELD_URL` 环境变量。
- 测试从 18 个增加到 60 个；README、AGENTS.md、CLAUDE.md 修正了过时的日志路径、命令形式和测试说明。

## v3.0.1

- 热点/Wi-Fi 场景优先复用可达 RSD，不再无条件取消并重建 tunnel。
- 后台定位启动等待默认延长到 60 秒，并支持 `SIMLOCATION_START_TIMEOUT_SECONDS` 覆盖；真正超时会清理孤儿进程。
- 清除定位由现有 DVT 保持会话写入确认状态，避免实际已清除却因二次 `/start-tunnel` 失败而报错。
- 新增只读 `simlocation doctor`，检查 Python、pymobiledevice3、tunneld、目标设备、RSD 和后台会话。
- 新增标准库 `unittest` 回归测试，覆盖 tunnel 选择、慢启动、clear 确认和 doctor。
- 验证兼容 `pymobiledevice3` 9.12.0 与 9.27.0；推荐运行版本更新为 9.27.0。
- 启动器会优先使用所选 Python 环境内的 `pymobiledevice3` CLI，避免模块与命令行版本错配。

## v3.0.0

- 跨平台支持：新增 Windows 和 Linux 兼容，进程管理、浏览器检测、子进程启动均已适配。
- 新增 `bin/simlocation.cmd` Windows 批处理启动器。
- 新增多设备管理子命令 `device`（`list`/`add`/`remove`/`default`），支持设备别名和 UDID 管理。
- 支持 `--device`（`-d`）全局参数指定目标设备。
- 运行时状态文件按设备 UDID 隔离（`var/<UDID>.state.json`、`var/<UDID>.pid`、`var/<UDID>.log`）。
- 修复 `--help` 被旧版解析器吞掉不显示的问题。
- README 更新为跨平台说明，补充开发者模式开启方法。

## v2.0.0

- 新增 `simlocation map` 子命令：在浏览器中打开高德地图选点页面，点选位置后自动设置虚拟定位。
- 支持 `--pick-only` 模式，仅输出坐标不设置定位，便于脚本集成。
- 内置 GCJ-02 → WGS-84 坐标转换，确保发送给设备的坐标准确。
- CLI 重构为子命令模式（`set`/`clear`/`map`），同时保持 `simlocation <lat> <lon>` 和 `simlocation --clear` 的向后兼容。
- 默认使用 OpenStreetMap（零配置即用），配置 `SIMLOCATION_AMAP_KEY` 后自动切换到高德地图。

## v1.0.1

- 兼容 `pymobiledevice3` v9.x：动态适配 `DvtSecureSocketProxyService`（v8）和 `DvtProvider`（v9）的 import 路径。
- README 补充安装依赖说明、设备开发者模式要求、pymobiledevice3 致谢与链接。
- 采用 GPL-3.0 许可证（与 pymobiledevice3 保持一致）。

## v1.0.0

- Prepared the first public GitHub release.
- Removed repository-embedded default coordinates and switched to optional local private defaults.
- Renamed project surface to `SimLocation`.
- Introduced the `simlocation` CLI.
- Included `pm3-afc-sync.sh` helper in `tools/`.
