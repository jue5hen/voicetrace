import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  EngineError,
  errorMessage,
  isVersionConflict,
  normalizeHistoryCount,
  normalizeModelState,
  normalizeModelsPresent,
  normalizeQueue,
  normalizeShellStatus,
  normalizeSystem,
  undoPath,
  usablePort,
  waitForEnginePort,
  withExpectedVersion,
} from "./engine";

describe("归一化：新契约字段", () => {
  it("model_state 只认四个合法值", () => {
    for (const value of ["idle", "loading", "ready", "failed"] as const) {
      assert.equal(normalizeModelState(value), value);
    }
    assert.equal(normalizeModelState("bogus"), undefined);
    assert.equal(normalizeModelState(undefined), undefined);
  });

  it("models_present 接受部分字段", () => {
    assert.deepEqual(normalizeModelsPresent({ aed: true, vad: false }), {
      aed: true,
      vad: false,
      punc: false,
    });
    assert.equal(normalizeModelsPresent(null), undefined);
  });

  it("normalizeSystem 保留 model_state / models_present / version", () => {
    const sys = normalizeSystem({
      version: "1.3.0",
      model_state: "ready",
      models_present: { aed: true, vad: true, punc: true },
      sample_rate: 16000,
      device: "cuda",
    });
    assert.equal(sys.version, "1.3.0");
    assert.equal(sys.model_state, "ready");
    assert.equal(sys.models_present?.punc, true);
    assert.equal(sys.sample_rate, 16000);
  });

  it("旧引擎（只有 models 数组）仍能读出已加载状态", () => {
    const sys = normalizeSystem({
      version: "1.2.0",
      models: [
        { key: "asr", loaded: true },
        { key: "punc", loaded: true },
      ],
    });
    assert.equal(sys.model_state, undefined);
    assert.equal(sys.models?.length, 2);
  });

  it("normalizeQueue：数字 pending 归一成数量，数组 pending 归一成长度", () => {
    assert.equal(normalizeQueue({ paused: true }).paused, true);
    assert.equal(normalizeQueue({ paused: true }).pending, undefined);
    assert.equal(normalizeQueue({ pending: 3 }).pending, 3);
    assert.equal(normalizeQueue({ pending: ["a", "b"] }).pending, 2);
    assert.equal(normalizeQueue({ current: null }).current, null);
    assert.deepEqual(normalizeQueue(null), {});
  });
});

describe("errorMessage", () => {
  it("Error / 字符串 / 未知值都能给出可读文案", () => {
    assert.equal(errorMessage(new Error("炸了")), "炸了");
    assert.equal(errorMessage("炸了"), "炸了");
    assert.ok(errorMessage({ weird: true }).length > 0);
  });
});

describe("写请求的版本契约（C3）", () => {
  it("PUT /sentences 的请求体带 expected_version", () => {
    assert.deepEqual(withExpectedVersion({ text: "改好了" }, 7), {
      text: "改好了",
      expected_version: 7,
    });
  });

  it("POST /apply 保留 actions 并带 expected_version", () => {
    const actions = [{ type: "dismiss", issue_id: "i1" }];
    assert.deepEqual(withExpectedVersion({ actions }, 3), { actions, expected_version: 3 });
  });

  it("版本未知时不伪造 expected_version（向后兼容）", () => {
    assert.deepEqual(withExpectedVersion({ text: "x" }, undefined), { text: "x" });
    assert.deepEqual(withExpectedVersion({ text: "x" }, null), { text: "x" });
    assert.deepEqual(withExpectedVersion({ text: "x" }, Number.NaN), { text: "x" });
  });

  it("undo 用查询参数传版本", () => {
    assert.equal(undoPath("abc", 4), "/jobs/abc/undo?expected_version=4");
    assert.equal(undoPath("a b", 2), "/jobs/a%20b/undo?expected_version=2");
    assert.equal(undoPath("abc", null), "/jobs/abc/undo");
  });

  it("409 版本冲突可识别，撤销栈为空的 409 不算冲突", () => {
    assert.equal(
      isVersionConflict(new EngineError("http_409", "版本冲突：当前 2，请求 1", 409)),
      true,
    );
    assert.equal(isVersionConflict(new EngineError("http_409", "没有可撤销的操作", 409)), false);
    assert.equal(isVersionConflict(new EngineError("network", "无法连接本地引擎", 0)), false);
    assert.equal(isVersionConflict(new Error("版本冲突")), false);
  });
});

