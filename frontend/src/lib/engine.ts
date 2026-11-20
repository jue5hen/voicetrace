import type {
  Action,
  ExportFormat,
  ImportResult,
  IssuesResponse,
  Job,
  ModelDownloadStatus,
  ModelState,
  ModelsPresent,
  QueueWire,
  Settings,
  SkippedImport,
  SystemInfo,
  Transcript,
  WsEvent,
  ConnectionState,
} from "../types";

/**
 * 引擎连接层。
 * - Tauri：invoke("engine_info") 取得 {port, token}
 * - 浏览器：VITE_API_BASE（默认 http://127.0.0.1:8765）+ VITE_TOKEN（默认 dev）
 * 所有 HTTP 请求带 Bearer；媒体与 WS 走 ?token=。
 */
export interface EngineConnection {
  base: string;
  token: string;
  source: "tauri" | "browser";
}

let connection: EngineConnection | null = null;

export function isTauriRuntime(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as unknown as Record<string, unknown>;
  return "__TAURI_INTERNALS__" in w || "__TAURI__" in w;
}

function normalizeBase(raw: string): string {
  let base = raw.trim().replace(/\/+$/, "");
  if (base.endsWith("/api")) base = base.slice(0, -4);
  return base;
}

/** 壳 `engine_status` 的进程阶段（与 docs/SPEC.md §5 一致）。 */
export type ShellEnginePhase = "starting" | "ready" | "failed" | "exited" | "unknown";

/** `engine_status` 的回包；浏览器/旧壳拿不到时为 null。 */
export interface ShellEngineStatus {
  phase: ShellEnginePhase;
  pid?: number;
  exit_code?: number;
  log_tail?: string;
  error?: string;
}

/** 端口必须是 1–65535 的整数；0/缺省表示壳还没拿到引擎自 bind 的端口。 */
export function usablePort(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && value <= 65535
    ? value
    : null;
}

/** `engine_status` 回包整形：未知 phase 归为 unknown，其余字段只收形状正确的。 */
export function normalizeShellStatus(raw: unknown): ShellEngineStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const src = raw as Record<string, unknown>;
  const phase: ShellEnginePhase =
    src.phase === "starting" || src.phase === "ready" || src.phase === "failed" || src.phase === "exited"
      ? src.phase
      : "unknown";
  const status: ShellEngineStatus = { phase };
  if (typeof src.pid === "number" && Number.isFinite(src.pid)) status.pid = src.pid;
  if (typeof src.exit_code === "number" && Number.isFinite(src.exit_code)) {
    status.exit_code = src.exit_code;
  }
  if (typeof src.log_tail === "string") status.log_tail = src.log_tail;
  if (typeof src.error === "string" && src.error) status.error = src.error;
  return status;
}

async function invokeShell<T>(command: string): Promise<T | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<T>(command);
  } catch {
    // 命令缺失/壳未就绪：调用方按 null 降级（浏览器 mock、旧壳）。
    return null;
  }
}

/** 读取壳的引擎进程状态；非 Tauri 或命令失败返回 null。 */
export async function readShellEngineStatus(): Promise<ShellEngineStatus | null> {
  return normalizeShellStatus(await invokeShell("engine_status"));
}

export interface EngineStartOptions {
  /** 等待壳公布端口的总预算（毫秒），默认 180 000。 */
  timeoutMs?: number;
  /** 等待期间回调（启动页据此显示 pid/阶段）。 */
  onWait?: (status: ShellEngineStatus | null) => void;
}

export interface PortWaitDeps {
  readInfo: () => Promise<{ port: number | null; token: string } | null>;
  readStatus: () => Promise<ShellEngineStatus | null>;
  timeoutMs?: number;
  intervalMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  onWait?: (status: ShellEngineStatus | null) => void;
}

/**
 * 等壳监督线程把引擎自 bind 的端口公布出来（D4 之后 `engine_info.port` 初始为 0）。
 * 进程 failed/exited 立即失败（带壳的 error 文案），超时才报超时；
 * 依赖可注入，便于在 node:test 里用假时钟/假 sleep 覆盖。
 */
