import React, { useEffect, useMemo, useRef, useState } from "react";
import type { Issue, IssueKind, Sentence } from "../types";
import { fmtClock } from "../lib/format";
import { Icon } from "./Icon";

const ROW = 64;
const OVERSCAN = 5;

interface Segment {
  text: string;
  kind?: IssueKind;
  title?: string;
}

function segmentsOf(text: string, issues: Issue[]): Segment[] {
  const ranges = issues
    .filter(
      (i) =>
        !i.resolved &&
        i.kind !== "lowconf" &&
        i.end > i.start &&
        i.start >= 0 &&
        i.end <= text.length,
    )
    .sort((a, b) => a.start - b.start);
  const segs: Segment[] = [];
  let cursor = 0;
  for (const issue of ranges) {
    if (issue.start < cursor) continue;
    if (issue.start > cursor) segs.push({ text: text.slice(cursor, issue.start) });
    segs.push({
      text: text.slice(issue.start, issue.end),
      kind: issue.kind,
      title:
        issue.kind === "repeat"
          ? `疑似重复「${issue.text}」，可去除`
          : `疑似「${issue.text}」→「${issue.suggestion}」`,
    });
    cursor = issue.end;
  }
  if (cursor < text.length) segs.push({ text: text.slice(cursor) });
  return segs.length ? segs : [{ text }];
}

interface SubtitleListProps {
  sentences: Sentence[];
  issues: Issue[];
  activeId: number | null;
  selectedId: number | null;
  editingId: number | null;
  onSelect: (sentence: Sentence) => void;
  onStartEdit: (sentence: Sentence) => void;
}

export function SubtitleList({
  sentences,
  issues,
  activeId,
  selectedId,
  editingId,
  onSelect,
  onStartEdit,
}: SubtitleListProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const followRef = useRef(true);
  const followTimer = useRef(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(480);

  const issuesBySentence = useMemo(() => {
    const map = new Map<number, Issue[]>();
    for (const issue of issues) {
      if (issue.resolved) continue;
      const list = map.get(issue.sentence_id);
      if (list) list.push(issue);
      else map.set(issue.sentence_id, [issue]);
    }
    return map;
  }, [issues]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewport(el.clientHeight));
    ro.observe(el);
    setViewport(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  const activeIndex = useMemo(
    () => sentences.findIndex((s) => s.id === activeId),
    [sentences, activeId],
  );

  // 播放时让当前句保持在视野内；用户主动滚动后 4 秒内不抢夺滚动。
  useEffect(() => {
    if (activeIndex < 0 || !followRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    const top = activeIndex * ROW;
    const viewTop = el.scrollTop;
    const viewBottom = viewTop + el.clientHeight;
    if (top < viewTop + ROW * 0.5 || top + ROW > viewBottom - ROW * 0.5) {
      el.scrollTo({
        top: Math.max(0, top - el.clientHeight / 2 + ROW / 2),
        behavior: "smooth",
      });
    }
  }, [activeIndex]);

  useEffect(() => {
    if (editingId == null) return;
    const idx = sentences.findIndex((s) => s.id === editingId);
    const el = scrollRef.current;
    if (idx < 0 || !el) return;
    const top = idx * ROW;
    if (top < el.scrollTop || top + ROW > el.scrollTop + el.clientHeight) {
      el.scrollTo({ top: Math.max(0, top - el.clientHeight / 2 + ROW / 2), behavior: "smooth" });
    }
  }, [editingId, sentences]);

  const start = Math.max(0, Math.floor(scrollTop / ROW) - OVERSCAN);
  const end = Math.min(sentences.length, Math.ceil((scrollTop + viewport) / ROW) + OVERSCAN);
  const visible = sentences.slice(start, end);

  const manualScroll = () => {
    followRef.current = false;
    window.clearTimeout(followTimer.current);
    followTimer.current = window.setTimeout(() => {
      followRef.current = true;
    }, 4000);
  };

  useEffect(() => () => window.clearTimeout(followTimer.current), []);

  return (
    <div
      ref={scrollRef}
      className="sub-scroll"
      role="listbox"
      aria-label="字幕句子"
      tabIndex={-1}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      onWheel={manualScroll}
      onTouchMove={manualScroll}
    >
      <div className="sub-inner" style={{ height: sentences.length * ROW }}>
        {visible.map((sentence, i) => {
          const index = start + i;
          const active = sentence.id === activeId;
          const selected = sentence.id === selectedId;
          const editing = sentence.id === editingId;
          const list = issuesBySentence.get(sentence.id) ?? [];
          const pending = list.filter((x) => x.kind !== "lowconf");
          const low = list.find((x) => x.kind === "lowconf");
          const segments = segmentsOf(sentence.text, list);
          const conf = Math.round(sentence.conf * 100);
          return (
            <div
              key={sentence.id}
              role="option"
              aria-selected={selected}
              className={[
                "sub-row",
                active ? "is-active" : "",
                selected ? "is-selected" : "",
                editing ? "is-editing" : "",
                sentence.edited ? "is-edited" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{ top: index * ROW, height: ROW }}
              onClick={() => onSelect(sentence)}
              onDoubleClick={() => onStartEdit(sentence)}
              title="单击定位 · 双击编辑"
            >
              <div className="sub-time tabular">
                <span>{fmtClock(sentence.start_ms, false)}</span>
                <span className="sub-time-end">{fmtClock(sentence.end_ms, false)}</span>
              </div>
              <div className="sub-copy">
                <p className="sub-text">
                  {segments.map((seg, si) =>
                    seg.kind ? (
                      <span key={si} className={`mark mark-${seg.kind}`} title={seg.title}>
                        {seg.text}
                      </span>
                    ) : (
                      <React.Fragment key={si}>{seg.text}</React.Fragment>
                    ),
                  )}
                </p>
                <div className="sub-tags">
                  {sentence.edited ? <span className="tag-edited">已校</span> : null}
                  {low ? (
                    <span className={`tag-conf tone-${conf < 60 ? "err" : "warn"}`} title={low.reason || "低置信度"}>
                      <Icon name="gauge" size={11} /> {conf}%
                    </span>
                  ) : null}
                  {pending.length ? (
                    <span className="tag-issue" title={pending.map((x) => x.reason).join("\n")}>
                      <span className="dot" /> {pending.length} 条建议
                    </span>
                  ) : null}
                </div>
              </div>
              <button
                type="button"
                className="row-edit"
                aria-label="编辑此句"
                data-tip="编辑"
                onClick={(e) => {
                  e.stopPropagation();
                  onStartEdit(sentence);
                }}
              >
                <Icon name="edit" size={14} />
              </button>
            </div>
          );
        })}
      </div>
      {!sentences.length ? <div className="sub-empty">这段媒体还没有字幕。</div> : null}
    </div>
  );
}
