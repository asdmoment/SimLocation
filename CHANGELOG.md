# Changelog

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