export async function waitForEnginePort(deps: PortWaitDeps): Promise<number> {
  const now = deps.now ?? Date.now;
  const sleep =
    deps.sleep ??
    ((ms: number) =>
      new Promise<void>((resolve) => {
        globalThis.setTimeout(resolve, ms);
      }));
  const interval = Math.max(25, deps.intervalMs ?? 250);
  const timeout = Math.max(0, deps.timeoutMs ?? 180_000);
  const deadline = now() + timeout;
  let last: ShellEngineStatus | null = null;
  for (;;) {
    const info = await deps.readInfo();
    const port = usablePort(info?.port);
    if (port !== null) return port;
    last = await deps.readStatus();
    deps.onWait?.(last);
    if (last?.phase === "failed" || last?.phase === "exited") {
      throw new EngineError(
        last.phase === "exited" ? "engine_exited" : "engine_failed",
        last.error ||
          (last.phase === "exited"
            ? "引擎进程已退出，请点击「重启引擎」后重试（详情见引擎日志）。"
            : "引擎进程未能启动，请查看引擎日志后重试。"),
      );
    }
    if (now() >= deadline) break;
    await sleep(interval);
  }
  throw new EngineError(
    "engine_port_timeout",
    last?.error ||
      `等待本地引擎启动超时（${Math.round(timeout / 1000)} 秒）。请查看引擎日志后重启应用。`,
  );
}

export async function initEngine(
  force = false,
  options: EngineStartOptions = {},
): Promise<EngineConnection> {
  if (connection && !force) return connection;
  // 强制重连（启动页重试/重启引擎）时先丢掉旧连接：等待新端口失败时
  // 不能让 getConnection() 继续指向上一轮的端口与 token。
  if (force) connection = null;
  if (isTauriRuntime()) {
    let raw: { port?: unknown; token?: unknown } | null = null;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      raw = await invoke<{ port?: unknown; token?: unknown }>("engine_info");
    } catch {
      throw new EngineError(
        "engine_info_failed",
        "无法读取桌面壳的引擎信息，请重启应用；若仍失败请查看引擎日志。",
      );
    }
    const token = typeof raw?.token === "string" ? raw.token : "";
    let port = usablePort(raw?.port);
    if (!token) {
      const status = await readShellEngineStatus();
      throw new EngineError(
        "engine_info_failed",
        status?.error || "未取得引擎访问令牌，请检查桌面壳的引擎启动日志。",
      );
    }
    if (port === null) {
      // 1.3.2 回归根因：D4 改为壳传 --port 0、引擎自 bind 后上报端口，
      // engine_info 在端口公布前返回 0。旧代码把 0 当启动失败，于是
      // 「首次启动必失败、点重试立即成功」。这里按契约等待端口公布。
      port = await waitForEnginePort({
        readInfo: async () => {
          const info = await invokeShell<{ port?: unknown; token?: unknown }>("engine_info");
          return info
            ? {
                port: usablePort(info.port),
                token: typeof info.token === "string" ? info.token : "",
              }
            : null;
        },
        readStatus: readShellEngineStatus,
        timeoutMs: options.timeoutMs,
        onWait: options.onWait,
      });
    }
    connection = {
      base: `http://127.0.0.1:${port}`,
      token,
      source: "tauri",
    };
  } else {
    // 浏览器默认与页面同源：开发服务器 / 预览服务器负责把 /api 代理到引擎。
    // 引擎端口每次启动都可能变化，前端不应在构建时写死。
    // 如需指向独立引擎，可在页面加载前设置 window.__VT_API_BASE__ / __VT_TOKEN__。
    const w = window as unknown as { __VT_API_BASE__?: string; __VT_TOKEN__?: string };
    connection = {
      base: normalizeBase(w.__VT_API_BASE__ ?? ""),
      token: w.__VT_TOKEN__ || "dev",
      source: "browser",
    };
  }
  return connection;
}

export function getConnection(): EngineConnection {
  if (!connection) throw new EngineError("not_initialized", "引擎连接尚未初始化。");
  return connection;
}

export function resetConnection(): void {
  connection = null;
}

export class EngineError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status = 0) {
    super(message);
    this.name = "EngineError";
    this.code = code;
    this.status = status;
  }
}

/**
 * 409 有两种含义：写请求的版本冲突（C3）与撤销栈为空。
 * 只有前者需要重新对齐版本，后者按「没有可撤销的修改」处理。
 */
