import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ExportFormat, Issue, Job, Sentence } from "../types";
import { api, errorMessage, isTauriRuntime } from "../lib/engine";
import {
  buildSubtitle,
  clamp,
  fmtBytes,
  fmtClock,
  fmtDate,
  fmtDuration,
  formatLabel,
} from "../lib/format";
import { revealPath, defaultExportFilePath } from "../lib/tauri";
import { buildPreviewCues, contentLen, cueAt, wrapSubtitle } from "../lib/subtitle";
import { useApp } from "../lib/store";
import { Icon } from "./Icon";
import { Player, type PlayerHandle } from "./Player";
import { ProofPanel } from "./ProofPanel";
import { SidePanel } from "./SidePanel";
import { SubtitlePanel } from "./SubtitlePanel";

const EXPORTS: { format: ExportFormat; label: string; desc: string; mime: string }[] = [
  { format: "srt", label: "SRT 字幕", desc: "通用播放器、剪辑软件", mime: "application/x-subrip" },
  { format: "vtt", label: "WebVTT 字幕", desc: "网页播放器、在线课堂", mime: "text/vtt" },
  { format: "ass", label: "ASS 字幕", desc: "带样式的字幕文件", mime: "text/plain" },
  { format: "txt", label: "纯文本", desc: "带时间码的排版稿", mime: "text/plain" },
  { format: "json", label: "JSON 数据", desc: "结构化转录结果", mime: "application/json" },
];

