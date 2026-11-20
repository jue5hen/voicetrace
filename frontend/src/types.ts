// 与 docs/SPEC.md 第 3 节严格对应的类型定义。
export type JobStatus = "queued" | "processing" | "done" | "error" | "canceled";
export type JobStage =
  | "queued"
  | "load"
  | "probe"
  | "extract"
  | "vad"
  | "asr"
  | "punc"
  | "finalize"
  | "done"
  | "error"
  | "canceled";

export interface Job {
  id: string;
  name: string;
  path: string;
  size_bytes: number;
  duration_ms: number;
  status: JobStatus;
  stage: JobStage;
  progress: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  rtf: number | null;
  width: number | null;
  height: number | null;
  has_transcript: boolean;
  issues_count: number;
  auto_correct: boolean;
  thumb_url?: string | null;
}

/** 引擎模型加载状态：模型按需加载，idle 与 ready 都能直接接新任务。 */
export type ModelState = "idle" | "loading" | "ready" | "failed";

/** 模型文件是否齐备，替代旧的 models 字段。 */
export interface ModelsPresent {
  aed: boolean;
  vad: boolean;
  punc: boolean;
}

/** 单个模型的下载状态（/api/system.models_download.models）。 */
export interface ModelDownloadItem {
  label: string;
  ready: boolean;
  bytes: number;
  total: number;
}

export type ModelDownloadState = "idle" | "running" | "done" | "error" | "canceled";

/** 离线模型下载状态；由引擎在 /api/system 与 /api/models 中返回。 */
export interface ModelDownloadStatus {
  state: ModelDownloadState;
  current: string | null;
  progress: number;
  downloaded_bytes: number;
  total_bytes: number;
  error: string | null;
  repos: string[];
  models: Record<string, ModelDownloadItem>;
}

/** POST /api/jobs 中被引擎拒绝的条目。 */
export interface SkippedImport {
  path: string;
  reason: string;
}

/**
 * WS 上的 queue 快照原始形态：引擎推 {paused, pending:number, current:string|null}，
 * 与前端派生的 QueueState（pending 是 id 数组）不同，必须归一后再合并。
 */
export interface QueueWire {
  paused?: boolean;
  pending?: number;
  current?: string | null;
  /** 旧引擎/mock 的完整快照字段。 */
  active?: string | null;
  total?: number;
  processing?: number;
  done?: number;
  error?: number;
}

export interface Word {
  text: string;
  start_ms: number;
  end_ms: number;
}

export interface Sentence {
  id: number;
  start_ms: number;
  end_ms: number;
  text: string;
  conf: number;
  words: Word[];
  edited: boolean;
}

export interface MediaInfo {
  name: string;
  path: string;
  duration_ms: number;
  width: number | null;
  height: number | null;
}

export interface TranscriptStats {
  chars: number;
  sentences: number;
  duration_ms: number;
  rtf: number;
  elapsed_s: number;
}

export interface Transcript {
  job_id: string;
  version: number;
  media: MediaInfo;
  sentences: Sentence[];
  stats: TranscriptStats;
}

export type IssueKind = "suspicious" | "repeat" | "lowconf";

export interface Issue {
  id: string;
  kind: IssueKind;
  sentence_id: number;
  start: number;
  end: number;
  text: string;
  suggestion: string;
  count: number;
  variant_count: number;
  score: number;
  reason: string;
  resolved: boolean;
}

export interface IssueSummary {
  suspicious: number;
  repeat: number;
  lowconf: number;
  total: number;
}

export interface IssuesResponse {
  issues: Issue[];
  summary: IssueSummary;
}

export type ReplaceScope = "all" | "selected";

export type Action =
  | {
      type: "replace";
      from: string;
      to: string;
      scope: ReplaceScope;
      sentence_ids: number[];
    }
  | { type: "repeat"; sentence_id: number; start: number; end: number }
  | { type: "dismiss"; issue_id: string };

export interface Settings {
  device: "auto" | "cuda" | "cpu";
  half: boolean;
  beam_size: number;
  batch_size: number;
  gpu_memory_fraction: number;
  vram_headroom_mb: number;
  window_minutes: number;
  min_ngram_count: number;
  fuzzy_pinyin: boolean;
  subtitle_max_chars: number;
  subtitle_max_lines: number;
  subtitle_clean_stutter: boolean;
  subtitle_clean_fillers: boolean;
  subtitle_punct_space: boolean;
  subtitle_merge_gap_ms: number;
  reduce_motion: boolean;
  keep_wav: boolean;
  auto_export: boolean;
  export_dir: string;
}

export interface QueueState {
  paused: boolean;
  active: string | null;
  pending: string[];
  total: number;
  processing: number;
  done: number;
  error: number;
}

export interface ModelStatus {
  key: string;
  label: string;
  loaded: boolean;
  progress: number;
  detail?: string;
}

export interface GpuInfo {
  name?: string;
  device?: string;
  cuda?: string | boolean;
  vram_mb?: number;
  free_mb?: number;
}

export interface SystemInfo {
  version?: string;
  device?: string;
  /** 引擎可能返回字符串或结构化信息，归一化后始终是可展示的字符串。 */
  gpu?: string;
  gpu_info?: GpuInfo;
  ready?: boolean;
  /** @deprecated 模型按需加载后启动门禁只看 model_state；仅旧引擎/mock 返回。 */
  loading?: boolean;
  load_progress?: number;
  /** 模型加载状态（F3 启动门禁的唯一依据）。 */
  model_state?: ModelState;
  /** 模型文件是否齐备。 */
  models_present?: ModelsPresent;
  /** 离线模型下载状态（1.0.0 起）。 */
  models_download?: ModelDownloadStatus;
  /** 模型加载失败原因（model_state === "failed" 时有效）。 */
  error?: string | null;
  /** 引擎采样率。 */
  sample_rate?: number;
  /** 旧引擎/mock 的逐组件状态；真实引擎已改用 models_present。 */
  models?: ModelStatus[];
  paths?: {
    data_dir?: string;
    models_dir?: string;
    export_dir?: string;
    /** 真实引擎字段别名 */
    data?: string;
    models?: string;
  };
  cache_bytes?: number;
  queue?: QueueWire;
}

export type ExportFormat = "srt" | "vtt" | "ass" | "txt" | "json";

export type WsEvent =
  | { type: "snapshot"; jobs: Job[]; queue?: QueueWire; engine?: SystemInfo }
  | { type: "job"; job: Job }
  | { type: "queue"; queue: QueueWire }
  | { type: "engine"; engine: SystemInfo }
  | { type: "toast"; level: "info" | "warn" | "error"; message: string };

export type ConnectionState = "connecting" | "online" | "polling";

export interface ImportResult {
  jobs: Job[];
  skipped: SkippedImport[];
}