export function isVersionConflict(err: unknown): boolean {
  if (!(err instanceof EngineError) || err.status !== 409) return false;
  return err.code === "version_conflict" || err.message.includes("版本冲突");
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const conn = getConnection();
  const url = `${conn.base}/api${path}`;
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    opts.timeoutMs ?? 30_000,
  );
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? "GET",
      headers: {
        Authorization: `Bearer ${conn.token}`,
        ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    window.clearTimeout(timeout);
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new EngineError("timeout", "请求超时，引擎可能正忙或未启动。");
    }
    throw new EngineError(
      "network",
      conn.source === "browser"
        ? "无法连接本地引擎。开发模式请先启动真实引擎（8765）。"
        : "无法连接本地引擎，请稍候或重启应用。",
    );
  }
  window.clearTimeout(timeout);

  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = `请求失败（HTTP ${res.status}）`;
    try {
      const payload = (await res.json()) as {
        error?: { code?: string; message?: string };
        detail?: string;
      };
      if (payload?.error) {
        code = payload.error.code || code;
        message = payload.error.message || message;
      } else if (payload?.detail) {
        message = payload.detail;
      }
    } catch {
      // 非 JSON 错误体，保留状态码文案。
    }
    throw new EngineError(code, message, res.status);
  }

  if (res.status === 204) return undefined as T;
  const raw = await res.text();
  try {
    return JSON.parse(raw) as T;
  } catch {
    // 代理/错误页可能回 200 + HTML：裸 SyntaxError 无法定位，这里给可读文案。
    const kind = res.headers.get("content-type") || "未知类型";
    throw new EngineError(
      "bad_response",
      `引擎返回了非 JSON 响应（HTTP ${res.status}，${kind}），请确认连接的是声迹引擎。`,
      res.status,
    );
  }
}

/* ---------------- 真实引擎响应归一化 ---------------- */

type RawSystem = Record<string, unknown> & {
  models?: unknown;
  models_present?: unknown;
  models_download?: unknown;
  model_state?: unknown;
  sample_rate?: unknown;
  paths?: Record<string, unknown>;
  gpu?: unknown;
  device?: unknown;
  ready?: unknown;
  loading?: unknown;
  load_progress?: unknown;
  error?: unknown;
};

/** 只接受契约里的四个状态值，其余（含缺省）一律返回 undefined 交给调用方回退。 */
export function normalizeModelState(raw: unknown): ModelState | undefined {
  return raw === "idle" || raw === "loading" || raw === "ready" || raw === "failed"
    ? raw
    : undefined;
}

/** {aed,vad,punc} 布尔表；缺字段按 false 处理，整体缺失返回 undefined。 */
export function normalizeModelsPresent(raw: unknown): ModelsPresent | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const src = raw as Record<string, unknown>;
  return {
    aed: src.aed === true,
    vad: src.vad === true,
    punc: src.punc === true,
  };
}

/**
 * 模型下载状态归一化：旧引擎没有该字段时返回 undefined，UI 按 idle 处理。
 */
export function normalizeModelDownload(raw: unknown): ModelDownloadStatus | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const src = raw as Record<string, unknown>;
  const state = src.state;
  if (
    state !== "idle" &&
    state !== "running" &&
    state !== "done" &&
    state !== "error" &&
    state !== "canceled"
  ) {
    return undefined;
  }
  const modelsRaw =
    src.models && typeof src.models === "object"
      ? (src.models as Record<string, Record<string, unknown>>)
      : {};
  const models: ModelDownloadStatus["models"] = {};
  for (const key of ["aed", "vad", "punc"]) {
    const item = modelsRaw[key];
    if (!item) continue;
    models[key] = {
      label: typeof item.label === "string" ? item.label : key,
      ready: item.ready === true,
      bytes: typeof item.bytes === "number" ? item.bytes : 0,
      total: typeof item.total === "number" ? item.total : 0,
    };
  }
  return {
    state,
    current: typeof src.current === "string" ? src.current : null,
    progress: typeof src.progress === "number" ? src.progress : 0,
    downloaded_bytes: typeof src.downloaded_bytes === "number" ? src.downloaded_bytes : 0,
    total_bytes: typeof src.total_bytes === "number" ? src.total_bytes : 0,
    error: typeof src.error === "string" ? src.error : null,
    repos: Array.isArray(src.repos)
      ? src.repos.filter((item): item is string => typeof item === "string")
      : [],
    models,
  };
}

/**
 * 真实引擎（FastAPI）与 SPEC 的字段形态存在差异：
 * - model_state 是模型加载状态（idle/loading/ready/failed）
 * - models_present 是模型文件是否齐备（替代旧 models）
 * - gpu 可能是 {cuda,device,name,vram_mb,...}
 * - paths 可能是 {data,models}
 * 这里统一成前端使用的 SystemInfo，避免 UI 到处做兼容。
 *
 * 注意：不再把旧 models 字典（{aed:true,...} 表示目录存在）翻译成 loaded，
 * 「模型目录存在」不等于「模型已加载」，旧字段只作 mock 的进度展示用。
 */