function downloadText(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Windows 文件名兜底：去掉保留字符和末尾的点/空格。 */
function safeFilename(name: string): string {
  const cleaned = name
    .replace(/[<>:"|?*]/g, "_")
    .split("\\").join("_")
    .split("/").join("_")
    .split("")
    .filter((ch: string) => ch.charCodeAt(0) >= 32)
    .join("")
    .replace(/[. ]+$/g, "")
    .trim();
  return cleaned || "字幕";
}

interface TaskEditorProps {
  job: Job;
}

export function TaskEditor({ job }: TaskEditorProps) {
  const {
    transcript,
    issues,
    summary,
    mutating,
    canUndo,
    settings,
    updateSentence,
    replaceIssue,
    fixRepeat,
    dismissIssue,
    applyIssueBatch,
    undo,
    deleteJob,
    toast,
    clearSelection,
    setEditorDirty,
  } = useApp();

  const sentences = transcript?.sentences ?? [];
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(sentences[0]?.id ?? null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  /** 上一次保存的错误文案（C2）：失败时页脚必须显示错误态，而不是「已保存」。 */
  const [saveError, setSaveError] = useState<string | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportFormat, setExportFormat] = useState<ExportFormat>("srt");
  const [panelTab, setPanelTab] = useState<"subs" | "proof">("subs");

  const playerRef = useRef<PlayerHandle | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  /** 草稿的同步镜像：保存期间用户继续输入时，用它与发起保存时的快照比对（C1）。 */
  const draftRef = useRef("");
  const exportRef = useRef<HTMLDivElement | null>(null);
  const playerRefForKeys = useRef<PlayerHandle | null>(null);
  playerRefForKeys.current = playerRef.current;

  const durationMs = transcript?.media.duration_ms || job.duration_ms;

  const previewCues = useMemo(
    () => (transcript ? buildPreviewCues(transcript, settings) : []),
    [transcript, settings],
  );
  const previewIndex = useMemo(() => {
    if (!previewCues.length) return 0;
    const exact = previewCues.findIndex((c) => currentMs >= c.start && currentMs < c.end);
    if (exact >= 0) return exact;
    if (currentMs >= previewCues[previewCues.length - 1].end) return previewCues.length - 1;
    return 0;
  }, [previewCues, currentMs]);

  const sentenceMap = useMemo(
    () => new Map(sentences.map((s) => [s.id, s])),
    [sentences],
  );

  /** 编辑框里的草稿是否还没写回服务端（C2：脏标记 / 导出前置校验）。 */
  const draftDirty = useMemo(() => {
    if (editingId == null) return false;
    const sentence = sentenceMap.get(editingId);
    return sentence ? draft.trim() !== sentence.text : false;
  }, [editingId, sentenceMap, draft]);

  // P1-6：切任务/返回会按 key 重挂载编辑器，脏标记交给 store 做切走确认。
  useEffect(() => {
    setEditorDirty(draftDirty);
    return () => setEditorDirty(false);
  }, [draftDirty, setEditorDirty]);

  useEffect(() => {
    if (!draftDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [draftDirty]);

  // C6：末条 cue 结束后返回 null，不再把末条常驻在播放器上（与导出的结束时间一致）。
  const activeCue = useMemo(() => cueAt(previewCues, currentMs), [previewCues, currentMs]);

  const softSubtitle = useMemo(
    () =>
      activeCue
        ? wrapSubtitle(
            activeCue.text,
            settings.subtitle_max_chars,
            settings.subtitle_max_lines,
          ).split("\n")
        : [],
    [activeCue, settings.subtitle_max_chars, settings.subtitle_max_lines],
  );

  const activeSentence = useMemo(() => {
    if (!sentences.length) return null;
    for (const s of sentences) {
      if (currentMs >= s.start_ms && currentMs < s.end_ms) return s;
    }
    if (currentMs >= sentences[sentences.length - 1].end_ms) {
      return sentences[sentences.length - 1];
    }
    return sentences[0];
  }, [sentences, currentMs]);

  const handleTime = useCallback((ms: number) => setCurrentMs(ms), []);

  useEffect(() => {
    if (selectedId != null && sentenceMap.has(selectedId)) return;
    if (sentences.length) setSelectedId(sentences[0].id);
  }, [sentences, selectedId, sentenceMap]);

  useEffect(() => {
    if (editingId == null) return;
    const el = textareaRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [editingId]);

  useEffect(() => {
    if (!exportOpen) return;
    const onDown = (e: MouseEvent) => {
      if (exportRef.current && !exportRef.current.contains(e.target as Node)) {
        setExportOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [exportOpen]);

  const selectSentence = useCallback(
    (sentence: Sentence) => {
      setSelectedId(sentence.id);
      playerRef.current?.seek(sentence.start_ms + 5);
    },
    [],
  );

  /** 草稿读写必须同时更新 ref，保存回包靠它判断「期间有没有继续输入」。 */
  const updateDraft = useCallback((next: string) => {
    draftRef.current = next;
    setDraft(next);
  }, []);

  const startEdit = useCallback(
    (sentence: Sentence) => {
      if (mutating) return;
      setPanelTab("subs");
      playerRef.current?.pause();
      setSelectedId(sentence.id);
      setEditingId(sentence.id);
      updateDraft(sentence.text);
      setSaveError(null);
    },
    [mutating, updateDraft],
  );

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    updateDraft("");
    setSaveError(null);
  }, [updateDraft]);

  const saveEdit = useCallback(async () => {
    if (editingId == null || saving) return;
    const sentenceId = editingId;
    const current = sentenceMap.get(sentenceId);
    if (!current) return;
    const text = draft.trim();
    if (!text) {
      toast("warn", "字幕内容不能为空");
      textareaRef.current?.focus();
      return;
    }
    if (text === current.text) {
      cancelEdit();
      return;
    }
    // 保存请求在途时输入框仍可编辑：记下发起时的草稿，回包只在这份快照没变时才收起编辑条。
    const submitted = draft;
    setSaving(true);
    setSaveError(null);
    const ok = await updateSentence(sentenceId, text);
    setSaving(false);
    if (!ok) {
      // C2：失败保留编辑条与草稿（不丢用户输入），页脚显示错误态。
      setSaveError("保存失败，修改仍在编辑框中");
      textareaRef.current?.focus();
      return;
    }
    if (draftRef.current === submitted) {
      setEditingId(null);
      updateDraft("");
    } else {
      // 保存期间又输入了新内容：保留草稿并明确告知，别让人以为全都存下了。
      toast("info", "已保存提交时的修改；保存期间的新输入仍未写入");
    }
  }, [
    editingId,
    saving,
    sentenceMap,
    draft,
    toast,
    updateSentence,
    cancelEdit,
    updateDraft,
  ]);

  const moveSelection = useCallback(
    (delta: number) => {
      if (!sentences.length) return;
      const index = sentences.findIndex((s) => s.id === selectedId);
      const next = sentences[clamp(index < 0 ? 0 : index + delta, 0, sentences.length - 1)];
      setSelectedId(next.id);
      playerRef.current?.seek(next.start_ms + 5);
    },
    [sentences, selectedId],
  );

  const locate = useCallback(
    (issue: Issue) => {
      const sentence = sentenceMap.get(issue.sentence_id);
      if (!sentence) return;
      setSelectedId(sentence.id);
      playerRef.current?.seek(sentence.start_ms + 5);
      if (issue.kind === "lowconf") startEdit(sentence);
    },
    [sentenceMap, startEdit],
  );

  const doExport = useCallback(
    async (format: ExportFormat) => {
      if (!transcript || exporting) return;
      // C2：导出前校验没有在途写入、没有未保存草稿，避免把旧稿导出去还提示成功。
      if (saving || mutating || draftDirty) {
        toast("warn", "还有未保存的修改，请先保存或取消编辑后再导出。");
        return;
      }
      setExportOpen(false);
      setExporting(true);
      const meta = EXPORTS.find((x) => x.format === format) ?? EXPORTS[0];
      const base = safeFilename(job.name.replace(/\.[^.]+$/, ""));
      const filename = `${base}.${format}`;
      try {
        if (isTauriRuntime()) {
          // 不调用 Windows 原生“另存为”：部分机器会把可写目录误判为无权限。
          // 直接写入设置目录/系统视频目录，失败再由引擎落到 data/exports。
          const target = await defaultExportFilePath(filename, settings.export_dir);
          if (target) {
            try {
              const res = await api.export(job.id, format, target);
              if (res.fallback) {
                toast("warn", `目标目录不可写，已导出到默认目录：${res.path}`);
              } else {
                toast("success", `字幕已导出：${res.path}`);
              }
              return;
            } catch (writeErr) {
              console.warn("[export] target dir failed, fallback to engine", writeErr);
            }
          }
          const res = await api.export(job.id, format);
          toast("warn", `目标目录不可写，已导出到默认目录：${res.path}`);
        } else {
          try {
            await api.export(job.id, format);
          } catch {
            // 引擎导出失败不影响浏览器下载副本。
          }
          const text = buildSubtitle(format, transcript, settings);
          downloadText(filename, text, meta.mime);
          toast("success", `已下载 ${filename}`);
        }
      } catch (err) {
        toast("error", `导出失败：${errorMessage(err)}`);
      } finally {
        setExporting(false);
      }
    },
    [transcript, exporting, job, toast, settings, saving, mutating, draftDirty],
  );

  // 键盘：空格播放、上下选句、回车编辑、Ctrl+Z 撤销、Ctrl+Enter 导出。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        !!target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (e.key === "Escape") {
        if (editingId != null) {
          e.preventDefault();
          cancelEdit();
        } else if (exportOpen) {
          setExportOpen(false);
        }
        return;
      }
      if (typing) return;
      const mod = e.ctrlKey || e.metaKey;
      if (e.code === "Space") {
        if (target?.closest("button, a")) return;
        e.preventDefault();
        playerRefForKeys.current?.toggle();
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        moveSelection(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      // C5：Ctrl/Meta+Enter 是导出，必须排在「Enter 打开编辑」之前，
      // 否则字幕列表聚焦时组合键会先命中 Enter 分支（export_requests=0）。
      if (mod && e.key === "Enter") {
        e.preventDefault();
        void doExport(exportFormat);
        return;
      }
      // SPEC §4 也承诺 Ctrl+E 导出，这里一起支持，避免文档与实现漂移。
      if (mod && e.key.toLowerCase() === "e") {
        e.preventDefault();
        void doExport(exportFormat);
        return;
      }
      if (e.key === "Enter") {
        // 兜底：任何组合键都不再落入「打开编辑条」。
        if (mod || e.altKey) return;
        if (target?.closest("button, a")) return;
        e.preventDefault();
        const sentence = selectedId != null ? sentenceMap.get(selectedId) : undefined;
        if (sentence) startEdit(sentence);
        return;
      }
      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (canUndo && !mutating) void undo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    editingId,
    exportOpen,
    cancelEdit,
    moveSelection,
    selectedId,
    sentenceMap,
    startEdit,
    canUndo,
    mutating,
    undo,
    doExport,
    exportFormat,
  ]);

  const editingSentence = editingId != null ? sentenceMap.get(editingId) : undefined;
  const displayName = job.name.replace(/\.[^.]+$/, "") || job.name;
  const chars = transcript?.stats.chars ?? sentences.reduce((n, s) => n + contentLen(s.text), 0);
  const activeId = activeSentence?.id ?? null;
  const draftChars = contentLen(draft);
  const overLimit = editingSentence ? draftChars > settings.subtitle_max_chars : false;

  /** 页脚保存状态（C2）：在途写 / 上次失败 / 未保存草稿 / 干净，四种态互斥。 */
  const saveState: { tone: "saving" | "error" | "warn" | "ok"; text: string } =
    saving || mutating
      ? { tone: "saving", text: "正在保存修改…" }
      : saveError
        ? { tone: "error", text: saveError }
        : draftDirty
          ? { tone: "warn", text: "有未保存的修改" }
          : { tone: "ok", text: "所有修改已保存到本地" };

  return (
    <div className="editor">
      <header className="editor-head">
        <button
          type="button"
          className="icon-btn back-btn"
          onClick={clearSelection}
          aria-label="返回工作台"
          data-tip="返回工作台"
        >
          <Icon name="chevronLeft" size={18} />
        </button>
        <div className="editor-title">
          <div className="title-line">
            <h1 title={job.name}>{displayName}</h1>
            <span className="status-pill tone-ok">
              <span className="dot" /> 已完成
            </span>
          </div>
          <div className="editor-meta">
            <span>
              <Icon name="audio" size={13} /> 中文
            </span>
            <span>{fmtDuration(durationMs)}</span>
            <span>{fmtBytes(job.size_bytes)}</span>
            <span>
              {sentences.length} 句 · {chars} 字
            </span>
            <span className="hide-sm">{fmtDate(job.created_at)}</span>
            {job.rtf ? <span className="hide-sm">{`${(1 / job.rtf).toFixed(1)}× 实时`}</span> : null}
          </div>
        </div>
        <div className="editor-head-actions">
          {summary.total > 0 ? (
            <span className="pending-note">
              <span className="dot amber" />
              {summary.total} 条建议待处理
            </span>
          ) : (
            <span className="pending-note is-clear">
              <Icon name="checkCircle" size={14} /> 校对完成
            </span>
          )}
          {isTauriRuntime() ? (
            <button
              type="button"
              className="icon-btn"
              data-tip="打开文件位置"
              aria-label="打开文件位置"
              onClick={async () => {
                const ok = await revealPath(job.path);
                if (!ok) toast("warn", "当前环境无法打开文件位置");
              }}
            >
              <Icon name="folder" size={17} />
            </button>
          ) : null}
          <button
            type="button"
            className="icon-btn danger-hover"
            data-tip="删除任务"
            aria-label="删除任务"
            onClick={() => void deleteJob(job)}
          >
            <Icon name="trash" size={16} />
          </button>
        </div>
      </header>

      <div className="editor-body">
        <div className="editor-main">
          <section className="player-card">
            <header className="player-head">
              <div className="player-head-left">
                <Icon name="film" size={15} />
                <span>媒体预览</span>
                <span className="head-sep" />
                <span className="muted tabular">
                  {fmtClock(currentMs, true)} / {fmtClock(durationMs, true)}
                </span>
              </div>
            </header>
            <Player
              ref={playerRef}
              job={job}
              durationMs={durationMs}
              onTime={handleTime}
              onPlayingChange={setPlaying}
              subtitle={softSubtitle}
            />
          </section>

        </div>

        <SidePanel
          tab={panelTab}
          onTab={setPanelTab}
          pendingCount={summary.total}
          subCount={sentences.length}
        >
          <div className="panel-pane" hidden={panelTab !== "subs"}>
            <SubtitlePanel
              sentences={sentences}
              issues={issues}
              cues={previewCues}
              index={previewIndex}
              maxChars={settings.subtitle_max_chars}
              maxLines={settings.subtitle_max_lines}
              activeId={activeId}
              selectedId={selectedId}
              editingId={editingId}
              playing={playing}
              onSelect={selectSentence}
              onStartEdit={startEdit}
            />

            {editingSentence ? (
              <div className="edit-bar panel-edit">
                <div className="edit-bar-head">
                  <span>
                    <Icon name="edit" size={13} /> 编辑第{" "}
                    {sentences.findIndex((s) => s.id === editingSentence.id) + 1} 句
                  </span>
                  <span className={`char-count tabular ${overLimit ? "is-over" : ""}`}>
                    {draftChars} / 建议 {settings.subtitle_max_chars} 字
                  </span>
                </div>
                <textarea
                  ref={textareaRef}
                  className="edit-input"
                  rows={2}
                  value={draft}
                  spellCheck={false}
                  onChange={(e) => updateDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      e.preventDefault();
                      cancelEdit();
                    } else if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void saveEdit();
                    }
                  }}
                />
                <div className="edit-bar-actions">
                  <span className="edit-hint">Enter 保存 · Esc 取消</span>
                  <button type="button" className="btn ghost sm" onClick={cancelEdit}>
                    取消
                  </button>
                  <button
                    type="button"
                    className="btn primary sm"
                    disabled={saving || !draft.trim()}
                    onClick={() => void saveEdit()}
                  >
                    {saving ? "保存中…" : "保存修改"}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
          <div className="panel-pane" hidden={panelTab !== "proof"}>
            <ProofPanel
              issues={issues}
              sentences={sentences}
              maxChars={settings.subtitle_max_chars}
              mutating={mutating}
              canUndo={canUndo}
              onLocate={locate}
              onReplace={(issue, scope) => void replaceIssue(issue, scope)}
              onRepeat={(issue) => void fixRepeat(issue)}
              onDismiss={(issue) => void dismissIssue(issue)}
              onBatch={(batch) => void applyIssueBatch(batch)}
            />
          </div>
        </SidePanel>
      </div>

      <footer className="editor-foot">
        <div className="foot-left">
          <button
            type="button"
            className="btn ghost sm undo-btn"
            disabled={!canUndo || mutating}
            onClick={() => void undo()}
            title="撤销上一步修改（Ctrl+Z）"
          >
            <Icon name="undo" size={15} />
            撤销
            <kbd>Ctrl Z</kbd>
          </button>
          <span
            className={`save-state${
              saveState.tone === "error"
                ? " is-error"
                : saveState.tone === "warn"
                  ? " is-warn"
                  : ""
            }`}
            role="status"
            aria-live="polite"
          >
            <span
              className={`dot ${
                saveState.tone === "saving"
                  ? "accent-dot pulse"
                  : saveState.tone === "error"
                    ? "err"
                    : saveState.tone === "warn"
                      ? "warn"
                      : "ok"
              }`}
            />
            {saveState.text}
          </span>
        </div>
        <div className="foot-right">
          <span className="export-hint hide-md">
            导出字幕 <kbd>Ctrl ↵</kbd>
          </span>
          <div className="export-group" ref={exportRef}>
            <button
              type="button"
              className="btn primary export-main"
              disabled={exporting || !sentences.length}
              onClick={() => void doExport(exportFormat)}
            >
              <Icon name="download" size={16} />
              {exporting ? "导出中…" : `导出 ${formatLabel(exportFormat)}`}
            </button>
            <button
              type="button"
              className="btn primary export-caret"
              aria-label="选择导出格式"
              aria-expanded={exportOpen}
              disabled={exporting || !sentences.length}
              onClick={() => setExportOpen((v) => !v)}
            >
              <Icon name={exportOpen ? "chevronDown" : "chevronUp"} size={14} />
            </button>
            {exportOpen ? (
              <div className="export-menu" role="menu">
                <div className="export-menu-head">选择导出格式</div>
                {EXPORTS.map((item) => (
                  <button
                    key={item.format}
                    type="button"
                    role="menuitem"
                    className={`export-item ${exportFormat === item.format ? "is-on" : ""}`}
                    onClick={() => {
                      setExportFormat(item.format);
                      void doExport(item.format);
                    }}
                  >
                    <span className="export-ext">{item.format.toUpperCase()}</span>
                    <span className="export-label">
                      <strong>{item.label}</strong>
                      <small>{item.desc}</small>
                    </span>
                    {exportFormat === item.format ? <Icon name="check" size={14} /> : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </footer>

    </div>
  );
}

/** Tauri：先弹保存框，再让引擎写入指定路径。取消时返回 null。 */

