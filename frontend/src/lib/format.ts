import type {
  ExportFormat,
  IssueSummary,
  Issue,
  Job,
  JobStage,
  JobStatus,
  Sentence,
  Settings,
  Transcript,
} from "../types";
import { buildSubtitleCues, cleanSubtitleText, wrapSubtitle } from "./subtitle";

export function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v));
}

/** 00:12.4 —— 用于播放器与字幕行，紧凑且等宽。 */
export function fmtClock(ms: number, withTenths = true): string {
  const total = Math.max(0, Math.round(ms));
  const h = Math.floor(total / 3_600_000);
  const m = Math.floor((total % 3_600_000) / 60_000);
  const s = Math.floor((total % 60_000) / 1000);
  const d = Math.floor((total % 1000) / 100);
  const body = h > 0
    ? `${h}:${pad(m, 2)}:${pad(s, 2)}`
    : `${pad(m, 2)}:${pad(s, 2)}`;
  return withTenths ? `${body}.${d}` : body;
}

/** 41:12 / 1:02:33 —— 用于任务元信息。 */
export function fmtDuration(ms: number | null | undefined): string {
  if (!ms || ms < 0) return "--:--";
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad(m, 2)}:${pad(s, 2)}` : `${pad(m, 2)}:${pad(s, 2)}`;
}

/** SRT 时间码 00:01:02,345 */
export function fmtSrtTime(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const h = Math.floor(total / 3_600_000);
  const m = Math.floor((total % 3_600_000) / 60_000);
  const s = Math.floor((total % 60_000) / 1000);
  const milli = total % 1000;
  return `${pad(h, 2)}:${pad(m, 2)}:${pad(s, 2)},${pad(milli, 3)}`;
}

/** VTT 时间码 00:01:02.345 */
export function fmtVttTime(ms: number): string {
  return fmtSrtTime(ms).replace(",", ".");
}

/** ASS 时间码 0:01:02.34 */
export function fmtAssTime(ms: number): string {
  const total = Math.max(0, Math.round(ms / 10));
  const h = Math.floor(total / 360_000);
  const m = Math.floor((total % 360_000) / 6000);
  const s = Math.floor((total % 6000) / 100);
  const cs = total % 100;
  return `${h}:${pad(m, 2)}:${pad(s, 2)}.${pad(cs, 2)}`;
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

export function fmtCount(n: number): string {
  return n.toLocaleString("zh-CN");
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = `${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}`;
  if (sameDay) return `今天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) {
    return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  }
  return `${d.getFullYear()}-${pad(d.getMonth() + 1, 2)}-${pad(d.getDate(), 2)}`;
}

export function pad(n: number, len: number): string {
  return String(Math.max(0, n)).padStart(len, "0");
}

export function stageLabel(stage: JobStage): string {
  switch (stage) {
    case "queued":
      return "排队中";
    case "load":
      return "加载模型";
    case "probe":
      return "读取媒体";
    case "extract":
      return "提取音轨";
    case "vad":
      return "语音检测";
    case "asr":
      return "语音识别";
    case "punc":
      return "标点恢复";
    case "finalize":
      return "写入结果";
    case "done":
      return "已完成";
    case "error":
      return "失败";
    case "canceled":
      return "已取消";
    default:
      return "处理中";
  }
}

export function statusLabel(status: JobStatus): string {
  switch (status) {
    case "queued":
      return "排队中";
    case "processing":
      return "转录中";
    case "done":
      return "已完成";
    case "error":
      return "失败";
    case "canceled":
      return "已取消";
    default:
      return "未知";
  }
}

export function statusTone(status: JobStatus): "ok" | "warn" | "err" | "muted" | "accent" {
  switch (status) {
    case "done":
      return "ok";
    case "processing":
      return "accent";
    case "queued":
      return "warn";
    case "error":
      return "err";
    default:
      return "muted";
  }
}

