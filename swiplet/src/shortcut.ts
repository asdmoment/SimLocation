import { whenConnected } from "./location";

export interface ShortcutResult {
  status: "ok" | "retry" | "error";
  message: string;
}

// What Swipt says when it cannot reach the phone, as seen on real devices, and
// what to do about each. Anything else keeps the general advice.
const CAUSES: [RegExp, string][] = [
  [/device is locked/i, "手机处于锁定状态，请解锁后重试。"],
  [/connection refused/i,
    "10.7.0.1 拒绝了连接。请解锁手机，把 Swipt 打开到前台后重试；只有蜂窝网络时，要先断开一次蜂窝数据才能连上。"],
  [/network is unreachable|no route to host|i\/o timeout/i,
    "连不上 10.7.0.1。请确认 Surge 已启动并启用了 SimLocation Reflect 模块，再把 Swipt 打开到前台后重试。"],
];

/** The message for an operation that did not finish; the error keeps its own line. */
export function explainFailure(error: string | null): string {
  const detail = error ?? "设备未连接。";
  const cause = CAUSES.find(([pattern]) => pattern.test(detail))?.[1];
  return `操作未完成${cause ? `：${cause}` : "，请确认 Swipt 的设备连接、Surge 和 reflect 模块正常后重试。"}\n${detail}`;
}

/** Both network types use the live connection first; Shortcuts owns the fallback. */
export async function runLocationShortcut(
  info: (signal: AbortSignal) => Promise<unknown>,
  task: (signal: AbortSignal) => Promise<void>,
  signal: AbortSignal,
  message: string,
  budget = 4_000,
): Promise<ShortcutResult> {
  const result = await whenConnected(info, task, signal, budget);
  if (result.connected && result.error === null) return { status: "ok", message };
  return {
    status: signal.aborted ? "error" : "retry",
    message: explainFailure(result.error),
  };
}
