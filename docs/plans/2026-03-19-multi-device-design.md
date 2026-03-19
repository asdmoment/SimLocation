# Multi-Device Location Management Design

## Summary

SimLocation 目前是单设备架构（一个 PID 文件、一个 state 文件）。本设计将其扩展为多设备独立会话管理，每台设备各自持有 DVT 会话，互不干扰。

## 文件结构

```
var/
  devices.json                    # 别名 + 默认设备配置
  <UDID>.pid                      # 每台设备独立的 PID 文件
  <UDID>.state.json               # 每台设备独立的 state 文件
```

`devices.json` 结构：

```json
{
  "default": "myphone",
  "aliases": {
    "myphone": "00008130-000845CC01EA001C"
  }
}
```

- `default` 值可以是别名或 UDID
- 旧的 `simlocation.pid` / `simlocation.state.json` 废弃

## 设备解析优先级

确定目标设备的顺序：

1. `--device <别名或UDID>` — 显式指定，最高优先
2. `SIMLOCATION_UDID` 环境变量 — 向后兼容
3. `devices.json` 中的 `default` — 配置的默认设备
4. 自动发现：只有一台设备时自动选中，多台时交互提示选择

交互提示格式：

```
[?] 检测到多台设备，请选择：
  1. myphone (00008130-000845CC01EA001C)
  2. 00008130-000845CC22BB003D
请输入序号:
```

## 新增子命令

### device 子命令组

```
simlocation device list                          # 列出所有设备（发现 + 已注册）
simlocation device add <别名> [UDID]             # 注册别名，UDID 可省略（交互选择）
simlocation device remove <别名>                 # 删除别名
simlocation device default [别名或UDID]          # 设置/查看默认设备
```

### 现有命令扩展

```
simlocation set --device <别名或UDID> <lat> <lon>
simlocation clear --device <别名或UDID>
simlocation clear --all                          # 清除所有设备
simlocation status                               # 所有设备状态概览
```

### status 输出格式

```
  UDID                              别名        默认    状态
  00008130-000845CC01EA001C         myphone     ✓      ready (34.2, 117.1)
  00008130-000845CC22BB003D         —           —      —
```

## README 变更

### 新增章节：连接设备（放在"设备准备"之后）

- 如何启动 tunneld（需要 sudo）
- 首次连接需在设备上信任电脑
- 验证连接：`pymobiledevice3 usbmux list`

### 新增章节：多设备管理（放在"坐标用法"之后）

- 设备注册、别名、默认设备完整用法
- status 和 clear --all 说明
- 从零开始的多设备工作流示例

### 修改：基本准备

去掉 "tunneld 正常运行" 前置假设，改为指向新的"连接设备"章节。

## 初始数据

首次实现时在 `var/devices.json` 中预设当前设备：

```json
{
  "default": "00008130-000845CC01EA001C",
  "aliases": {}
}
```
