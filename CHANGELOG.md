# Changelog

## v3.1.0

- 修复 `set --device <别名> <纬度> <经度>`、`clear --device <别名>` 等写法中 `--device` 被忽略的问题：共享选项现在写在子命令前后都生效，之前子命令会把 `--device`、`--debug`、`--connection` 重置为默认值，导致操作落到默认设备上。
- 坐标在入口处校验数值和范围，多余参数、未知设备别名会直接报错；交互式选设备在非终端或 Ctrl-C/EOF 时干净退出。
- 获取 RSD 时只使用登记在目标设备 UDID 下的 tunnel，不再回退到快照中其他设备的地址。
- `/start-tunnel` 改为单次长等待请求（45 秒），不再按 usbmux/usb/wifi 循环重试；tunneld 自身已按顺序尝试全部传输方式，重复请求会在 tunneld 内部产生并发的 tunnel 任务。
- `auto` 模式会逐个探测 tunneld 中该设备的所有 tunnel，任一可达即复用。
- 后台保持会话若在建立连接期间就收到停止请求，不再设置定位。
- `status`/`device list` 对进程已退出的 `ready` 会话显示 `stale`。
- `doctor` 在缺少 `pymobiledevice3` CLI 时继续完成其余检查；支持 `--device` 与 `SIMLOCATION_UDID`；列出每个 RSD 的可达性。
- `pymobiledevice3` CLI 查找顺序改为：`SIMLOCATION_PMD3`、当前 Python 环境旁的 CLI（含 Windows `Scripts\`）、PATH。
- 地图选点服务移除通配 CORS 响应头，页面改为相对路径提交，限制请求体大小并校验坐标。
- 新增 `--version`，新增 `SIMLOCATION_TUNNELD_URL` 环境变量。
- 测试从 18 个增加到 58 个；README、AGENTS.md、CLAUDE.md 修正了过时的日志路径、命令形式和测试说明。

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