describe("启动端口等待（D4 回归：engine_info.port 初始为 0）", () => {
  it("usablePort 只接受 1–65535 的整数", () => {
    assert.equal(usablePort(43210), 43210);
    assert.equal(usablePort(0), null);
    assert.equal(usablePort(-1), null);
    assert.equal(usablePort(65536), null);
    assert.equal(usablePort(1.5), null);
    assert.equal(usablePort("43210"), null);
    assert.equal(usablePort(undefined), null);
  });

  it("normalizeShellStatus 只收合法 phase，未知值归为 unknown", () => {
    assert.deepEqual(
      normalizeShellStatus({ phase: "failed", pid: 3, error: "找不到 Python", extra: 1 }),
      { phase: "failed", pid: 3, error: "找不到 Python" },
    );
    assert.equal(normalizeShellStatus({ phase: "weird" })?.phase, "unknown");
    assert.equal(normalizeShellStatus(null), null);
    assert.equal(normalizeShellStatus("nope"), null);
  });

  it("端口先回 0、随后回真实端口时必须继续等，不能把 0 当失败", async () => {
    const ports: Array<number | null> = [0, 0, 43210];
    let fakeNow = 0;
    let ticks = 0;
    const port = await waitForEnginePort({
      readInfo: async () => ({ port: ports.shift() ?? null, token: "tok" }),
      readStatus: async () => {
        ticks += 1;
        return { phase: "starting", pid: 88 };
      },
      sleep: async (ms) => {
        fakeNow += ms;
      },
      now: () => fakeNow,
      timeoutMs: 5000,
      intervalMs: 250,
    });
    assert.equal(port, 43210);
    assert.ok(ticks >= 1, "等待期间应读取壳状态用于进度展示");
  });

  it("壳报 failed 时立即失败并带出根因", async () => {
    await assert.rejects(
      waitForEnginePort({
        readInfo: async () => ({ port: 0, token: "tok" }),
        readStatus: async () => ({
          phase: "failed",
          error: "开发模式找不到 Python：D:\\missing\\python.exe",
        }),
        sleep: async () => undefined,
        now: () => 0,
        timeoutMs: 5000,
      }),
      (err: unknown) =>
        err instanceof EngineError &&
        err.code === "engine_failed" &&
        err.message.includes("Python"),
    );
  });

  it("壳报 exited 时错误码是 engine_exited", async () => {
    await assert.rejects(
      waitForEnginePort({
        readInfo: async () => ({ port: 0, token: "tok" }),
        readStatus: async () => ({ phase: "exited", exit_code: 2, error: "引擎进程（pid 1）已退出" }),
        sleep: async () => undefined,
        now: () => 0,
        timeoutMs: 5000,
      }),
      (err: unknown) => err instanceof EngineError && err.code === "engine_exited",
    );
  });

  it("始终拿不到端口时以 engine_port_timeout 结束，绝不去连 0 端口", async () => {
    let fakeNow = 0;
    await assert.rejects(
      waitForEnginePort({
        readInfo: async () => ({ port: 0, token: "tok" }),
        readStatus: async () => ({ phase: "starting" }),
        sleep: async (ms) => {
          fakeNow += ms;
        },
        now: () => fakeNow,
        timeoutMs: 2000,
        intervalMs: 1000,
      }),
      (err: unknown) => err instanceof EngineError && err.code === "engine_port_timeout",
    );
  });
});

describe("撤销历史深度（C4）", () => {
  it("接受 {count:n}，非法形状返回 null 交给调用方保留原值", () => {
    assert.equal(normalizeHistoryCount({ count: 3 }), 3);
    assert.equal(normalizeHistoryCount({ count: 0 }), 0);
    assert.equal(normalizeHistoryCount({ count: 2.9 }), 2);
    assert.equal(normalizeHistoryCount(4), 4);
    assert.equal(normalizeHistoryCount({ count: -1 }), null);
    assert.equal(normalizeHistoryCount({ count: "3" }), null);
    assert.equal(normalizeHistoryCount({ count: Number.NaN }), null);
    assert.equal(normalizeHistoryCount(null), null);
    assert.deepEqual(normalizeHistoryCount({}), null);
  });
});