export function normalizeSystem(input: unknown): SystemInfo {
  const raw = (input && typeof input === "object" ? input : {}) as RawSystem;
  const models = Array.isArray(raw.models)
    ? (raw.models as SystemInfo["models"])
    : undefined;

  const gpuRaw = raw.gpu;
  const gpuInfo =
    gpuRaw && typeof gpuRaw === "object"
      ? (gpuRaw as SystemInfo["gpu_info"])
      : undefined;
  const gpu =
    typeof gpuRaw === "string"
      ? gpuRaw
      : gpuInfo?.name || gpuInfo?.device || undefined;

  const pathsRaw = raw.paths ?? {};
  const ready = raw.ready === true;
  const loading =
    typeof raw.loading === "boolean" ? raw.loading : ready ? false : undefined;

  return {
    ...(raw as unknown as SystemInfo),
    device:
      typeof raw.device === "string"
        ? raw.device
        : gpuInfo?.device || (gpuInfo?.cuda ? "cuda" : undefined),
    gpu,
    gpu_info: gpuInfo,
    ready,
    loading,
    load_progress:
      typeof raw.load_progress === "number"
        ? raw.load_progress
        : ready
          ? 1
          : undefined,
    model_state: normalizeModelState(raw.model_state),
    models_present: normalizeModelsPresent(raw.models_present),
    models_download: normalizeModelDownload(raw.models_download),
    sample_rate: typeof raw.sample_rate === "number" ? raw.sample_rate : undefined,
    error: typeof raw.error === "string" ? raw.error : null,
    models,
    paths: {
      ...(pathsRaw as SystemInfo["paths"]),
      data_dir:
        (pathsRaw.data_dir as string) ?? (pathsRaw.data as string) ?? undefined,
      models_dir:
        (pathsRaw.models_dir as string) ??
        (pathsRaw.models as string) ??
        undefined,
    },
  };
}

/**
 * WS queue 快照归一化（F6）。
 * 引擎实际推的是 {paused, pending:排队数量, current:当前任务 id}，
 * 而前端 QueueState 的 pending 是 id 数组、total 由 jobs 派生，两者形状不同，
 * 直接 setQueue(事件) 会让 pending 变成数字并被 .length 读取成 undefined。
 * mock/旧引擎推的是完整快照（pending 可能已是数组），这里一并兼容。
 */
export function normalizeQueue(input: unknown): QueueWire {
  if (!input || typeof input !== "object") return {};
  const src = input as Record<string, unknown>;
  const num = (value: unknown): number | undefined =>
    typeof value === "number" && Number.isFinite(value) ? value : undefined;
  const id = (value: unknown): string | null | undefined =>
    typeof value === "string" ? value : value === null ? null : undefined;

  return {
    paused: typeof src.paused === "boolean" ? src.paused : undefined,
    pending: Array.isArray(src.pending) ? src.pending.length : num(src.pending),
    current: id(src.current),
    active: typeof src.active === "string" ? src.active : undefined,
    total: num(src.total),
    processing: num(src.processing),
    done: num(src.done),
    error: num(src.error),
  };
}

/* ---------------- 写请求的版本契约（C3） ---------------- */

/**
 * 写请求体：带 expected_version 让服务端做 CAS（docs/SPEC.md §3「并发写」）。
 * 版本未知（尚未加载/旧引擎）时只发业务字段，服务端会跳过 CAS，保持向后兼容。
 */
export function withExpectedVersion<T extends Record<string, unknown>>(
  body: T,
  expectedVersion: number | null | undefined,
): T & { expected_version?: number } {
  if (typeof expectedVersion !== "number" || !Number.isFinite(expectedVersion)) return body;
  return { ...body, expected_version: Math.max(0, Math.floor(expectedVersion)) };
}

/** undo 的版本走查询参数（服务端签名是 undo(expected_version: int | None = None)）。 */
export function undoPath(id: string, expectedVersion: number | null | undefined): string {
  const path = `/jobs/${encodeURIComponent(id)}/undo`;
  if (typeof expectedVersion !== "number" || !Number.isFinite(expectedVersion)) return path;
  return `${path}?expected_version=${Math.max(0, Math.floor(expectedVersion))}`;
}

