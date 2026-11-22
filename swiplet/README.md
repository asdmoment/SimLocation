# SimLocation Swiplet

在 Swipt 里运行的 SimLocation：定点、路线播放和恢复真实定位都由手机自己完成，不需要 Mac、VPS 或 a-Shell 中继；配对和开发者连接由 Swipt 管理。这是独立项目，不是 Swipt 官方扩展。仓库 `deploy/` 下的 VPS reflect 中继是这之前的做法，现在只作为 legacy 保留。

## 安装

```sh
cd swiplet
npm ci --ignore-scripts
npm run build   # 输出 dist/simlocation.swiplet
```

手机上需要 Surge（iOS 5.23.0 以上，增强模式）并启用 SimLocation Reflect 模块，见下面的「Surge 模块」。

在 Swipt 的 Swiplets 页面导入 `dist/simlocation.swiplet`，导入本身不会改变定位。手机连着电脑时，也可以用 `npx swipt swiplet install dist/simlocation.swiplet -d 设备` 直接安装。id 仍是 `simlocation`，覆盖旧版后收藏、路线草稿和 `SimLocationCellular` 快捷指令都照常可用。0.2.0 去掉了默认位置（用收藏代替），以及 `useDefault` 和只供旧界面调用的那些 action；0.3.0 加的 Simulate Favorite 按名字模拟收藏，代替 `useDefault`。

## 使用

- **定点**：轻点地图放图钉，按「模拟此位置」。图钉正好在 Swipt 实际持有的模拟位置上时，按钮变成「恢复真实位置」；设置或恢复失败时按钮和菜单都不会当成已生效。
- **路线**：切到「路线」，依次轻点地图添加途经点，按「开始路线」。播放中按钮变成「停止移动」，按下后停在当前位置。
- 右上角菜单：搜索地点、输入坐标（WGS-84）、收藏夹、撤销上一点、路线设置（速度、速度波动、位置偏移、循环、从剪贴板导入、复制路线 JSON）、恢复真实位置、启动 Surge、蜂窝网络重连。搜索结果、坐标和收藏在定点模式下移动图钉，在路线模式下追加为途经点。
- Swipt 有定位权限时，地图上的蓝点就是 iOS 实际报告的位置，能直接看出模拟有没有生效；没有权限时地图从全球视图开始，用搜索、坐标或收藏跳过去。
- 收藏夹和网页控制台的 `var/favorites.json` 格式相同，可以经剪贴板双向导入，按 id 合并，最多 200 个。
- 路线导入格式与 CLI 相同：`[[lat, lon], ...]`、`{"points": [...]}` 或只含一条 track segment / route 的 GPX，最多 10000 个点、4 MiB。
- 中国大陆的 Apple 地图是 GCJ-02 坐标，图钉和途经点在提交时换算成 WGS-84；港澳按实际边界排除（0.5.0 以前是两个矩形，蛇口、拱北、横琴口岸会偏约 600 米）。紧贴边界约 600 米内，地图上同一点可能对应两侧，反算时按内地处理（横琴上的澳门辖区除外）；从收藏或输入坐标选的点原样提交，不受影响。边界分类尚未逐点在真机上核对。
- 输入的坐标按 WGS-84 处理。从高德、腾讯地图复制的数字是 GCJ-02，照样输入会偏几百米；输入后看一眼图钉是否落在想要的位置。

## 工作方式

- 定点和恢复真实定位由 action 直接调用 `simulatedLocation.set`。Swipt 会保存这个位置，重连或自身重启后自动恢复，所以不需要后台服务。
- 路线由后台服务播放：每秒按开始以来的时间算出位置并发送，卡顿或重启后直接追到应在的位置，非循环路线停在精确终点。设备断开时服务退出，Swipt 等设备回来再重启它。
- 界面、每次 action 和服务各自运行在独立的 JS runtime 里，靠共享的 `simlocation.db` 通信：`requests` 表只保留最新一条请求，`state` 表存路线草稿，`favorites` 表存收藏。界面通过 SQLite 变更通知看到快捷指令和服务写入的变化。`requests` 只表示请求过什么、用于排序和播放路线；是否生效以不暴露给快捷指令的 `heldLocation`（`simulatedLocation.get()`）为准，每次设备调用结束都会通知界面重读。
- 不要和 StikDebug、官方「模拟位置」或旧 VPS 中继同时控制定位。
- 菜单里的「启动 Surge」打开 `surge:///start`，Surge 启动后要手动回到 Swipt。

