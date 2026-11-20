import type { Sentence, Settings, Transcript } from "../types";

const FILLERS = "嗯呃额哦噢唉哎啊呀呐";
const SEP = "[\\s，。！？；：、,.!?;:]";
const BREAK_CHARS = "，。！？；：、,.!?;:…—";

export interface SubtitleCue {
  start: number;
  end: number;
  text: string;
  sentenceId: number;
}

export interface PreviewCue extends SubtitleCue {
  chars: number;
  cps: number;
  lines: number;
}

export function contentLen(text: string): number {
  return Array.from(text).filter(isContent).length;
}

// 与引擎 textutil.is_punct 同口径：Unicode 标点（P）、空白（Z）、符号（S）
// 都不算内容字符；控制字符（C，含换行）也必须排除，否则换行会被算成内容字。
// 旧实现只排除 BREAK_CHARS 里的少数几种标点，导致「“你好”」在预览里算 4 字、
// 引擎 stats.chars 算 2 字（SPEC §3.2 要求一致）。
const NON_CONTENT = /[\p{P}\p{Z}\p{S}\p{C}]/u;

function isContent(ch: string): boolean {
  return !NON_CONTENT.test(ch);
}

export function cleanSubtitleText(
  text: string,
  settings?: Partial<Pick<Settings, "subtitle_clean_fillers" | "subtitle_punct_space">> | null,
): string {
  let out = text ?? "";
  if (settings?.subtitle_clean_fillers ?? true) {
    out = out.replace(new RegExp(`(^|${SEP})(?:[${FILLERS}])+(?=${SEP}|$)`, "g"), "$1");
    out = out.replace(new RegExp(`^(?:[${FILLERS}])+[，,、\\s]*`), "");
    out = out.replace(/^[，,、\s]+/, "");
    out = out.replace(
      /([^\s，。！？；：、,.!?;:])[啊呀哦](?=[\s，。！？；：、,.!?;:]|$)/g,
      (match, prev: string) => (prev === "天" ? match : prev),
    );
    out = out.replace(new RegExp(`([，,、])\\s*(?:[${FILLERS}])+(?=${SEP}|$)`, "g"), "$1");
    out = out.replace(/\s{2,}/g, " ").replace(/\s+([，。！？；：、,.!?;:])/g, "$1");
  }
  if (settings?.subtitle_punct_space ?? true) {
    out = out
      .replace(/\s*[，。、]\s*/g, " ")
      .replace(/[ \t]{2,}/g, " ")
      .replace(/\s+([！？!?])/g, "$1");
  }
  return out.trim();
}

function charTimes(sentence: Sentence): Array<[string, number, number]> {
  const chars = Array.from(sentence.text).filter(isContent);
  if (!chars.length) return [];
  const start = sentence.start_ms;
  const end = Math.max(start + 1, sentence.end_ms);
  const flat: Array<[string, number, number]> = [];
  for (const word of sentence.words ?? []) {
    const wordChars = Array.from(word.text).filter(isContent);
    if (!wordChars.length) continue;
    const ws = word.start_ms;
    const we = Math.max(ws + 1, word.end_ms);
    wordChars.forEach((ch, i) => {
      flat.push([ch, ws + ((we - ws) * i) / wordChars.length, ws + ((we - ws) * (i + 1)) / wordChars.length]);
    });
  }
  if (flat.length === chars.length) return flat;
  return chars.map((ch, i) => [ch, start + ((end - start) * i) / chars.length, start + ((end - start) * (i + 1)) / chars.length]);
}

interface Unit {
  text: string;
  c0: number;
  c1: number;
  start: number;
  end: number;
}

function makeUnit(text: string, c0: number, c1: number, times: Array<[string, number, number]>): Unit {
  return {
    text: text.trim(),
    c0,
    c1,
    start: times[c0][1],
    end: times[c1 - 1][2],
  };
}

