# SimLocation v2.0.0 — 地图选点设计

## 概述

新增 `simlocation map` 子命令，在浏览器中打开高德地图页面，用户点选位置后坐标自动回传 CLI，默认直接设置定位。

## CLI 变更

```
simlocation set <lat> <lon>          # 原有功能
simlocation clear                    # 原有 --clear
simlocation map                      # 地图选点 → 自动 set
simlocation map --pick-only          # 地图选点 → 只输出坐标
```

向后兼容：`simlocation <lat> <lon>` 和 `simlocation --clear` 继续可用。

## 地图页面

- 文件：`web/map.html`
- 高德地图 JS API，全屏显示，默认中心北京
- 点击放置标记，显示 GCJ-02 和 WGS-84 坐标
- 内置 POI 搜索框
- GCJ-02 → WGS-84 转换在前端 JS 完成（内嵌算法）
- 确认按钮 POST `{lat, lon}`（WGS-84）到本地 server `/confirm`
- 确认后页面提示可关闭

坐标流转：用户点击 (GCJ-02) → JS 转 WGS-84 → POST 本地 → CLI 使用 WGS-84

## 本地 HTTP Server

- 绑定 `127.0.0.1` 随机端口
- `webbrowser.open()` 自动打开
- 端点：
  - `GET /` → 返回 map.html（注入 Amap Key 和端口）
  - `POST /confirm` → 接收坐标，关闭 server
- 5 分钟超时自动退出

## 高德 Key 处理

- 环境变量 `SIMLOCATION_AMAP_KEY`
- 未设置时终端打印申请引导
- README 添加申请步骤

## 文件变更

- 新增：`web/map.html`
- 修改：`bin/simlocation.py`、`README.md`、`VERSION`、`CHANGELOG.md`

## 依赖

无新增 Python 依赖。高德 JS API 由浏览器加载。
