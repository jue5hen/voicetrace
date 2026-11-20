import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  api,
  connectEvents,
  EngineError,
  errorMessage,
  initEngine,
  isTauriRuntime,
  isVersionConflict,
  normalizeHistoryCount,
  normalizeQueue,
  normalizeSystem,
  readShellEngineStatus,
  type EventBridge,
} from "./engine";
import type {
  Action,
  ConnectionState,
  Issue,
  IssueSummary,
  Job,
  ModelState,
  ModelStatus,
  ModelsPresent,
  QueueState,
  QueueWire,
  Settings,
  SkippedImport,
  SystemInfo,
  Transcript,
} from "../types";
import { basename, summarize } from "./format";
import { isStaleResponse } from "./race";

/* ---------------- 类型 ---------------- */

export type ToastLevel = "info" | "success" | "warn" | "error";

/** 主导航的三个页面：单条转录、批量队列、设置。 */
export type AppView = "workspace" | "queue" | "settings";

export interface Toast {
  id: number;
  level: ToastLevel;
  message: string;
}

interface BootState {
  stage: string;
  detail: string;
  progress: number;
  connected: boolean;
  /** 旧引擎/mock 的逐组件状态（真实引擎已改为 models_present）。 */
  models: ModelStatus[];
  /** /api/system.model_state：启动门禁的唯一依据（F3）。 */
  modelState: ModelState | null;
  /** /api/system.models_present：模型文件是否齐备。 */
  modelsPresent: ModelsPresent | null;
  elapsedS: number;
  error: string | null;
}

export interface ConfirmOptions {
  title: string;
  description: string;
  confirmText?: string;
  tone?: "default" | "danger";
}

interface ConfirmState extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

interface AppContextValue {
  phase: "boot" | "ready";
  boot: BootState;
  retryBoot: () => void;

  connection: ConnectionState;
  system: SystemInfo | null;
  jobs: Job[];
  queue: QueueState;
  settings: Settings;

  view: AppView;
  setView: (view: AppView) => void;

  selectedJobId: string | null;
  selectedJob: Job | null;
  transcript: Transcript | null;
  issues: Issue[];
  summary: IssueSummary;
  transcriptLoading: boolean;
  transcriptError: string | null;

  mutating: boolean;
  canUndo: boolean;

  selectJob: (id: string) => void;
  clearSelection: () => void;
  /** 编辑条是否有未保存草稿：切任务/返回前据此弹确认（P1-6）。 */
  setEditorDirty: (dirty: boolean) => void;
  setAutoCorrect: (id: string, enabled: boolean) => Promise<boolean>;
  setAllAutoCorrect: (enabled: boolean) => Promise<number>;
  importPaths: (paths: string[]) => Promise<boolean>;
  importFiles: (files: FileList | File[]) => Promise<boolean>;
  importing: boolean;

  applyActions: (actions: Action[], successMessage?: string) => Promise<boolean>;
  updateSentence: (sentenceId: number, text: string) => Promise<boolean>;
  replaceIssue: (issue: Issue, scope: "all" | "selected") => Promise<boolean>;
  fixRepeat: (issue: Issue) => Promise<boolean>;
  dismissIssue: (issue: Issue) => Promise<boolean>;
  applyIssueBatch: (issues: Issue[]) => Promise<boolean>;
  undo: () => Promise<boolean>;

  toggleQueue: () => Promise<void>;
  retryJob: (job: Job) => Promise<void>;
  cancelJob: (job: Job) => Promise<void>;
  deleteJob: (job: Job) => Promise<void>;

  saveSettings: (next: Settings) => Promise<boolean>;

  toasts: Toast[];
  toast: (level: ToastLevel, message: string) => void;
  dismissToast: (id: number) => void;

  confirm: (options: ConfirmOptions) => Promise<boolean>;
  confirmState: ConfirmState | null;
  resolveConfirm: (ok: boolean) => void;
}

export const DEFAULT_SETTINGS: Settings = {
  device: "auto",
  half: true,
  beam_size: 3,
  batch_size: 6,
  gpu_memory_fraction: 0.88,
  vram_headroom_mb: 700,
  window_minutes: 30,
  min_ngram_count: 3,
  fuzzy_pinyin: false,
  subtitle_max_chars: 22,
  subtitle_max_lines: 2,
  subtitle_clean_stutter: true,
  subtitle_clean_fillers: true,
  subtitle_punct_space: true,
  subtitle_merge_gap_ms: 900,
  reduce_motion: false,
  keep_wav: false,
  // 转录完成后自动把 SRT 写到视频所在目录（引擎侧执行）。
  auto_export: true,
  export_dir: "",
};

const EMPTY_QUEUE: QueueState = {
  paused: false,
  active: null,
  pending: [],
  total: 0,
  processing: 0,
  done: 0,
  error: 0,
};