function splitUnit(unit: Unit, maxChars: number, maxMs: number, times: Array<[string, number, number]>): Unit[] {
  const n = unit.c1 - unit.c0;
  const duration = unit.end - unit.start;
  if (n <= maxChars && duration <= maxMs) return [unit];
  const k = Math.max(2, Math.ceil(n / maxChars), Math.ceil(duration / maxMs));
  if (k <= 1 || n < 2) return [unit];
  const chars = Array.from(unit.text);
  const offsets: number[] = [];
  const contentOffsets: number[] = [];
  chars.forEach((ch, i) => {
    if (isContent(ch)) {
      offsets.push(i);
      contentOffsets.push(i);
    }
  });
  const pieces: Unit[] = [];
  let prev = 0;
  let prevCut = 0;
  for (let idx = 1; idx < k; idx += 1) {
    const target = Math.round((n * idx) / k);
    let boundary = Math.max(prev + 1, Math.min(n - 1, target));
    // Prefer a nearby word boundary; falls back to the balanced position.
    for (let delta = 0; delta <= 4; delta += 1) {
      const candidate = target + delta;
      if (candidate > prev + 1 && candidate < n - 1) {
        boundary = candidate;
        break;
      }
    }
    const cut = contentOffsets[boundary] ?? chars.length;
    pieces.push(makeUnit(chars.slice(prevCut, cut).join(""), unit.c0 + prev, unit.c0 + boundary, times));
    prev = boundary;
    prevCut = cut;
  }
  pieces.push(makeUnit(chars.slice(prevCut).join(""), unit.c0 + prev, unit.c1, times));
  return pieces.flatMap((p) => splitUnit(p, maxChars, maxMs, times));
}

function packUnits(units: Unit[], maxChars: number, maxMs: number, mergeGapMs: number): Unit[] {
  const groups: Unit[] = [];
  let current: Unit | null = null;
  for (const unit of units) {
    if (!current) {
      current = { ...unit };
      continue;
    }
    const gap = unit.start - current.end;
    const combined = current.c1 - current.c0 + (unit.c1 - unit.c0);
    if (gap <= mergeGapMs && combined <= maxChars && unit.end - current.start <= maxMs) {
      current = {
        ...current,
        text: `${current.text}${unit.text}`,
        c1: unit.c1,
        end: unit.end,
      };
    } else {
      groups.push(current);
      current = { ...unit };
    }
  }
  if (current) groups.push(current);
  return groups;
}

function splitSentence(
  sentence: Sentence,
  maxChars: number,
  maxMs: number,
  mergeGapMs: number,
): Unit[] {
  const times = charTimes(sentence);
  if (!times.length) return [];
  const units: Unit[] = [];
  let textStart = 0;
  let contentStart = 0;
  let contentCount = 0;
  Array.from(sentence.text).forEach((ch, i) => {
    if (isContent(ch)) contentCount += 1;
    else if (BREAK_CHARS.includes(ch) && contentCount > contentStart) {
      units.push(makeUnit(sentence.text.slice(textStart, i + 1), contentStart, contentCount, times));
      textStart = i + 1;
      contentStart = contentCount;
    }
  });
  if (contentCount > contentStart) {
    units.push(makeUnit(sentence.text.slice(textStart), contentStart, contentCount, times));
  }
  const split = units.flatMap((u) => splitUnit(u, maxChars, maxMs, times));
  return packUnits(split, maxChars, maxMs, mergeGapMs);
}

const MAX_CUE_MS = 6500;
const MIN_CUE_MS = 900;
const GAP_MS = 40;
/** 两条 cue 起点差小于这个值就无法各自成条（900ms 最短时长会压住下一条），合并。 */
const MERGE_CLOSER_THAN_MS = 420;
const READING_CPS = 8.5;

/**
 * 与引擎 segmentation.clamp_cue_end 同一策略：40ms 条间间隔**绝对优先**，
 * 900ms 最短时长与语速下限都让位于它（旧实现保留 200ms 硬底，句间距
 * <240ms 时必然重叠）。密集语流里允许本条极短，只保证正时长、不压下一条。
 */
function clampCueEnd(start: number, end: number, chars: number, nextStart?: number): number {
  let out = Math.max(Math.round(end), start + Math.round((chars * 1000) / READING_CPS));
  out = Math.max(out, start + MIN_CUE_MS);
  if (nextStart !== undefined) out = Math.min(out, nextStart - GAP_MS);
  out = Math.min(out, start + MAX_CUE_MS);
  return Math.max(out, start + 1);
}