## Surge 模块

Swipt 经 `10.7.0.1:49152` 连手机自己的开发者服务。这个地址并不存在，由 Surge 把发往它的包送回本机：

```
快捷指令 ─ Run Swiplet Action ─▶ Swipt ─ SimLocation（simulatedLocation.set）
                                   │
                                   └─ 开发者连接 10.7.0.1:49152
                                            │
                          Surge VIF：[IP Rewrite] 10.7.0.1 = reflect
                                            │
                                  手机自己的开发者服务
```

`surge/SimLocation-Reflect.sgmodule` 照 [Surge 手册 IP Rewrite 一节](https://manual.nssurge.com/features/ip-rewrite.html)的 On-Device Developer Services 示例写成，需要 Surge iOS 5.23.0 以上并以增强模式运行：

- `tun-included-routes = %INSERT% 10.7.0.1/32` 让发往 10.7.0.1 的包进入 Surge VIF。
- `10.7.0.1 = reflect` 在 IP 层把包的源地址和目的地址对调后送回，早于规则和策略，所以不需要 DIRECT 规则。
- `ipv6-vif = disabled`：手册写明 SideStore 找这条回环时会跳过带 IPv6 地址的 VPN 接口。这是 Surge 的默认值；主配置里设了 `auto` 或 `always` 的手机启用模块后，Surge 不再接管直接连 IPv6 地址的原始 TCP 连接。
- `#!requirement=CORE_VERSION>=6010000` 让没有 reflect 的旧版 Surge 把模块显示为不满足条件。

按 URL 安装的模块改动后，要在手机的模块列表里更新，Surge 不会自己刷新它。LocalDevVPN 和 WrapPin 的内置隧道做的是同一件事；iOS 同时只能开一个 VPN，所以这里由 Surge 承担。

## 快捷指令

| Action | 输入（JSON） | 作用 |
|---|---|---|
| Set Simulated Location (`setPoint`) | `{"latitude": 22.28, "longitude": 114.15}` | 立即模拟这个位置 |
| Simulate Favorite (`setFavorite`) | `{"name": "默认位置"}` | 最多等 20 秒连上设备，再模拟这个名字的收藏（名字要唯一）；返回值和 Check Cellular Connection 一样，失败也正常返回 |
| Restore Real Location (`clear`) | 无 | 恢复真实定位 |
| Apply SimLocation Shortcut (`locationShortcut`) | `{"mode":"favorite","name":"默认位置"}` 或 `{"mode":"clear"}`；重连期间加 `"reconnect":true` | Wi-Fi／蜂窝通用入口；返回供流程判断的 `status` 和面向用户的中文 `message`。连接、执行各最多等 4 秒，重连期间各 20 秒；无效收藏返回 `error`，连接或执行失败返回 `retry`，只有执行成功返回 `ok` |
| Play SimLocation Route (`startRoute`) | `{"points": [[lat, lon], ...], "speed": 5, "loop": false, "speedNoise": 0, "positionNoise": 0}`，只有 `points` 必填 | 提交路线。服务开着才会播放；用 Swipt 的 Set Swiplet Service 打开 |
| Check Cellular Connection (`probeConnection`) | 无 | 20 秒内反复检查 Swipt 能否连上设备，返回 `{"connected": true, "error": null}`；连不上也正常返回 |

### 只有蜂窝网络时

只有蜂窝网络时，Swipt 的开发者连接可能建立不起来。做法借鉴 SideStore：先关掉蜂窝数据，让连接在本机建立，再把数据打开。这套流程 2026-11-08 在连着 Wi-Fi 的手机上跑通过，只有蜂窝网络时还没验证。

先建两个辅助快捷指令：`TurnOffData`（设定蜂窝数据 → 关闭）和 `TurnOnData`（设定蜂窝数据 → 打开），然后建 `SimLocationCellular`：

1. 运行快捷指令 TurnOffData
2. 等待 1 秒
3. Swipt → Run Swiplet Action → SimLocation 的 Check Cellular Connection
4. 运行快捷指令 TurnOnData（无条件执行，不要放进判断分支）
5. 等待 6 秒（Swipt 每 5 秒检查一次路由变化）
6. 再运行一次 Check Cellular Connection
7. 显示结果
8. 打开 App → Swipt

菜单里的「蜂窝网络重连」就是运行这个快捷指令。如果中途被打断、数据没打开，手动运行 TurnOnData，或者在控制中心打开。

### 默认位置和恢复真实定位（0.4.0）

`SimLocation默认位置` 和 `SimLocation恢复真实定位` 共用同一流程，不按网络类型限制操作：

1. 调用 Apply SimLocation Shortcut，使用当前连接直接设置收藏或清除模拟定位。
2. 保存结果为 LocationResult，读取 `status`。
3. **仅当 status 是 retry**：关闭蜂窝数据 → 等 1 秒 → 以 reconnect=true 再执行 → 打开蜂窝数据 → 等 6 秒 → 以 reconnect=true 再执行，保存最后结果。
4. 只显示 LocationResult 的 `message`。成功提示「已设置默认位置。」或「已恢复真实定位。」；失败显示原因。不显示 JSON，也不打开 Swipt。

当前连接正常（包括 Wi-Fi 和蜂窝）时不会切换网络。无效收藏也不会触发网络切换。重连分支不关闭 Wi-Fi，但会把蜂窝最终设为开启；它不读取或还原原来的蜂窝开关状态。动作的普通错误或超时会返回结果，让后面打开蜂窝数据的步骤继续执行；用户取消、系统终止或 App Intent 自身报错仍可能中断流程，此时手动开启蜂窝。重试成功表示设备操作已完成，不代表已经测过长时间保持或重新获得了 GPS 定位。

0.4.1 起，失败的 `message` 第一行按 Swipt 的报错说明原因，第二行保留原始报错；`status` 的判定没有变，所以 0.4.0 的快捷指令不用重做：

| Swipt 的报错 | 提示 |
|---|---|
| `device is locked` | 手机处于锁定状态，解锁后重试 |
| `connection refused` | 10.7.0.1 拒绝了连接：解锁、把 Swipt 打开到前台；只有蜂窝网络时要先断开一次蜂窝数据 |
| `network is unreachable`、`no route to host`、`i/o timeout` | 连不上 10.7.0.1：确认 Surge 已启动并启用了 SimLocation Reflect 模块 |
| 其他 | 原来的通用提示 |

这几种报错都来自真机上的记录，对应关系是按当时怎么恢复的归纳出来的，不是对每种原因的完整诊断。

`tools/build_shortcuts.py TEAM NEW_OUTPUT_DIRECTORY` 生成这两个指令的未签名文件。TEAM 是那台手机上 Swipt 的签名团队标识，也就是 bundle id `io.nekohasekai.swipt.TEAM` 的最后一段；文件里除此之外没有和手机有关的内容，也不读取快捷指令数据库。指令自己切换蜂窝数据，不依赖 TurnOffData／TurnOnData 辅助指令。用系统 `shortcuts sign --mode anyone` 签名，再通过快捷指令 App 导入。当前系统会把已有同名指令加上数字后缀，保留新旧两份；核对新版后再清理旧副本。签好的文件和每台手机的安装记录放在 `phones/`，这个目录不进仓库。

网络恢复流程仍依赖 Swipt、Surge reflect、系统版本和配对状态。自动化测试中的 Wi-Fi／蜂窝成功场景使用假设备实现；真实纯蜂窝冷启动、恢复网络及持续保持需要在拔掉 USB 后单独验证，不能由这些测试推断。

## 开发

```sh
npm run check   # tsc
npm test        # node:test，数据库用 Node 内置的 node:sqlite，不接触手机
python3 -I tests/test_shortcut_files.py  # 快捷指令分支、输入、恢复网络顺序，不接触手机
npm run build
```

需要 Node.js 22.13 以上。`src/route.ts` 是从 `bin/simlocation.py` 移植的路线算法和导入格式；`src/location.ts` 是 action 和服务对设备做的事，测试里用假实现驱动；`src/store.ts` 是共享数据库；`src/extension.ts` 和 `src/App.tsx` 只负责接到 Swipt。构建目标是 `@swipt/api` / `@swipt/cli` `0.1.0-nightly.60`。

`src/coordinates.ts` 的坐标换算改编自 [official-swiplets](https://github.com/swipt-automation/official-swiplets) 的 simulate-location（`c15eefc`，© 2026 nekohasekai，GPL-3.0-or-later），声明随包放在 `assets/NOTICE.txt`。港澳边界 `src/regions.ts` 简化自 OpenStreetMap（ODbL 1.0），声明也在同一文件里。