const AppContext = createContext<AppContextValue | null>(null);

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** 模型加载进度：优先引擎给的 load_progress，其次按 model_state 粗估。 */
function modelProgress(sys: SystemInfo): number {
  if (typeof sys.load_progress === "number") {
    return Math.min(1, Math.max(0, sys.load_progress));
  }
  if (sys.model_state === "ready") return 1;
  if (sys.model_state === "loading") return 0.5;
  if (sys.model_state === "idle") return 0.15;
  if (sys.models?.length) {
    const sum = sys.models.reduce(
      (n, m) => n + (m.loaded ? 1 : Math.min(1, Math.max(0, m.progress || 0))),
      0,
    );
    return sum / sys.models.length;
  }
  return sys.loading ? 0.35 : 1;
}

/**
 * 启动门禁（F3）：模型改为按需加载，idle/loading 都不该把用户挡在启动页，
 * 只有 model_state === "failed" 才判定失败（由 bootstrap 出错误页）。
 * 没有 model_state 的旧引擎/mock 才回退到 loading 字段。
 */
function systemReady(sys: SystemInfo | null, waitedMs: number): boolean {
  if (!sys) return waitedMs > 2200;
  if (sys.model_state) return sys.model_state !== "failed";
  if (sys.models?.length && sys.models.every((m) => m.loaded)) return true;
  if (sys.loading === false) return true;
  if (sys.loading === undefined) return true;
  return false;
}

/** 启动页「模型」一栏的文案（F3）。 */
function modelDetail(sys: SystemInfo | null, models: ModelStatus[]): string {
  const state = sys?.model_state;
  if (state === "ready") return "模型已就绪，可以开始转录";
  if (state === "loading") return "正在加载模型（首次转录 20–90 秒）";
  if (state === "idle") return "模型按需加载（首次转录 20–90 秒）";
  if (state === "failed") return "模型加载失败";
  if (models.length) {
    return `${models.filter((m) => m.loaded).length}/${models.length} 个组件就绪`;
  }
  const present = sys?.models_present;
  if (present) {
    const ok = [present.aed, present.vad, present.punc].filter(Boolean).length;
    return `${ok}/3 个模型文件就绪`;
  }
  return "等待引擎返回模型状态";
}

/**
 * 写请求的 CAS 版本（C3）：与服务端 check_version 口径一致，
 * 缺失版本号（旧数据/第三方 mock）时按 1 处理，避免改写被永久拒绝。
 */
function transcriptVersionOf(transcript: Transcript | null | undefined): number {
  const version = transcript?.version;
  return typeof version === "number" && Number.isFinite(version) ? Math.floor(version) : 1;
}

