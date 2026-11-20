import { useMemo, useState } from "react";
import type { Issue, IssueKind, Sentence } from "../types";
import { contentLen } from "../lib/subtitle";
import { Icon } from "./Icon";

interface ProofPanelProps {
  issues: Issue[];
  sentences: Sentence[];
  /** 单条字幕的建议字数上限，用于空态质量检查。 */
  maxChars: number;
  mutating: boolean;
  canUndo: boolean;
  onLocate: (issue: Issue) => void;
  onReplace: (issue: Issue, scope: "all" | "selected") => void;
  onRepeat: (issue: Issue) => void;
  onDismiss: (issue: Issue) => void;
  onBatch: (issues: Issue[]) => void;
}

type Filter = "all" | IssueKind;

const KIND_META: Record<IssueKind, { label: string; icon: "alert" | "repeat" | "gauge"; hint: string }> = {
  suspicious: { label: "疑似同音", icon: "alert", hint: "结合上下文判断是否为同音错字" },
  repeat: { label: "重复用词", icon: "repeat", hint: "识别引擎偶尔会重复输出相邻字词" },
  lowconf: { label: "低置信", icon: "gauge", hint: "置信度偏低的句子建议人工复核" },
};

function ContextLine({ issue, sentence }: { issue: Issue; sentence?: Sentence }) {
  if (!sentence) return null;
  const before = sentence.text.slice(0, issue.start);
  const hit = sentence.text.slice(issue.start, issue.end);
  const after = sentence.text.slice(issue.end);
  return (
    <p className="issue-context">
      {before.length > 18 ? `…${before.slice(-18)}` : before}
      <mark>{hit || issue.text}</mark>
      {after.length > 26 ? `${after.slice(0, 26)}…` : after}
    </p>
  );
}