export function confTone(conf: number): "ok" | "warn" | "err" {
  if (conf >= 0.85) return "ok";
  if (conf >= 0.55) return "warn";
  return "err";
}

export function basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

export function extname(path: string): string {
  const name = basename(path);
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function emptySummary(): IssueSummary {
  return { suspicious: 0, repeat: 0, lowconf: 0, total: 0 };
}

export function summarize(issues: Issue[]): IssueSummary {
  const live = issues.filter((i) => !i.resolved);
  return {
    suspicious: live.filter((i) => i.kind === "suspicious").length,
    repeat: live.filter((i) => i.kind === "repeat").length,
    lowconf: live.filter((i) => i.kind === "lowconf").length,
    total: live.length,
  };
}

/* ---------------- 字幕导出（浏览器回退，不依赖引擎） ---------------- */

const FMT_LABEL: Record<ExportFormat, string> = {
  srt: "SRT",
  vtt: "VTT",
  ass: "ASS",
  txt: "TXT",
  json: "JSON",
};

export function formatLabel(f: ExportFormat): string {
  return FMT_LABEL[f];
}

export function buildSubtitle(
  format: ExportFormat,
  transcript: Transcript,
  settings?: Settings | null,
): string {
  const { media, stats } = transcript;
  const maxChars = settings?.subtitle_max_chars ?? 22;
  const maxLines = settings?.subtitle_max_lines ?? 2;
  const cues = buildSubtitleCues(transcript, settings);
  switch (format) {
    case "srt":
      return cues
        .map(
          (c, i) =>
            `${i + 1}\n${fmtSrtTime(c.start)} --> ${fmtSrtTime(c.end)}\n${wrapSubtitle(c.text, maxChars, maxLines)}`,
        )
        .join("\n\n");
    case "vtt":
      return `WEBVTT\n\n${cues
        .map(
          (c) =>
            `${fmtVttTime(c.start)} --> ${fmtVttTime(c.end)}\n${wrapSubtitle(c.text, maxChars, maxLines)}`,
        )
        .join("\n\n")}`;
    case "ass": {
      const head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Microsoft YaHei UI,54,&H00FFFFFF,&H000000FF,&H00101010,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,54,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      ].join("\n");
      const body = cues
        .map(
          (c) =>
            `Dialogue: 0,${fmtAssTime(c.start)},${fmtAssTime(c.end)},Default,,0,0,0,,${wrapSubtitle(c.text, maxChars, maxLines).replace(/\n/g, "\\N")}`,
        )
        .join("\n");
      return `${head}\n${body}\n`;
    }
    case "txt":
      return cues
        .map((c) => `[${fmtClock(c.start, false)}] ${cleanSubtitleText(c.text, settings)}`)
        .join("\n");
    case "json":
      return JSON.stringify(
        {
          job_id: transcript.job_id,
          media: { name: media.name, duration_ms: media.duration_ms },
          stats,
          sentences: transcript.sentences.map((s) => ({
            id: s.id,
            start_ms: s.start_ms,
            end_ms: s.end_ms,
            text: s.text,
            conf: s.conf,
            words: s.words,
            edited: s.edited,
          })),
        },
        null,
        2,
      );
    default:
      return "";
  }
}

export function concatSentences(sentences: Sentence[]): string {
  return sentences
    .map((s) => s.text)
    .join("")
    .replace(/\s+/g, "")
    .length.toString();
}

export function jobEta(job: Job): string {
  if (job.status !== "processing" || !job.progress || job.progress <= 0.01) return "计算中";
  if (!job.started_at) return "计算中";
  const started = new Date(job.started_at).getTime();
  const elapsed = Date.now() - started;
  if (Number.isNaN(started) || elapsed <= 0) return "计算中";
  const remain = (elapsed / job.progress) * (1 - job.progress);
  if (remain > 3600_000) return "> 1 小时";
  return `约 ${Math.max(1, Math.round(remain / 60000))} 分钟`;
}
