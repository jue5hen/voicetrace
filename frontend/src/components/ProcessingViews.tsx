import { useEffect, useState } from "react";
import type { Job, JobStage } from "../types";
import { fmtBytes, fmtDuration, jobEta, stageLabel } from "../lib/format";
import { useApp } from "../lib/store";
import { Icon } from "./Icon";

const STAGES: { key: JobStage; label: string }[] = [
  { key: "load", label: "加载模型" },
  { key: "probe", label: "读取媒体" },
  { key: "extract", label: "提取音轨" },
  { key: "vad", label: "人声检测" },
  { key: "asr", label: "语音识别" },
  { key: "punc", label: "标点恢复" },
  { key: "finalize", label: "生成字幕" },
];

const RING = 2 * Math.PI * 66;

function useTick(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

export function ProcessingView({ job }: { job: Job }) {
  const { jobs, queue, toggleQueue, cancelJob, confirm, selectJob } = useApp();
  const now = useTick(job.status === "processing");
  const queued = job.status === "queued";
  const pct = Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
  const stageIndex = Math.max(0, STAGES.findIndex((s) => s.key === job.stage));
  const started = job.started_at ? new Date(job.started_at).getTime() : 0;
  const elapsed = job.status === "processing" && started ? now - started : 0;
  const ahead = queued ? Math.max(1, queue.pending.indexOf(job.id) + 1) : 0;
  const activeJob = jobs.find((j) => j.status === "processing" && j.id !== job.id);
  const displayName = job.name.replace(/\.[^.]+$/, "") || job.name;

  const onCancel = async () => {
    const ok = await confirm({
      title: queued ? "取消排队" : "停止转录",
      description: `确定要取消「${job.name}」吗？${
        queued ? "该任务将从队列中移除。" : "已经处理的部分不会保留。"
      }`,
      confirmText: queued ? "取消排队" : "停止转录",
      tone: "danger",
    });
    if (ok) await cancelJob(job);
  };

  return (
    <div className="state-page">
      <div className="state-card proc-card">
        <div className="proc-visual">
          <svg className="proc-ring" viewBox="0 0 148 148" aria-hidden="true">
            <circle className="ring-bg" cx="74" cy="74" r="66" />
            <circle
              className={`ring-fg ${queued ? "is-queued" : ""}`}
              cx="74"
              cy="74"
              r="66"
              strokeDasharray={RING}
              strokeDashoffset={queued ? RING * 0.82 : RING * (1 - Math.max(0.02, pct / 100))}
            />
          </svg>
          <div className="proc-ring-label">
            {queued ? (
              <>
                <Icon name="clock" size={26} />
                <strong>队列中</strong>
                <span>第 {ahead} 位</span>
              </>
            ) : (
              <>
                <strong className="tabular">{pct}%</strong>
                <span>{stageLabel(job.stage)}</span>
              </>
            )}
          </div>
          <div className="proc-pulse" aria-hidden="true">
            <i />
            <i />
          </div>
        </div>

        <div className="proc-body">
          <div className="proc-eyebrow">
            <span className={`live-dot ${queued ? "is-wait" : ""}`} />
            {queued ? "等待开始 · 本地队列串行处理" : "正在转写 · 全部计算在本机完成"}
          </div>
          <h1 title={job.name}>{displayName}</h1>
          <p className="proc-sub">
            {queued
              ? queue.paused
                ? "队列已暂停，继续后将自动开始。"
                : `前方还有 ${Math.max(0, ahead - 1)} 个任务，完成后会自动开始。`
              : `${stageLabel(job.stage)}进行中，可离开此页，任务会在后台继续。`}
          </p>

          <div className="proc-track-wrap">
            <div className="proc-track">
              <div
                className={`proc-fill ${queued ? "is-queued" : ""}`}
                style={{ width: queued ? "8%" : `${Math.max(2, pct)}%` }}
              />
            </div>
            <div className="proc-track-meta">
              <span>{queued ? "尚未开始" : stageLabel(job.stage)}</span>
              <span className="tabular">{queued ? "—" : `${pct}%`}</span>
            </div>
          </div>

          <div className="proc-facts">
            <div>
              <span>媒体时长</span>
              <strong className="tabular">{fmtDuration(job.duration_ms)}</strong>
            </div>
            <div>
              <span>文件大小</span>
              <strong className="tabular">{fmtBytes(job.size_bytes)}</strong>
            </div>
            <div>
              <span>{queued ? "队列位置" : "已用时"}</span>
              <strong className="tabular">
                {queued ? `第 ${ahead} 位` : fmtDuration(elapsed)}
              </strong>
            </div>
            <div>
              <span>预计剩余</span>
              <strong className="tabular">{queued ? "等待中" : jobEta(job)}</strong>
            </div>
          </div>

          <ol className="stage-rail">
            {STAGES.map((s, i) => {
              const done = !queued && i < stageIndex;
              const current = !queued && i === stageIndex;
              return (
                <li
                  key={s.key}
                  className={`${done ? "is-done" : ""} ${current ? "is-current" : ""}`}
                >
                  <span className="stage-dot">
                    {done ? <Icon name="check" size={10} /> : <i />}
                  </span>
                  <span className="stage-label">{s.label}</span>
                </li>
              );
            })}
          </ol>

          <div className="proc-actions">
            <button type="button" className="btn ghost" onClick={() => void onCancel()}>
              <Icon name="close" size={15} />
              {queued ? "取消排队" : "停止转录"}
            </button>
            <button type="button" className="btn subtle" onClick={() => void toggleQueue()}>
              <Icon name={queue.paused ? "play" : "pause"} size={15} />
              {queue.paused ? "继续队列" : "暂停队列"}
            </button>
            {activeJob && queued ? (
              <button
                type="button"
                className="text-btn"
                onClick={() => selectJob(activeJob.id)}
              >
                查看正在处理的任务
                <Icon name="arrowRight" size={13} />
              </button>
            ) : null}
          </div>
        </div>
      </div>

      <div className="state-footnote">
        <Icon name="lock" size={13} />
        离线推理：媒体与文字始终留在你的电脑上，不会上传到任何服务器。
      </div>
    </div>
  );
}

export function JobErrorView({ job }: { job: Job }) {
  const { retryJob, deleteJob, clearSelection } = useApp();
  const canceled = job.status === "canceled";
  return (
    <div className="state-page">
      <div className="state-card error-card">
        <div className={`error-icon ${canceled ? "is-muted" : ""}`}>
          <Icon name={canceled ? "alertCircle" : "alert"} size={26} />
        </div>
        <h1>{canceled ? "任务已取消" : "转录没有完成"}</h1>
        <p className="error-lead">
          {canceled
            ? `「${job.name}」已被取消，可以重新加入队列。`
            : `「${job.name}」在处理过程中遇到了问题。`}
        </p>
        {!canceled && job.error ? (
          <pre className="error-box">{job.error}</pre>
        ) : null}
        <div className="error-meta">
          <span>时长 {fmtDuration(job.duration_ms)}</span>
          <span>大小 {fmtBytes(job.size_bytes)}</span>
          <span>阶段 {stageLabel(job.stage)}</span>
        </div>
        <div className="error-actions">
          <button type="button" className="btn primary" onClick={() => void retryJob(job)}>
            <Icon name="retry" size={16} /> 重新转录
          </button>
          <button type="button" className="btn ghost" onClick={() => void deleteJob(job)}>
            <Icon name="trash" size={15} /> 删除任务
          </button>
          <button type="button" className="text-btn" onClick={clearSelection}>
            返回工作台
          </button>
        </div>
        <p className="error-hint">
          常见原因：文件被占用、格式不受支持、显存不足。重试前可先关闭占用该文件的程序。
        </p>
      </div>
    </div>
  );
}

export function TranscriptSkeleton({ job }: { job: Job }) {
  return (
    <div className="skeleton-page" aria-busy="true" aria-label="正在加载转录">
      <div className="sk-head">
        <div className="sk-line w40" />
        <div className="sk-line w65" />
      </div>
      <div className="sk-player">
        <span className="spinner" />
        <p>正在读取本地转录结果…</p>
        <small>{job.name}</small>
      </div>
      <div className="sk-rows">
        {Array.from({ length: 6 }).map((_, i) => (
          <div className="sk-row" key={i}>
            <div className="sk-line w10" />
            <div className="sk-line" style={{ width: `${58 + ((i * 13) % 32)}%` }} />
          </div>
        ))}
      </div>
    </div>
  );
}
