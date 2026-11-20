import { useMemo } from "react";
import type { Issue, Sentence } from "../types";
import { fmtClock } from "../lib/format";
import { type PreviewCue, wrapSubtitle } from "../lib/subtitle";
import { SubtitleList } from "./SubtitleList";

interface SubtitlePanelProps {
  sentences: Sentence[];
  issues: Issue[];
  cues: PreviewCue[];
  index: number;
  maxChars: number;
  maxLines: number;
  activeId: number | null;
  selectedId: number | null;
  editingId: number | null;
  playing: boolean;
  onSelect: (sentence: Sentence) => void;
  onStartEdit: (sentence: Sentence) => void;
}

/**
 * 右栏的「字幕」工作区：当前句折行预览 + 折行指标 + 可编辑字幕列表。
 * 媒体预览独占左栏后，字幕的浏览、定位与编辑都集中在这里。
 */
export function SubtitlePanel({
  sentences,
  issues,
  cues,
  index,
  maxChars,
  maxLines,
  activeId,
  selectedId,
  editingId,
  playing,
  onSelect,
  onStartEdit,
}: SubtitlePanelProps) {
  const stats = useMemo(() => {
    const total = cues.length;
    return {
      total,
      longest: cues.reduce((max, cue) => Math.max(max, cue.chars), 0),
      avgCps: total ? cues.reduce((sum, cue) => sum + cue.cps, 0) / total : 0,
      maxLines: cues.reduce((max, cue) => Math.max(max, cue.lines), 0),
    };
  }, [cues]);

  if (!sentences.length) {
    return <div className="panel-empty">这段媒体还没有字幕。</div>;
  }

  const safeIndex = Math.min(Math.max(index, 0), cues.length - 1);
  const cue = cues[safeIndex];
  const caption = cue ? wrapSubtitle(cue.text, maxChars, maxLines).split("\n") : [];

  return (
    <div className="panel-subs">
      {cue ? (
        <div className="subs-now">
          <div className="subs-caption">
            {caption.map((line, i) => (
              <span key={i}>{line || " "}</span>
            ))}
          </div>
          <div className="subs-meta">
            <span className="tabular">
              {fmtClock(cue.start)} — {fmtClock(cue.end)}
            </span>
            <span>
              {cue.chars} 字 · {cue.cps.toFixed(1)} 字/秒 · {cue.lines} 行
              {playing ? " · 播放中" : ""}
            </span>
          </div>
        </div>
      ) : null}

      <div className="subs-stats">
        <span>
          <b>{stats.total}</b> 条
        </span>
        <span>
          最长 <b>{stats.longest}</b> 字
        </span>
        <span>
          平均 <b>{stats.avgCps.toFixed(1)}</b> 字/秒
        </span>
        <span>
          最多 <b>{stats.maxLines}</b> 行
        </span>
      </div>

      <SubtitleList
        sentences={sentences}
        issues={issues}
        activeId={activeId}
        selectedId={selectedId}
        editingId={editingId}
        onSelect={onSelect}
        onStartEdit={onStartEdit}
      />

      <div className="subs-hint">单击定位 · 双击编辑 · ↑↓ 切换</div>
    </div>
  );
}