export function ProofPanel({
  issues,
  sentences,
  maxChars,
  mutating,
  canUndo,
  onLocate,
  onReplace,
  onRepeat,
  onDismiss,
  onBatch,
}: ProofPanelProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());

  const pending = useMemo(() => issues.filter((i) => !i.resolved), [issues]);
  const counts = useMemo(
    () => ({
      suspicious: pending.filter((i) => i.kind === "suspicious").length,
      repeat: pending.filter((i) => i.kind === "repeat").length,
      lowconf: pending.filter((i) => i.kind === "lowconf").length,
    }),
    [pending],
  );
  const sentenceMap = useMemo(() => new Map(sentences.map((s) => [s.id, s])), [sentences]);
  const cueStats = useMemo(() => {
    const lengths = sentences.map((s) => contentLen(s.text));
    const total = lengths.reduce((sum, n) => sum + n, 0);
    return {
      avg: lengths.length ? total / lengths.length : 0,
      max: lengths.reduce((m, n) => Math.max(m, n), 0),
      over: lengths.filter((n) => n > maxChars).length,
    };
  }, [sentences, maxChars]);
  const processed = issues.length - pending.length;
  const progress = issues.length ? (processed / issues.length) * 100 : 100;

  const visible = filter === "all" ? pending : pending.filter((i) => i.kind === filter);
  const selectableVisible = visible.filter((issue) => issue.kind !== "lowconf");
  const selectedBatch = pending.filter(
    (issue) => selected.has(issue.id) && issue.kind !== "lowconf",
  );
  const allSelected =
    selectableVisible.length > 0 &&
    selectableVisible.every((issue) => selected.has(issue.id));

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const issue of selectableVisible) {
        if (allSelected) next.delete(issue.id);
        else next.add(issue.id);
      }
      return next;
    });
  };

  return (
    <>
      {pending.length ? (
        <>
      <div className="proof-bars" aria-hidden="true">
        {counts.suspicious ? (
          <span className="bar bar-suspicious" style={{ flex: counts.suspicious }} />
        ) : null}
        {counts.repeat ? <span className="bar bar-repeat" style={{ flex: counts.repeat }} /> : null}
        {counts.lowconf ? <span className="bar bar-lowconf" style={{ flex: counts.lowconf }} /> : null}
      </div>

      <div className="proof-filters" role="tablist" aria-label="建议类型">
        {(
          [
            ["all", "全部", pending.length],
            ["suspicious", "疑似", counts.suspicious],
            ["repeat", "重复", counts.repeat],
            ["lowconf", "低置信", counts.lowconf],
          ] as const
        ).map(([key, label, count]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={filter === key}
            className={`filter-chip ${filter === key ? "is-on" : ""}`}
            onClick={() => setFilter(key)}
          >
            {label}
            <span>{count}</span>
          </button>
        ))}
      </div>

      <div className="proof-batch" aria-label="批量处理建议">
        <label className="batch-select">
          <input
            type="checkbox"
            checked={allSelected}
            disabled={!selectableVisible.length || mutating}
            onChange={toggleAll}
          />
          <span>全选当前</span>
        </label>
        <span className="batch-count">已选 {selectedBatch.length}</span>
        <button
          type="button"
          className="btn tiny primary"
          disabled={!selectedBatch.length || mutating}
          onClick={() => onBatch(selectedBatch)}
          title="批量替换疑似同音词、去除重复；低置信建议保留人工复核"
        >
          批量处理
        </button>
        {selectedBatch.length ? (
          <button
            type="button"
            className="batch-clear"
            onClick={() => setSelected(new Set())}
            title="清空选择"
          >
            <Icon name="close" size={12} />
          </button>
        ) : null}
      </div>
        </>
      ) : null}

      <div className="issue-list">
        {visible.map((issue) => {
          const meta = KIND_META[issue.kind];
          const sentence = sentenceMap.get(issue.sentence_id);
          return (
            <article key={issue.id} className={`issue-card kind-${issue.kind}`}>
              <header>
                {issue.kind === "lowconf" ? (
                  <span className="issue-check is-locked" title="低置信建议需人工复核">
                    <Icon name="lock" size={11} />
                  </span>
                ) : (
                  <label className="issue-check" title="选择此建议">
                    <input
                      type="checkbox"
                      checked={selected.has(issue.id)}
                      disabled={mutating}
                      onChange={() => toggleOne(issue.id)}
                      aria-label={`选择建议：${issue.text || issue.kind}`}
                    />
                  </label>
                )}
                <span className={`kind-badge tone-${issue.kind}`}>
                  <Icon name={meta.icon} size={13} />
                  {meta.label}
                </span>
                <span className="issue-stat">
                  {issue.kind === "lowconf"
                    ? `置信度 ${Math.round(issue.score * (issue.score <= 1 ? 100 : 1))}%`
                    : issue.kind === "repeat"
                      ? "连续重复"
                      : issue.count > 1
                        ? `全文 ${issue.count} 处`
                        : "出现 1 处"}
                </span>
                <button
                  type="button"
                  className="icon-btn subtle"
                  data-tip="忽略此建议"
                  aria-label="忽略此建议"
                  disabled={mutating}
                  onClick={() => onDismiss(issue)}
                >
                  <Icon name="eyeOff" size={14} />
                </button>
              </header>

              <div className="issue-swap">
                <span className="issue-from">{issue.text || "（空）"}</span>
                {issue.kind !== "lowconf" && issue.suggestion ? (
                  <>
                    <Icon name="arrowRight" size={14} className="issue-arrow" />
                    <span className="issue-to">{issue.suggestion}</span>
                  </>
                ) : (
                  <span className="issue-low">建议人工复核</span>
                )}
              </div>

              <ContextLine issue={issue} sentence={sentence} />

              <footer>
                <button
                  type="button"
                  className="text-btn"
                  onClick={() => onLocate(issue)}
                  title="在字幕中定位"
                >
                  <Icon name="search" size={13} /> 定位
                </button>
                <div className="issue-actions">
                  {issue.kind === "suspicious" ? (
                    <>
                      <button
                        type="button"
                        className="btn tiny primary"
                        disabled={mutating}
                        onClick={() => onReplace(issue, "all")}
                        title={`将全文中所有「${issue.text}」替换为「${issue.suggestion}」`}
                      >
                        全部替换{issue.count > 1 ? ` ×${issue.count}` : ""}
                      </button>
                      <button
                        type="button"
                        className="btn tiny ghost"
                        disabled={mutating}
                        onClick={() => onReplace(issue, "selected")}
                      >
                        仅此句
                      </button>
                    </>
                  ) : null}
                  {issue.kind === "repeat" ? (
                    <button
                      type="button"
                      className="btn tiny primary"
                      disabled={mutating}
                      onClick={() => onRepeat(issue)}
                    >
                      去除重复
                    </button>
                  ) : null}
                  {issue.kind === "lowconf" ? (
                    <button
                      type="button"
                      className="btn tiny primary"
                      disabled={mutating}
                      onClick={() => onLocate(issue)}
                    >
                      定位并编辑
                    </button>
                  ) : null}
                </div>
              </footer>
            </article>
          );
        })}

        {!visible.length ? (
          pending.length ? (
            <div className="proof-empty">
              <span className="proof-empty-icon">
                <Icon name="search" size={22} />
              </span>
              <strong>当前筛选下没有建议</strong>
              <p>切换到其他分类看看。</p>
            </div>
          ) : (
            <div className="proof-done">
              <div className="proof-done-hero">
                <span className="proof-done-icon">
                  <Icon name="checkCircle" size={22} />
                </span>
                <div>
                  <strong>校对完成</strong>
                  <p>没有待处理建议，可以直接导出字幕。</p>
                </div>
              </div>

              <div className="proof-done-stats">
                <div>
                  <strong className="tabular">{sentences.length}</strong>
                  <span>条字幕</span>
                </div>
                <div>
                  <strong className={`tabular ${cueStats.over ? "is-warn" : ""}`}>
                    {cueStats.over}
                  </strong>
                  <span>条超长</span>
                </div>
                <div>
                  <strong className="tabular">{cueStats.avg.toFixed(1)}</strong>
                  <span>平均字数</span>
                </div>
                <div>
                  <strong className="tabular">{cueStats.max}</strong>
                  <span>最长字数</span>
                </div>
              </div>

              <ul className="proof-checks">
                <li className={cueStats.over ? "is-warn" : "is-ok"}>
                  <Icon name={cueStats.over ? "alert" : "checkCircle"} size={13} />
                  {cueStats.over
                    ? `${cueStats.over} 条超过 ${maxChars} 字，导出前建议拆分`
                    : `所有字幕都在 ${maxChars} 字以内`}
                </li>
                <li className="is-ok">
                  <Icon name="checkCircle" size={13} /> 首尾各抽查 1–2 句，确认没有整段错字
                </li>
                <li>
                  <Icon name="keyboard" size={13} /> 双击任意行可编辑，Ctrl+Z 可撤销
                </li>
              </ul>

              <p className="proof-done-note">
                重复口吃与口语语气词已在识别阶段自动清理，仍可在设置中关闭。
              </p>
            </div>
          )
        ) : null}
      </div>

      {pending.length ? (
      <footer className="proof-foot">
        <div className="proof-progress">
          <div className="proof-progress-text">
            <span>已处理 {processed} / {issues.length}</span>
            <span className="tabular">{Math.round(progress)}%</span>
          </div>
          <div className="track">
            <progress value={progress} max={100} aria-label="建议处理进度" />
          </div>
        </div>
        <p className="proof-hint">
          <Icon name="undo" size={12} />
          {canUndo ? "可随时 Ctrl+Z 撤销上一步" : "修改会自动保存到本地"}
        </p>
      </footer>
      ) : null}
    </>
  );
}