function upsertJobs(prev: Job[], incoming: Job[]): Job[] {
  const map = new Map(prev.map((j) => [j.id, j]));
  for (const job of incoming) map.set(job.id, job);
  return [...map.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

function queueFromJobs(jobs: Job[], paused: boolean): QueueState {
  const processing = jobs.filter((j) => j.status === "processing");
  return {
    paused,
    active: processing[0]?.id ?? null,
    pending: jobs.filter((j) => j.status === "queued").map((j) => j.id),
    total: jobs.length,
    processing: processing.length,
    done: jobs.filter((j) => j.status === "done").length,
    error: jobs.filter((j) => j.status === "error").length,
  };
}

/* ---------------- Provider ---------------- */

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<"boot" | "ready">("boot");
  const [boot, setBoot] = useState<BootState>({
    stage: "正在启动本地引擎",
    detail: isTauriRuntime() ? "由桌面壳拉起离线推理引擎" : "开发模式 · 连接 127.0.0.1:8765",
    progress: 0,
    connected: false,
    models: [],
    modelState: null,
    modelsPresent: null,
    elapsedS: 0,
    error: null,
  });
  const bootRun = useRef(0);

  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [queue, setQueue] = useState<QueueState>(EMPTY_QUEUE);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);

  /* -------- 减少动效偏好同步到根节点 -------- */
  useEffect(() => {
    const root = document.documentElement;
    if (settings.reduce_motion) root.dataset.reduceMotion = "1";
    else delete root.dataset.reduceMotion;
  }, [settings.reduce_motion]);

  const [view, setView] = useState<AppView>("workspace");
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [mutating, setMutating] = useState(false);
  const [undoDepth, setUndoDepth] = useState(0);
  const [importing, setImporting] = useState(false);

  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);

  const selectedRef = useRef<string | null>(null);
  const transcriptJobRef = useRef<string | null>(null);
  /** TaskEditor 同步过来的草稿脏标记：切走会重挂载编辑器，必须先确认（P1-6）。 */
  const editorDirtyRef = useRef(false);
  /**
   * transcript 加载请求序号（C3）：每次加载递增；写操作成功时也递增，
   * 让加载中的旧回包作废，避免旧稿覆盖刚保存的新稿。
   */
  const loadSeq = useRef(0);
  /** 当前面板里这份 transcript 的版本号，写请求用它做 CAS（C3）。 */
  const transcriptVersionRef = useRef<number | null>(null);
  /** /history 请求序号（C4）：慢回包不得覆盖更新的深度。 */
  const historySeq = useRef(0);
  /** 并发中的修改请求计数：过期请求收尾时不能把 mutating 提前关掉（F1）。 */
  const mutSeq = useRef(0);
  const toastSeq = useRef(1);
  const bridgeRef = useRef<EventBridge | null>(null);

  /* -------- toast -------- */
  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    (level: ToastLevel, message: string) => {
      const id = toastSeq.current++;
      setToasts((prev) => [...prev.slice(-3), { id, level, message }]);
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, level === "error" ? 6200 : 4200);
    },
    [],
  );

  /* -------- confirm -------- */
  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        setConfirmState({ ...options, resolve });
      }),
    [],
  );

  const resolveConfirm = useCallback((ok: boolean) => {
    setConfirmState((prev) => {
      prev?.resolve(ok);
      return null;
    });
  }, []);

  /* -------- 加载转录 -------- */
  /** 清空当前转录面板（切任务/删除/重试时统一走这里，版本与撤销深度一起复位）。 */
  const resetTranscript = useCallback(() => {
    // C3：作废在途加载，切任务/删除/重试后旧回包不得再写回面板。
    loadSeq.current += 1;
    setTranscriptLoading(false);
    setTranscript(null);
    transcriptJobRef.current = null;
    transcriptVersionRef.current = null;
    setIssues([]);
    setUndoDepth(0);
  }, []);

  /**
   * 作废所有在途的 transcript 加载（C3）。写操作成功后调用：
   * 之前发出的加载即使是同一 jobId，其回包也已经过时，必须丢弃。
   */
  const invalidatePendingLoads = useCallback(() => {
    loadSeq.current += 1;
    setTranscriptLoading(false);
  }, []);

  /**
   * 撤销可用性由服务端历史深度驱动（C4）：切任务/重连后不再一律禁用撤销。
   * 旧引擎没有 /history（404）时保留当前值，不回退成 0。
   */
  const syncUndoDepth = useCallback(async (jobId: string) => {
    const seq = ++historySeq.current;
    try {
      const res = await api.history(jobId);
      const count = normalizeHistoryCount(res);
      if (count === null) return;
      if (isStaleResponse(seq, historySeq.current, jobId, selectedRef.current)) return;
      setUndoDepth(count);
    } catch {
      // 引擎尚未提供 /history（旧版本 / mock）：保留本地累计的深度。
    }
  }, []);

  const loadTranscript = useCallback(
    async (jobId: string) => {
      const seq = ++loadSeq.current;
      // C3：同一任务的旧回包（重连/重复触发）不得覆盖更新的状态。
      const stale = () => isStaleResponse(seq, loadSeq.current, jobId, selectedRef.current);
      setTranscriptLoading(true);
      setTranscriptError(null);
      transcriptJobRef.current = null;
      setUndoDepth(0);
      try {
        const [t, issueRes] = await Promise.all([
          api.transcript(jobId),
          api.issues(jobId).catch(() => null),
        ]);
        if (stale()) return;
        setTranscript(t);
        transcriptJobRef.current = jobId;
        transcriptVersionRef.current = transcriptVersionOf(t);
        setIssues(issueRes?.issues ?? []);
        void syncUndoDepth(jobId);
      } catch (err) {
        if (stale()) return;
        setTranscript(null);
        transcriptVersionRef.current = null;
        setTranscriptError(errorMessage(err));
      } finally {
        if (!stale()) setTranscriptLoading(false);
      }
    },
    [syncUndoDepth],
  );

  /* -------- 启动 -------- */
  const bootstrap = useCallback(async () => {
    const run = ++bootRun.current;
    const started = Date.now();
    const browser = !isTauriRuntime();
    setPhase("boot");
    setBoot({
      stage: "正在启动本地引擎",
      detail: browser ? "开发模式 · 连接 127.0.0.1:8765" : "由桌面壳拉起离线推理引擎",
      progress: 0,
      connected: false,
      models: [],
      modelState: null,
      modelsPresent: null,
      elapsedS: 0,
      error: null,
    });

    try {
      await initEngine(true, {
        onWait: (status) => {
          if (run !== bootRun.current) return;
          setBoot((prev) => ({
            ...prev,
            stage: "正在启动本地引擎",
            detail: status?.pid
              ? `引擎进程已启动（pid ${status.pid}），等待端口公布…`
              : "正在拉起本地引擎进程…",
            elapsedS: Math.round((Date.now() - started) / 1000),
          }));
        },
      });
    } catch (err) {
      if (run !== bootRun.current) return;
      // 壳已明确报 failed/exited 时 errorMessage 里带的是根因（如路径错误、退出码），
      // 不要再用「引擎进程未能启动」把它盖掉。
      setBoot((prev) => ({
        ...prev,
        error: errorMessage(err),
        detail: "引擎进程未能启动",
      }));
      return;
    }

    let sys: SystemInfo | null = null;
    let failures = 0;
    let ready = false;
    while (Date.now() - started < 180_000) {
      if (run !== bootRun.current) return;
      try {
        const health = await api.health();
        if (!health?.ok) throw new Error("引擎尚未就绪");
        failures = 0;
        sys = await api.system().catch(() => sys);
        if (sys) setSystem(sys);
        const waited = Date.now() - started;
        // 模型加载失败直接出错误页，不再等满 180 秒（F3）。
        if (sys?.model_state === "failed") {
          if (run !== bootRun.current) return;
          setBoot((prev) => ({
            ...prev,
            stage: "模型加载失败",
            detail: "引擎已启动，但语音模型未能加载",
            progress: 0,
            connected: true,
            models: sys?.models ?? [],
            modelState: "failed",
            modelsPresent: sys?.models_present ?? null,
            elapsedS: Math.round(waited / 1000),
            error:
              sys?.error ||
              "模型加载失败。请检查 models 目录是否完整，或在设置里把计算设备切换为 CPU 后重试。",
          }));
          return;
        }
        const p = sys ? modelProgress(sys) : Math.min(0.5, waited / 6000);
        const isReady = systemReady(sys, waited);
        const loaded = sys?.models?.filter((m) => m.loaded).length ?? 0;
        const totalModels = sys?.models?.length ?? 0;
        const models = sys?.models ?? [];
        setBoot({
          stage: isReady
            ? "准备就绪"
            : totalModels
              ? `正在加载模型 · ${loaded}/${totalModels}`
              : "正在初始化推理引擎",
          detail: isReady
            ? modelDetail(sys, models)
            : sys?.device
              ? `推理设备 ${String(sys.device).toUpperCase()}${sys.gpu ? ` · ${sys.gpu}` : ""}`
              : "正在预热推理图与声学模型…",
          progress: isReady ? 1 : Math.max(0.06, p),
          connected: true,
          models,
          modelState: sys?.model_state ?? null,
          modelsPresent: sys?.models_present ?? null,
          elapsedS: Math.round(waited / 1000),
          error: null,
        });
        if (isReady) {
          ready = true;
          break;
        }
      } catch (err) {
        failures += 1;
        const waited = Date.now() - started;
        if (!browser && failures >= 2) {
          // 端口已拿到但健康检查连续失败：先看壳的进程状态。进程已经退出就
          // 立刻报真实原因（含 exit_code/日志提示），不要空等 15 秒再给通用句。
          const shell = await readShellEngineStatus();
          if (shell && (shell.phase === "failed" || shell.phase === "exited")) {
            if (run !== bootRun.current) return;
            setBoot((prev) => ({
              ...prev,
              progress: 0,
              connected: false,
              elapsedS: Math.round(waited / 1000),
              error: shell.error || "引擎进程已退出，请点击「重启引擎」后重试。",
              detail: shell.phase === "exited" ? "引擎进程已退出" : "引擎启动失败",
            }));
            return;
          }
        }
        if (waited > 15_000 && failures >= 3) {
          if (run !== bootRun.current) return;
          setBoot((prev) => ({
            ...prev,
            progress: 0,
            connected: false,
            elapsedS: Math.round(waited / 1000),
            error: browser
              ? "无法连接本地引擎。请确认引擎已启动（8765）。"
              : "无法连接本地引擎。请确认桌面壳已拉起 engine 子进程，然后重试。",
            detail: "连接已中断",
          }));
          return;
        }
        setBoot((prev) => ({
          ...prev,
          stage: "正在连接本地引擎",
          detail: waited > 6000 ? "引擎启动较慢，仍在等待…" : "正在等待引擎响应…",
          elapsedS: Math.round(waited / 1000),
          progress: prev.progress,
        }));
      }
      await sleep(650);
    }

    if (run !== bootRun.current) return;
    if (!ready) {
      setBoot((prev) => ({
        ...prev,
        error: "引擎启动超时（180 秒）。请检查模型文件是否完整，或重启应用后重试。",
        detail: "启动超时",
      }));
      return;
    }

    try {
      const [jobsRes, settingsRes] = await Promise.all([api.jobs(), api.settings()]);
      if (run !== bootRun.current) return;
      setJobs(jobsRes.jobs.sort((a, b) => b.created_at.localeCompare(a.created_at)));
      setSettings({ ...DEFAULT_SETTINGS, ...settingsRes });
      setQueue(queueFromJobs(jobsRes.jobs, false));
      setPhase("ready");
      setConnection("connecting");
    } catch (err) {
      if (run !== bootRun.current) return;
      setBoot((prev) => ({
        ...prev,
        error: `已连接引擎，但读取任务失败：${errorMessage(err)}`,
        detail: "初始化未完成",
      }));
    }
  }, []);

  const retryBoot = useCallback(() => {
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    void bootstrap();
    return () => {
      bootRun.current += 1;
      bridgeRef.current?.dispose();
      bridgeRef.current = null;
    };
  }, [bootstrap]);

  /* -------- WS -------- */
  /** queue 事件与 /api/system.queue 的归一化合并：只取 paused/active，其余由 jobs 派生。 */
  const applyQueue = useCallback((raw: QueueWire) => {
    const wire = normalizeQueue(raw);
    setQueue((prev) => ({
      ...prev,
      paused: typeof wire.paused === "boolean" ? wire.paused : prev.paused,
      active: wire.current ?? wire.active ?? prev.active,
    }));
  }, []);

  const handleWsEvent = useCallback(
    (event: import("../types").WsEvent) => {
      if (event.type === "snapshot") {
        // 连接建立后引擎会先推一份快照（F2）：任务、队列、引擎信息一起对齐。
        const incoming = Array.isArray(event.jobs) ? event.jobs : [];
        if (incoming.length) setJobs((prev) => upsertJobs(prev, incoming));
        if (event.queue) applyQueue(event.queue);
        if (event.engine) setSystem(normalizeSystem(event.engine));
        return;
      }
      if (event.type === "job") {
        const job = event.job;
        // 引擎在 OOM 重试与删除竞态下可能推 job:null，直接 upsert 会抛 TypeError。
        if (!job || !job.id) return;
        setJobs((prev) => upsertJobs(prev, [job]));
        if (job.id === selectedRef.current) {
          if (job.status === "error") {
            setTranscriptError(job.error || "转录失败");
          }
          if (
            job.status === "done" &&
            job.has_transcript &&
            transcriptJobRef.current !== job.id
          ) {
            void loadTranscript(job.id);
          }
        }
        return;
      }
      if (event.type === "queue") {
        applyQueue(event.queue);
        return;
      }
      if (event.type === "engine") {
        setSystem((prev) => normalizeSystem({ ...(prev ?? {}), ...event.engine }));
        return;
      }
      if (event.type === "toast") {
        toast(event.level, event.message);
      }
    },
    [applyQueue, loadTranscript, toast],
  );

  /**
   * 断线/重连后的对齐（F2）：刷新任务列表，并重新拉一次当前选中任务的
   * transcript + issues（loadTranscript 内部已按 selectedRef 做过期判断）。
   */
  const refreshFromEngine = useCallback(async () => {
    let list: Job[] | null = null;
    try {
      const res = await api.jobs();
      list = res.jobs;
      setJobs(res.jobs.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)));
    } catch {
      // 刚重连时引擎可能还没完全就绪，交给下一次重连或轮询。
    }
    const id = selectedRef.current;
    if (!id) return;
    const job = list?.find((item) => item.id === id);
    // 只对已有转录的任务重拉，避免给处理中的任务打上「转录失败」。
    const hasTranscript = job ? job.has_transcript : transcriptJobRef.current === id;
    if (hasTranscript) void loadTranscript(id);
  }, [loadTranscript]);

  useEffect(() => {
    if (phase !== "ready") return;
    bridgeRef.current?.dispose();
    const bridge = connectEvents(handleWsEvent, setConnection, () => {
      void refreshFromEngine();
    });
    bridgeRef.current = bridge;
    return () => {
      bridge.dispose();
      bridgeRef.current = null;
    };
  }, [phase, handleWsEvent, refreshFromEngine]);

  /* -------- 轮询兜底 -------- */
  useEffect(() => {
    if (phase !== "ready" || connection === "online") return;
    let stopped = false;
    const poll = async () => {
      try {
        const res = await api.jobs();
        if (!stopped) setJobs(res.jobs.sort((a, b) => b.created_at.localeCompare(a.created_at)));
      } catch {
        // 离线时保持现有界面，等待 WS 重连。
      }
    };
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [phase, connection]);

  /* -------- 系统信息刷新：模型加载完成后 device 会从 cpu 变为 cuda -------- */
  useEffect(() => {
    if (phase !== "ready") return;
    let stopped = false;
    const refresh = async () => {
      try {
        const sys = await api.system();
        if (!stopped && sys) setSystem(sys);
      } catch {
        // 引擎短暂不可用时保留上次信息。
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 8000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [phase]);

  /* -------- 选中任务 -------- */
  const setEditorDirty = useCallback((dirty: boolean) => {
    editorDirtyRef.current = dirty;
  }, []);

  /** 切任务/返回会重挂载编辑器并丢掉草稿，脏的时候必须先确认（P1-6）。 */
  const confirmDiscardDraft = useCallback(() => {
    if (!editorDirtyRef.current) return true;
    return window.confirm("当前句子有未保存的修改，切换将丢弃。继续？");
  }, []);

  const selectJob = useCallback(
    (id: string) => {
      const job = jobs.find((j) => j.id === id);
      if (!job) return;
      if (id !== selectedRef.current && !confirmDiscardDraft()) return;
      selectedRef.current = id;
      setSelectedJobId(id);
      setView("workspace");
      resetTranscript();
      // 重试失败但旧稿仍在盘上（has_transcript）时不打错误标记，让旧字幕可继续
      // 查看/编辑（P1-1）；只有确实没有转录的错误任务才显示错误页。
      setTranscriptError(job.status === "error" && !job.has_transcript ? job.error : null);
      if (job.has_transcript) void loadTranscript(id);
    },
    [jobs, loadTranscript, resetTranscript, confirmDiscardDraft],
  );

  const clearSelection = useCallback(() => {
    if (selectedRef.current && !confirmDiscardDraft()) return;
    selectedRef.current = null;
    setSelectedJobId(null);
    resetTranscript();
    setTranscriptError(null);
    setView("workspace");
  }, [resetTranscript, confirmDiscardDraft]);

  const selectedJob = useMemo(
    () => jobs.find((j) => j.id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );
  const setAutoCorrect = useCallback(
    async (id: string, enabled: boolean) => {
      const before = jobs.find((j) => j.id === id);
      if (!before) return false;
      setJobs((prev) => prev.map((j) => (j.id === id ? { ...j, auto_correct: enabled } : j)));
      try {
        const updated = await api.setAutoCorrect(id, enabled);
        setJobs((prev) => upsertJobs(prev, [updated]));
        return true;
      } catch (err) {
        setJobs((prev) => prev.map((j) => (j.id === id ? before : j)));
        toast("error", `自动校正设置失败：${errorMessage(err)}`);
        return false;
      }
    },
    [jobs, toast],
  );

  const setAllAutoCorrect = useCallback(
    async (enabled: boolean) => {
      const targets = jobs.filter(
        (j) =>
          (j.status === "queued" || j.status === "processing") &&
          j.auto_correct !== enabled,
      );
      if (!targets.length) return 0;
      let ok = 0;
      await Promise.all(
        targets.map(async (job) => {
          if (await setAutoCorrect(job.id, enabled)) ok += 1;
        }),
      );
      return ok;
    },
    [jobs, setAutoCorrect],
  );


  // 选中的任务被删除时回到空工作台。
  useEffect(() => {
    if (selectedJobId && !jobs.some((j) => j.id === selectedJobId)) {
      selectedRef.current = null;
      setSelectedJobId(null);
      resetTranscript();
    }
  }, [jobs, selectedJobId, resetTranscript]);

  /* -------- 队列派生 -------- */
  // pending/total/done/error 一律由 jobs 派生（引擎的 queue 事件只给 paused/current），
  // 这里只把 WS 侧拿到的 active 作为 jobs 尚未刷新时的过渡值。
  const queueDerived = useMemo<QueueState>(() => {
    const derived = queueFromJobs(jobs, queue.paused);
    return {
      ...derived,
      paused: queue.paused,
      active: derived.active ?? queue.active,
    };
  }, [jobs, queue]);

  /* -------- 导入 -------- */
  /** 把引擎 skipped 明细提示给用户（F4）：SPEC §4 要求逐条列出原因，不得截断。 */
  const reportSkipped = useCallback(
    (skipped: SkippedImport[]) => {
      if (!skipped.length) return;
      const reasons = skipped.map(
        (item) => `${basename(item.path) || "未命名文件"}（${item.reason}）`,
      );
      toast("warn", `${skipped.length} 个文件未被加入：${reasons.join("；")}`);
    },
    [toast],
  );

  const importPaths = useCallback(
    async (paths: string[]) => {
      const clean = paths.map((p) => p.trim()).filter(Boolean);
      if (!clean.length) return false;
      setImporting(true);
      try {
        // 默认「全部自动校正」：导入即开启，用户仍可在队列里逐条关掉。
        const res = await api.createJobs(clean, true);
        reportSkipped(res.skipped);
        if (!res.jobs.length) {
          if (!res.skipped.length) {
            toast("warn", "没有文件被加入队列，请检查文件格式或路径。");
          }
          return false;
        }
        setJobs((prev) => upsertJobs(prev, res.jobs));
        setView("workspace");
        const first = res.jobs[0];
        if (first) {
          selectedRef.current = first.id;
          setSelectedJobId(first.id);
          resetTranscript();
          setTranscriptError(null);
        }
        toast(
          "success",
          res.jobs.length === 1
            ? `已加入队列：${res.jobs[0].name}`
            : `已加入队列：${res.jobs.length} 个文件`,
        );
        return true;
      } catch (err) {
        toast("error", `导入失败：${errorMessage(err)}`);
        return false;
      } finally {
        setImporting(false);
      }
    },
    [reportSkipped, resetTranscript, toast],
  );

  const importFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files);
      if (!list.length) return false;
      // 浏览器无法取得绝对路径：开发模式下用固定前缀伪造，仅 mock 引擎可用。
      const paths = list.map((f) => `D:\\dev-import\\${f.name}`);
      toast("info", "浏览器开发模式：以模拟路径提交，仅 mock 引擎可用。");
      return importPaths(paths);
    },
    [importPaths, toast],
  );

  /* -------- 修改 -------- */
  /** 请求发起时的 jobId 与「当前选中」不一致 → 结果属于过期任务，整份丢弃（F1）。 */
  const isStale = useCallback((jobId: string) => selectedRef.current !== jobId, []);

  const beginMutation = useCallback(() => {
    mutSeq.current += 1;
    setMutating(true);
  }, []);

  const endMutation = useCallback(() => {
    mutSeq.current = Math.max(0, mutSeq.current - 1);
    if (mutSeq.current === 0) setMutating(false);
  }, []);

  const commitMutation = useCallback(
    (
      jobId: string,
      result: { transcript: Transcript; issues: Issue[]; summary: IssueSummary },
    ) => {
      // 竞态守卫：切走了就不写 transcript / issues，也不推进 undo。
      if (selectedRef.current !== jobId) return false;
      // C3：写成功即作废在途的旧加载回包，避免重连的延迟结果把新稿盖回去。
      invalidatePendingLoads();
      setTranscript(result.transcript);
      transcriptJobRef.current = jobId;
      transcriptVersionRef.current = transcriptVersionOf(result.transcript);
      setIssues(result.issues);
      setUndoDepth((n) => n + 1);
      // C4：以服务端历史深度为准（旧引擎没有 /history 时保留上面的本地累计）。
      void syncUndoDepth(jobId);
      setJobs((prev) =>
        prev.map((j) =>
          j.id === jobId
            ? {
                ...j,
                issues_count: result.summary?.total ?? result.issues.length,
                has_transcript: true,
                status: "done",
              }
            : j,
        ),
      );
      return true;
    },
    [invalidatePendingLoads, syncUndoDepth],
  );

  /**
   * 写失败统一出口（C3）：版本冲突说明别处已改过同一份稿，
   * 这里提示并重新拉取最新内容（保留编辑框里的草稿），不做「自动换顶重试」。
   */
  const recoverConflict = useCallback(
    (jobId: string, err: unknown, prefix: string): boolean => {
      if (!isVersionConflict(err)) return false;
      toast(
        "error",
        `${prefix}：字幕已在其他窗口更新，已重新载入最新内容，请确认保存。`,
      );
      void loadTranscript(jobId);
      return true;
    },
    [loadTranscript, toast],
  );

  const applyActions = useCallback(
    async (actions: Action[], successMessage?: string) => {
      const jobId = selectedJobId; // 发起时捕获，返回后用它判断是否已被切走
      const version = transcriptVersionRef.current;
      if (!jobId || !transcript || version == null) return false;
      beginMutation();
      try {
        // C3：所有写请求带 expected_version；服务端版本不同则 409，不覆盖别处的新稿。
        const result = await api.apply(jobId, actions, version);
        if (!commitMutation(jobId, result)) return false;
        if (successMessage) toast("success", successMessage);
        return true;
      } catch (err) {
        if (isStale(jobId)) return false;
        if (!recoverConflict(jobId, err, "应用失败")) {
          toast("error", `应用失败：${errorMessage(err)}`);
        }
        return false;
      } finally {
        endMutation();
      }
    },
    [
      selectedJobId,
      transcript,
      commitMutation,
      toast,
      beginMutation,
      endMutation,
      isStale,
      recoverConflict,
    ],
  );

  const updateSentence = useCallback(
    async (sentenceId: number, text: string) => {
      const jobId = selectedJobId;
      const version = transcriptVersionRef.current;
      if (!jobId || version == null) return false;
      beginMutation();
      try {
        const result = await api.updateSentence(jobId, sentenceId, text, version);
        if (!commitMutation(jobId, result)) return false;
        toast("success", "字幕已保存");
        return true;
      } catch (err) {
        if (isStale(jobId)) return false;
        if (!recoverConflict(jobId, err, "保存失败")) {
          toast("error", `保存失败：${errorMessage(err)}`);
        }
        return false;
      } finally {
        endMutation();
      }
    },
    [selectedJobId, commitMutation, toast, beginMutation, endMutation, isStale, recoverConflict],
  );

  const replaceIssue = useCallback(
    (issue: Issue, scope: "all" | "selected") => {
      const label =
        scope === "all"
          ? `已将全部「${issue.text}」替换为「${issue.suggestion}」`
          : `已替换为「${issue.suggestion}」`;
      return applyActions(
        [
          {
            type: "replace",
            from: issue.text,
            to: issue.suggestion,
            scope,
            sentence_ids: scope === "selected" ? [issue.sentence_id] : [],
          },
        ],
        label,
      );
    },
    [applyActions],
  );

  const fixRepeat = useCallback(
    (issue: Issue) =>
      applyActions(
        [
          {
            type: "repeat",
            sentence_id: issue.sentence_id,
            start: issue.start,
            end: issue.end,
          },
        ],
        `已去除重复「${issue.text}」`,
      ),
    [applyActions],
  );

  const dismissIssue = useCallback(
    (issue: Issue) =>
      applyActions([{ type: "dismiss", issue_id: issue.id }], "已忽略该提示"),
    [applyActions],
  );

  const applyIssueBatch = useCallback(
    async (batch: Issue[]) => {
      const repeats = batch
        .filter((issue) => issue.kind === "repeat")
        .sort((a, b) => b.start - a.start);
      const replaceMap = new Map<string, Issue>();
      for (const issue of batch) {
        if (issue.kind !== "suspicious" || !issue.suggestion) continue;
        replaceMap.set(`${issue.text}\u0000${issue.suggestion}`, issue);
      }
      const actions: Action[] = [
        ...repeats.map((issue) => ({
          type: "repeat" as const,
          sentence_id: issue.sentence_id,
          start: issue.start,
          end: issue.end,
        })),
        ...[...replaceMap.values()].map((issue) => ({
          type: "replace" as const,
          from: issue.text,
          to: issue.suggestion,
          scope: "all" as const,
          sentence_ids: [] as number[],
        })),
      ];
      if (!actions.length) return false;
      return applyActions(actions, `已批量处理 ${batch.length} 条建议`);
    },
    [applyActions],
  );

  const undo = useCallback(async () => {
    const jobId = selectedJobId;
    const version = transcriptVersionRef.current;
    if (!jobId || version == null) return false;
    beginMutation();
    try {
      // C3：撤销同样做 CAS（版本走查询参数），避免把别处的新稿回退掉。
      const result = await api.undo(jobId, version);
      // 竞态守卫：撤销结果只对发起时的任务有效（F1）。
      if (isStale(jobId)) return false;
      if (!result.undone) {
        setUndoDepth(0);
        toast("info", "没有可撤销的修改");
        return false;
      }
      // 撤销成功同样是「写」：作废在途的旧加载，并同步新版本号。
      invalidatePendingLoads();
      setTranscript(result.transcript);
      transcriptJobRef.current = jobId;
      transcriptVersionRef.current = transcriptVersionOf(result.transcript);
      setIssues(result.issues);
      setUndoDepth((n) => Math.max(0, n - 1));
      // C4：撤销会消耗一条历史，深度以服务端为准。
      void syncUndoDepth(jobId);
      setJobs((prev) =>
        prev.map((j) =>
          j.id === jobId
            ? { ...j, issues_count: result.summary?.total ?? result.issues.length }
            : j,
        ),
      );
      toast("success", "已撤销上一步修改");
      return true;
    } catch (err) {
      if (isStale(jobId)) return false;
      if (err instanceof EngineError && err.status === 409 && !isVersionConflict(err)) {
        // 服务端撤销栈为空（409「没有可撤销的操作」）：只更新可用性，不当作故障。
        setUndoDepth(0);
        toast("info", "没有可撤销的修改");
        return false;
      }
      if (!recoverConflict(jobId, err, "撤销失败")) {
        toast("error", `撤销失败：${errorMessage(err)}`);
      }
      return false;
    } finally {
      endMutation();
    }
  }, [
    selectedJobId,
    toast,
    beginMutation,
    endMutation,
    isStale,
    invalidatePendingLoads,
    syncUndoDepth,
    recoverConflict,
  ]);

  /* -------- 队列操作 -------- */
  const toggleQueue = useCallback(async () => {
    const next = !queueDerived.paused;
    try {
      await api.setQueuePaused(next);
      setQueue((prev) => ({ ...prev, paused: next }));
      toast("info", next ? "队列已暂停，当前任务会在完成本段后停下" : "队列已继续");
    } catch (err) {
      toast("error", `操作失败：${errorMessage(err)}`);
    }
  }, [queueDerived.paused, toast]);

  const retryJob = useCallback(
    async (job: Job) => {
      try {
        const updated = await api.retryJob(job.id);
        setJobs((prev) => upsertJobs(prev, [updated]));
        toast("info", `已重新排队：${job.name}`);
        if (selectedRef.current === job.id) {
          resetTranscript();
          setTranscriptError(null);
        }
      } catch (err) {
        toast("error", `重试失败：${errorMessage(err)}`);
      }
    },
    [resetTranscript, toast],
  );

  const cancelJob = useCallback(
    async (job: Job) => {
      try {
        const updated = await api.cancelJob(job.id);
        setJobs((prev) => upsertJobs(prev, [updated]));
        toast("info", `已取消：${job.name}`);
      } catch (err) {
        toast("error", `取消失败：${errorMessage(err)}`);
      }
    },
    [toast],
  );

  const deleteJob = useCallback(
    async (job: Job) => {
      const ok = await confirm({
        title: "删除任务",
        description: `将删除「${job.name}」及其转录结果，此操作不可恢复。`,
        confirmText: "删除",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await api.deleteJob(job.id);
        setJobs((prev) => prev.filter((j) => j.id !== job.id));
        if (selectedRef.current === job.id) {
          selectedRef.current = null;
          setSelectedJobId(null);
          resetTranscript();
        }
        toast("success", "任务已删除");
      } catch (err) {
        toast("error", `删除失败：${errorMessage(err)}`);
      }
    },
    [confirm, resetTranscript, toast],
  );

  /* -------- 设置 -------- */
  const saveSettings = useCallback(
    async (next: Settings) => {
      try {
        const saved = await api.saveSettings(next);
        setSettings({ ...DEFAULT_SETTINGS, ...saved });
        toast("success", "设置已保存");
        return true;
      } catch (err) {
        toast("error", `保存设置失败：${errorMessage(err)}`);
        return false;
      }
    },
    [toast],
  );

  const summary = useMemo(() => summarize(issues), [issues]);

  const value: AppContextValue = {
    phase,
    boot,
    retryBoot,
    connection,
    system,
    jobs,
    queue: queueDerived,
    settings,
    view,
    setView,
    selectedJobId,
    selectedJob,
    transcript,
    issues,
    summary,
    transcriptLoading,
    transcriptError,
    mutating,
    canUndo: undoDepth > 0,
    selectJob,
    clearSelection,
    setEditorDirty,
    setAutoCorrect,
    setAllAutoCorrect,
    importPaths,
    importFiles,
    importing,
    applyActions,
    updateSentence,
    replaceIssue,
    fixRepeat,
    dismissIssue,
    applyIssueBatch,
    undo,
    toggleQueue,
    retryJob,
    cancelJob,
    deleteJob,
    saveSettings,
    toasts,
    toast,
    dismissToast,
    confirm,
    confirmState,
    resolveConfirm,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp 必须在 AppProvider 内使用");
  return ctx;
}