export function buildSubtitleCues(
  transcript: Transcript,
  settings?: Partial<
    Pick<
      Settings,
      | "subtitle_max_chars"
      | "subtitle_merge_gap_ms"
      | "subtitle_clean_fillers"
      | "subtitle_punct_space"
    >
  > | null,
): SubtitleCue[] {
  const maxChars = settings?.subtitle_max_chars ?? 22;
  // F7：合并间隔跟随设置，不再硬编码 900ms（默认值与 DEFAULT_SETTINGS 一致）。
  const mergeGapMs = Math.max(0, settings?.subtitle_merge_gap_ms ?? 900);
  const maxMs = MAX_CUE_MS;
  const mediaEnd = Math.max(0, Math.round(transcript.media?.duration_ms ?? 0));

  // 1) 切分 + 清洗，先只收集，不改时间。
  const raw: SubtitleCue[] = [];
  for (const sentence of transcript.sentences) {
    const units = splitSentence(sentence, maxChars, maxMs, mergeGapMs);
    for (const unit of units) {
      const text = cleanSubtitleText(unit.text, settings);
      if (!text) continue;
      raw.push({
        start: Math.max(0, Math.round(unit.start)),
        end: Math.round(unit.end),
        text,
        sentenceId: sentence.id,
      });
    }
  }

  // 2) 起点过近的合并：否则 900ms 最短时长无论如何都会压住下一条（回归：
  //    「好，我们今天就…」这种首字成句会切出 0.1 秒的第一条 cue）。
  const merged: SubtitleCue[] = [];
  for (const cue of raw) {
    const prev = merged[merged.length - 1];
    const tooClose = prev !== undefined && cue.start - prev.start < MERGE_CLOSER_THAN_MS;
    const fits = prev !== undefined && contentLen(prev.text + cue.text) <= maxChars * 2;
    if (prev && tooClose && fits) {
      prev.text = (prev.text + " " + cue.text).trim();
      prev.end = Math.max(prev.end, cue.end);
      continue;
    }
    merged.push({ ...cue });
  }

  // 3) 统一钳制：间隔优先、正时长、不算超 6.5 秒、末条不越过媒体时长。
  const cues: SubtitleCue[] = [];
  for (let i = 0; i < merged.length; i += 1) {
    const cur = merged[i];
    const nextStart = i + 1 < merged.length ? merged[i + 1].start : undefined;
    let end = clampCueEnd(cur.start, cur.end, contentLen(cur.text), nextStart);
    if (mediaEnd > cur.start) {
      end = Math.max(cur.start + 1, Math.min(end, mediaEnd));
    }
    cues.push({ start: cur.start, end, text: cur.text, sentenceId: cur.sentenceId });
  }
  return cues;
}

export function wrapSubtitle(text: string, maxChars: number, maxLines: number): string {
  if (contentLen(text) <= maxChars) return text;
  const lines: string[] = [];
  let rest = text;
  while (rest && lines.length < maxLines) {
    if (contentLen(rest) <= maxChars) {
      lines.push(rest.trim());
      rest = "";
      break;
    }
    let cut = 0;
    let count = 0;
    const chars = Array.from(rest);
    for (let i = 0; i < chars.length; i += 1) {
      if (isContent(chars[i])) count += 1;
      if (count >= maxChars) {
        cut = i + 1;
        for (let j = i; j >= Math.max(0, i - 4); j -= 1) {
          if (BREAK_CHARS.includes(chars[j]) || chars[j] === " ") {
            cut = j + 1;
            break;
          }
        }
        break;
      }
    }
    if (cut <= 0) cut = chars.length;
    lines.push(chars.slice(0, cut).join("").trim());
    rest = chars.slice(cut).join("").trim();
  }
  if (rest) lines[lines.length - 1] = `${lines[lines.length - 1]}${rest}`;
  return lines.join("\n");
}

/**
 * 当前时刻应显示的字幕（C6）。
 * 末条 cue 结束后必须返回 null——旧实现回退到末条，播放到片尾仍常驻预览，
 * 与导出的 cue 结束时间不一致；条间空隙同样不显示（不回退邻近条目）。
 */
export function cueAt(cues: SubtitleCue[], ms: number): SubtitleCue | null {
  if (!cues.length) return null;
  for (const cue of cues) {
    if (ms >= cue.start && ms < cue.end) return cue;
  }
  return null;
}

export function buildPreviewCues(
  transcript: Transcript,
  settings?: Partial<
    Pick<
      Settings,
      | "subtitle_max_chars"
      | "subtitle_max_lines"
      | "subtitle_merge_gap_ms"
      | "subtitle_clean_fillers"
      | "subtitle_punct_space"
    >
  > | null,
): PreviewCue[] {
  const maxChars = settings?.subtitle_max_chars ?? 22;
  const maxLines = settings?.subtitle_max_lines ?? 2;
  return buildSubtitleCues(transcript, settings).map((cue) => {
    const chars = contentLen(cue.text);
    const seconds = Math.max(0.001, (cue.end - cue.start) / 1000);
    return {
      ...cue,
      chars,
      cps: chars / seconds,
      lines: wrapSubtitle(cue.text, maxChars, maxLines).split("\n").length,
    };
  });
}
