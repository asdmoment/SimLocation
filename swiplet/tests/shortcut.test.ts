import test from "node:test";
import assert from "node:assert/strict";
import { runLocationShortcut } from "../src/shortcut";

const signal = () => new AbortController().signal;

for (const operation of ["已设置默认位置。", "已恢复真实定位。"]) {
  for (const network of ["Wi-Fi", "cellular"]) {
    test(`${operation}: a live ${network} connection succeeds directly`, async () => {
      let calls = 0;
      const result = await runLocationShortcut(async () => {}, async () => { calls++; }, signal(), operation);
      assert.deepEqual(result, { status: "ok", message: operation });
      assert.equal(calls, 1);
      assert.doesNotMatch(result.message, /connected|error|null/);
    });
  }
  test(`${operation}: unavailable device requests fallback without applying`, async () => {
    let called = false;
    const result = await runLocationShortcut(async () => { throw new Error("offline"); }, async () => { called = true; }, signal(), operation, 10);
    assert.equal(result.status, "retry");
    assert.equal(called, false);
    assert.match(result.message, /操作未完成/);
  });
  test(`${operation}: a connection alone is not success`, async () => {
    const result = await runLocationShortcut(async () => {}, async () => { throw new Error("set failed"); }, signal(), operation);
    assert.equal(result.status, "retry");
    assert.match(result.message, /set failed/);
    assert.notEqual(result.message, operation);
  });
}

test("a task that ignores cancellation cannot keep cellular data off indefinitely", async () => {
  const result = await runLocationShortcut(async () => {}, () => new Promise(() => {}), signal(), "Done", 10);
  assert.equal(result.status, "retry");
  assert.match(result.message, /操作未完成/);
});

test("a failure names the cause seen on real devices and keeps the error", async () => {
  const dial = "the device is unavailable: none of the VPN addresses 10.7.0.1 answers on port 49152: dial tcp 10.7.0.1:49152: ";
  for (const [error, hint] of [
    ["the device is locked: unlock it first", /解锁后重试/],
    [`${dial}connect: connection refused`, /拒绝了连接/],
    [`${dial}connect: network is unreachable`, /Surge 已启动/],
    [`${dial}i/o timeout`, /Surge 已启动/],
    ["something new", /请确认 Swipt 的设备连接/],
  ] as const) {
    const result = await runLocationShortcut(async () => { throw new Error(error); }, async () => {}, signal(), "Done", 10);
    // The fallback in Shortcuts depends on the status, so only the wording differs.
    assert.equal(result.status, "retry");
    assert.match(result.message, hint);
    assert.ok(result.message.startsWith("操作未完成") && result.message.endsWith(`\n${error}`));
  }
});

test("a cancelled request does not ask Shortcuts to cycle cellular data", async () => {
  const controller = new AbortController();
  controller.abort();
  let called = false;
  const result = await runLocationShortcut(async () => { called = true; }, async () => { called = true; }, controller.signal, "Done");
  assert.equal(result.status, "error");
  assert.equal(called, false);
});

test("a successful reconnect retry returns the friendly success message", async () => {
  const unavailable = await runLocationShortcut(async () => { throw new Error("offline"); }, async () => {}, signal(), "已恢复真实定位。", 10);
  assert.equal(unavailable.status, "retry");
  const restored = await runLocationShortcut(async () => {}, async () => {}, signal(), "已恢复真实定位。", 10);
  assert.deepEqual(restored, { status: "ok", message: "已恢复真实定位。" });
});