/** GET /jobs/{id}/history 的回包 {count:n}；形状不对返回 null，调用方保留原值（C4）。 */
export function normalizeHistoryCount(raw: unknown): number | null {
  const value =
    typeof raw === "number"
      ? raw
      : raw && typeof raw === "object"
        ? (raw as { count?: unknown }).count
        : undefined;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return Math.floor(value);
}

/* ---------------- 便捷接口 ---------------- */

export const api = {
  health: async () => {
    try {
      return await request<{ ok: boolean; version?: string; uptime_s?: number }>(
        "/health",
        { timeoutMs: 5_000 },
      );
    } catch (err) {
      // 真实引擎的 /health 没有 CORS 头，浏览器跨源会直接 TypeError。
      // /system 带 CORS，用它做连通性兜底（Tauri 同源不受影响）。
      if (err instanceof EngineError && (err.code === "network" || err.status === 0)) {
        const sys = await api.system();
        return { ok: true, version: sys.version };
      }
      throw err;
    }
  },
  system: async () => normalizeSystem(await request<unknown>("/system")),
  models: () => request<ModelDownloadStatus>("/models", { timeoutMs: 10_000 }),
  downloadModels: (body: { repos?: string[]; proxy?: string; endpoint?: string }) =>
    request<ModelDownloadStatus>("/models/download", {
      method: "POST",
      body,
      timeoutMs: 20_000,
    }),
  cancelModelDownload: () =>
    request<ModelDownloadStatus>("/models/cancel", { method: "POST" }),
  jobs: () => request<{ jobs: Job[] }>("/jobs"),
  job: (id: string) => request<Job>(`/jobs/${encodeURIComponent(id)}`),
  /**
   * 引擎会逐个文件校验：格式不支持/已存在/无法读取的条目进 skipped。
   * 这里保证两个字段都是数组，避免旧引擎只返回 jobs 时前端崩。
   */
  createJobs: async (paths: string[], autoCorrect = true): Promise<ImportResult> => {
    const res = await request<{ jobs?: unknown; skipped?: unknown }>("/jobs", {
      method: "POST",
      body: { paths, auto_correct: autoCorrect },
    });
    const jobs = Array.isArray(res.jobs) ? (res.jobs as Job[]) : [];
    const skipped: SkippedImport[] = Array.isArray(res.skipped)
      ? (res.skipped as unknown[]).flatMap((item) => {
          if (!item || typeof item !== "object") return [];
          const rec = item as { path?: unknown; reason?: unknown };
          return [
            {
              path: typeof rec.path === "string" ? rec.path : "",
              reason: typeof rec.reason === "string" && rec.reason ? rec.reason : "未加入队列",
            },
          ];
        })
      : [];
    return { jobs, skipped };
  },
  deleteJob: (id: string) =>
    request<void>(`/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  retryJob: (id: string) =>
    request<Job>(`/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  cancelJob: (id: string) =>
    request<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  setAutoCorrect: (id: string, enabled: boolean) =>
    request<Job>(`/jobs/${encodeURIComponent(id)}/auto-correct`, {
      method: "POST",
      body: { enabled },
    }),
  setQueuePaused: (paused: boolean) =>
    request<{ paused: boolean }>("/queue", { method: "POST", body: { paused } }),
  transcript: (id: string) =>
    request<Transcript>(`/jobs/${encodeURIComponent(id)}/transcript`, {
      timeoutMs: 60_000,
    }),
  issues: (id: string) =>
    request<IssuesResponse>(`/jobs/${encodeURIComponent(id)}/issues`),
  /** 字幕改写：携带 expected_version，版本落后时服务端 409（C3）。 */
  updateSentence: (id: string, sid: number, text: string, expectedVersion: number) =>
    request<IssuesResponse & { transcript: Transcript }>(
      `/jobs/${encodeURIComponent(id)}/sentences/${sid}`,
      { method: "PUT", body: withExpectedVersion({ text }, expectedVersion) },
    ),
  /** 批量处理：同样携带 expected_version，避免覆盖别处的新稿（C3）。 */
  apply: (id: string, actions: Action[], expectedVersion: number) =>
    request<IssuesResponse & { transcript: Transcript }>(
      `/jobs/${encodeURIComponent(id)}/apply`,
      { method: "POST", body: withExpectedVersion({ actions }, expectedVersion) },
    ),
  undo: (id: string, expectedVersion: number) =>
    request<IssuesResponse & { transcript: Transcript; undone: boolean }>(
      undoPath(id, expectedVersion),
      { method: "POST" },
    ),
  /**
   * 撤销栈深度（C4）：服务端当前保留的历史条数，驱动「撤销」按钮是否可用。
   * 引擎（1.3.3+）提供 GET /jobs/{id}/history → {count:n}；旧引擎 404 时调用方保留本地值。
   */
  history: (id: string) =>
    request<{ count?: number }>(`/jobs/${encodeURIComponent(id)}/history`, {
      timeoutMs: 10_000,
    }),
  export: (id: string, format: ExportFormat, path?: string) =>
    request<{ path: string; fallback?: boolean; requested_path?: string }>(`/jobs/${encodeURIComponent(id)}/export`, {
      method: "POST",
      body: path ? { format, path } : { format },
      timeoutMs: 180_000,
    }),
  settings: () => request<Settings>("/settings"),
  // 引擎按 { settings: {...} } 作为请求体解析；扁平结构会被忽略，切勿改回。
  saveSettings: (settings: Settings) =>
    request<Settings>("/settings", { method: "PUT", body: { settings } }),
};

/* ---------------- 资源 URL ---------------- */

export function mediaUrl(jobId: string): string {
  const conn = getConnection();
  return `${conn.base}/api/jobs/${encodeURIComponent(jobId)}/media?token=${encodeURIComponent(conn.token)}`;
}

export function thumbUrl(job: Job): string {
  const conn = getConnection();
  const raw = job.thumb_url;
  if (raw) {
    if (/^https?:\/\//i.test(raw)) return raw;
    const base = `${conn.base}${raw.startsWith("/") ? "" : "/"}${raw}`;
    // 引擎给的 thumb_url 不含 token（避免 token 扩散到任务对象/日志），这里补上。
    if (raw.includes("token=")) return base;
    return `${base}${base.includes("?") ? "&" : "?"}token=${encodeURIComponent(conn.token)}`;
  }
  return `${conn.base}/api/jobs/${encodeURIComponent(job.id)}/thumb?token=${encodeURIComponent(conn.token)}`;
}

/* ---------------- WebSocket ---------------- */

export interface EventBridge {
  dispose: () => void;
}

export function connectEvents(
  onEvent: (event: WsEvent) => void,
  onState: (state: ConnectionState) => void,
  /** 连接建立（含重连成功）时回调，用于重新对齐任务与当前转录。 */
  onOpen?: () => void,
): EventBridge {
  let disposed = false;
  let attempt = 0;
  let socket: WebSocket | null = null;
  let retryTimer = 0;

  const setState = (s: ConnectionState) => {
    if (!disposed) onState(s);
  };

  const schedule = () => {
    if (disposed) return;
    // 引擎在 127.0.0.1：浏览器判定「离线」（拔线/飞行模式）时本地依然可达，不能停重连。
    setState(attempt === 0 ? "connecting" : "polling");
    const delay = Math.min(8_000, 600 * 2 ** attempt) + Math.floor(Math.random() * 250);
    attempt = Math.min(attempt + 1, 5);
    retryTimer = window.setTimeout(open, delay);
  };

  const open = () => {
    if (disposed) return;
    window.clearTimeout(retryTimer);
    const conn = getConnection();
    let origin = conn.base.replace(/^http/i, "ws");
    if (!origin) {
      const proto =
        typeof location !== "undefined" && location.protocol === "https:"
          ? "wss:"
          : "ws:";
      origin = `${proto}//${location.host}`;
    }
    const url = `${origin}/api/ws?token=${encodeURIComponent(conn.token)}`;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      schedule();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      attempt = 0;
      setState("online");
      onOpen?.();
    };
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(String(msg.data)) as WsEvent;
        if (data && typeof data === "object" && "type" in data) onEvent(data);
      } catch {
        // 忽略非 JSON 帧。
      }
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        // 忽略。
      }
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      if (disposed) return;
      schedule();
    };
  };

  open();

  return {
    dispose: () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      try {
        socket?.close(1000, "bye");
      } catch {
        // 忽略。
      }
      socket = null;
    },
  };
}

export function errorMessage(err: unknown): string {
  if (err instanceof EngineError) return err.message;
  if (err instanceof Error) return err.message || "发生未知错误。";
  return String(err || "发生未知错误。");
}
