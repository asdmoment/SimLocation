# Changelog

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
