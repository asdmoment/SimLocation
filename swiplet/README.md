# SimLocation Swiplet

在 Swipt 里运行的 SimLocation：定点、路线播放和恢复真实定位都由手机自己完成，不需要 Mac、VPS 或 a-Shell 中继；配对和开发者连接由 Swipt 管理。这是独立项目，不是 Swipt 官方扩展。

## 安装

```sh
cd swiplet
npm ci --ignore-scripts
npm run build   # 输出 dist/simlocation.swiplet
```

在 Swipt 的 Swiplets 页面导入 `dist/simlocation.swiplet`，导入本身不会改变定位。id 仍是 `simlocation`，覆盖旧版后收藏、路线草稿和 `SimLocationCellular` 快捷指令都照常可用。0.2.0 去掉了默认位置（用收藏代替），以及 `useDefault` 和只供旧界面调用的那些 action。

## 使用

- **定点**：轻点地图放图钉，按「模拟此位置」。图钉正好在当前模拟位置上时，按钮变成「恢复真实位置」。
- **路线**：切到「路线」，依次轻点地图添加途经点，按「开始路线」。播放中按钮变成「停止移动」，按下后停在当前位置。
- 右上角菜单：搜索地点、输入坐标（WGS-84）、收藏夹、撤销上一点、路线设置（速度、速度波动、位置偏移、循环、从剪贴板导入、复制路线 JSON）、恢复真实位置、蜂窝网络重连。搜索结果、坐标和收藏在定点模式下移动图钉，在路线模式下追加为途经点。
- 地图上的蓝点是 iOS 实际报告的位置，可以直接看出模拟有没有生效。
- 收藏夹和网页控制台的 `var/favorites.json` 格式相同，可以经剪贴板双向导入，按 id 合并，最多 200 个。
- 路线导入格式与 CLI 相同：`[[lat, lon], ...]`、`{"points": [...]}` 或只含一条 track segment / route 的 GPX，最多 10000 个点、4 MiB。
- 中国大陆的 Apple 地图是 GCJ-02 坐标，图钉和途经点在提交时换算成 WGS-84；港澳不换算。

## 工作方式

- 定点和恢复真实定位由 action 直接调用 `simulatedLocation.set`。Swipt 会保存这个位置，重连或自身重启后自动恢复，所以不需要后台服务。
- 路线由后台服务播放：每秒按开始以来的时间算出位置并发送，卡顿或重启后直接追到应在的位置，非循环路线停在精确终点。设备断开时服务退出，Swipt 等设备回来再重启它。
- 界面、每次 action 和服务各自运行在独立的 JS runtime 里，靠共享的 `simlocation.db` 通信：`requests` 表只保留最新一条请求，`state` 表存路线草稿，`favorites` 表存收藏。界面通过 SQLite 变更通知看到快捷指令和服务写入的变化。
- 不要和 StikDebug、官方「模拟位置」或旧 VPS 中继同时控制定位。

## 快捷指令

| Action | 输入（JSON） | 作用 |
|---|---|---|
| Set Simulated Location (`setPoint`) | `{"latitude": 22.28, "longitude": 114.15}` | 立即模拟这个位置 |
| Restore Real Location (`clear`) | 无 | 恢复真实定位 |
| Play SimLocation Route (`startRoute`) | `{"points": [[lat, lon], ...], "speed": 5, "loop": false, "speedNoise": 0, "positionNoise": 0}`，只有 `points` 必填 | 提交路线。服务开着才会播放；用 Swipt 的 Set Swiplet Service 打开 |
| Check Cellular Connection (`probeConnection`) | 无 | 20 秒内反复检查 Swipt 能否连上设备，返回 `{"connected": true, "error": null}`；连不上也正常返回 |

### 只有蜂窝网络时

只有蜂窝网络时，Swipt 的开发者连接可能建立不起来。做法借鉴 SideStore：先关掉蜂窝数据，让连接在本机建立，再把数据打开。这个流程还没在真机上完整验证过。

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

## 开发

```sh
npm run check   # tsc
npm test        # node:test，数据库用 Node 内置的 node:sqlite，不接触手机
npm run build
```

需要 Node.js 22.13 以上。`src/route.ts` 是从 `bin/simlocation.py` 移植的路线算法和导入格式；`src/location.ts` 是 action 和服务对设备做的事，测试里用假实现驱动；`src/store.ts` 是共享数据库；`src/extension.ts` 和 `src/App.tsx` 只负责接到 Swipt。构建目标是 `@swipt/api` / `@swipt/cli` `0.1.0-nightly.60`。

`src/coordinates.ts` 的坐标换算改编自 [official-swiplets](https://github.com/swipt-automation/official-swiplets) 的 simulate-location（`c15eefc`，© 2026 nekohasekai，GPL-3.0-or-later），声明随包放在 `assets/NOTICE.txt`。
