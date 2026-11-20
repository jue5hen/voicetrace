import { useState } from "react";
import type { Job } from "../types";
import { fmtBytes, fmtDate, fmtDuration, stageLabel, statusLabel } from "../lib/format";
import { useApp } from "../lib/store";
import { Icon } from "./Icon";

interface QueueViewProps {
  onImport: () => void;
}

function QueueRow({
  job,
  index,
  onOpen,
}: {
  job: Job;
  index?: number;
  onOpen: () => void;
}) {
  const { cancelJob, retryJob, deleteJob, confirm, setAutoCorrect } = useApp();
  const processing = job.status === "processing";
  const queued = job.status === "queued";
  const error = job.status === "error";
  const done = job.status === "done";
  const canceled = job.status === "canceled";

  const onCancel = async () => {
    const ok = await confirm({
      title: "取消任务",
      description: `确定要取消「${job.name}」吗？已处理的部分不会保留。`,
      confirmText: "取消任务",
      tone: "danger",
    });
    if (ok) await cancelJob(job);
  };

  return (
    <div
      className={`queue-row tone-${job.status}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        // 只响应行本身：否则聚焦取消/重试/删除按钮时 Enter/Space 会被劫持成打开任务。
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="queue-leading">
        {typeof index === "number" ? (
          <span className="queue-index tabular">{String(index).padStart(2, "0")}</span>
        ) : (
          <span className={`queue-dot tone-${job.status}`} />
        )}
      </div>

      <div className="queue-info">
        <div className="queue-name-line">
          <strong title={job.name}>{job.name.replace(/\.[^.]+$/, "")}</strong>
          <span className={`mini-status tone-${job.status}`}>{statusLabel(job.status)}</span>
          {done && job.issues_count > 0 ? (
            <span className="mini-issues">{job.issues_count} 条建议</span>
          ) : null}
        </div>
        <div className="queue-meta">
          <span>{fmtDuration(job.duration_ms)}</span>
          <span>{fmtBytes(job.size_bytes)}</span>
          <span>{processing ? stageLabel(job.stage) : fmtDate(job.created_at)}</span>
          {job.rtf ? <span>{`${(1 / job.rtf).toFixed(1)}× 实时`}</span> : null}
        </div>
        {processing ? (
          <div className="queue-progress">
            <div className="track">
              <progress value={job.progress * 100} max={100} aria-label="转写进度" />
            </div>
            <span className="tabular">{Math.round(job.progress * 100)}%</span>
          </div>
        ) : null}
      </div>

      <div className="queue-actions" onClick={(e) => e.stopPropagation()}>
        {!done ? (
          <label
            className="queue-auto"
            title="转写完成后自动处理重复词与同音词建议"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            <input
              type="checkbox"
              checked={Boolean(job.auto_correct)}
              onChange={(e) => void setAutoCorrect(job.id, e.target.checked)}
            />
            <span>自动校正</span>
          </label>
        ) : null}
        {queued || processing ? (
          <button
            type="button"
            className="icon-btn"
            data-tip={queued ? "取消排队" : "停止转录"}
            aria-label={queued ? "取消排队" : "停止转录"}
            onClick={() => void onCancel()}
          >
            <Icon name="close" size={15} />
          </button>
        ) : null}
        {error || canceled ? (
          <button
            type="button"
            className="icon-btn"
            data-tip={canceled ? "重新加入队列" : "重新转录"}
            aria-label={canceled ? "重新加入队列" : "重新转录"}
            onClick={() => void retryJob(job)}
          >
            <Icon name="retry" size={15} />
          </button>
        ) : null}
        {job.status !== "processing" ? (
          <button
            type="button"
            className="icon-btn danger-hover"
            data-tip="删除任务"
            aria-label="删除任务"
            onClick={() => void deleteJob(job)}
          >
            <Icon name="trash" size={15} />
          </button>
        ) : null}
        <Icon name="chevronRight" size={15} className="queue-chevron" />
      </div>
    </div>
  );
}

export function QueueView({ onImport }: QueueViewProps) {
  const { jobs, queue, toggleQueue, selectJob, setAllAutoCorrect } = useApp();
  const [showAllDone, setShowAllDone] = useState(false);
  const [showAllCanceled, setShowAllCanceled] = useState(false);
  const [autoBusy, setAutoBusy] = useState(false);

  const pending = jobs
    .filter((j) => j.status === "queued")
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const active = jobs.find((j) => j.status === "processing") ?? null;
  const failed = jobs.filter((j) => j.status === "error");
  const done = jobs
    .filter((j) => j.status === "done")
    .sort((a, b) => (b.finished_at ?? b.created_at).localeCompare(a.finished_at ?? a.created_at));
  // C7：取消的任务以前没有任何入口（离开详情页就找不到），这里给它们独立分组。
  const canceled = jobs
    .filter((j) => j.status === "canceled")
    .sort((a, b) => (b.finished_at ?? b.created_at).localeCompare(a.finished_at ?? a.created_at));
  const visibleDone = showAllDone ? done : done.slice(0, 5);
  const visibleCanceled = showAllCanceled ? canceled : canceled.slice(0, 5);
  const totalLive = pending.length + (active ? 1 : 0);
  const liveJobs = active ? [active, ...pending] : pending;
  const allAutoCorrect = liveJobs.length > 0 && liveJobs.every((j) => j.auto_correct);

  const toggleAllAutoCorrect = async () => {
    if (!liveJobs.length || autoBusy) return;
    setAutoBusy(true);
    await setAllAutoCorrect(!allAutoCorrect);
    setAutoBusy(false);
  };

  if (!jobs.length) {
    return (
      <div className="page-scroll queue-page">
        <div className="queue-empty">
          <span className="empty-orb">
            <Icon name="layers" size={26} />
          </span>
          <h1>队列还是空的</h1>
          <p>导入音视频后，任务会在这里按顺序自动处理。</p>
          <button type="button" className="btn primary" onClick={onImport}>
            <Icon name="import" size={16} /> 导入音视频
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="page-scroll queue-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">QUEUE · 串行处理</span>
          <h1>转写队列</h1>
          <p>任务按加入顺序逐个处理，避免显存争抢；随时可以暂停或继续。</p>
        </div>
        <div className="page-head-actions">
          <label
            className="queue-auto-all"
            title="对当前排队和正在处理的任务统一设置"
          >
            <input
              type="checkbox"
              checked={allAutoCorrect}
              disabled={!liveJobs.length || autoBusy}
              onChange={() => void toggleAllAutoCorrect()}
            />
            <span>全部自动校正</span>
          </label>
          {queue.paused ? (
            <span className="paused-pill">
              <Icon name="pause" size={12} /> 已暂停
            </span>
          ) : null}
          <button
            type="button"
            className={`btn ${queue.paused ? "primary" : "ghost"}`}
            onClick={() => void toggleQueue()}
            disabled={!totalLive}
          >
            <Icon name={queue.paused ? "play" : "pause"} size={15} />
            {queue.paused ? "继续队列" : "暂停队列"}
          </button>
        </div>
      </header>

      <div className="queue-stats">
        <div className="queue-stat">
          <span className="stat-icon tone-accent">
            <Icon name="wave" size={17} />
          </span>
          <div>
            <strong className="tabular">{queue.processing}</strong>
            <span>正在处理</span>
          </div>
        </div>
        <div className="queue-stat">
          <span className="stat-icon tone-warn">
            <Icon name="clock" size={17} />
          </span>
          <div>
            <strong className="tabular">{pending.length}</strong>
            <span>等待中</span>
          </div>
        </div>
        <div className="queue-stat">
          <span className="stat-icon tone-ok">
            <Icon name="checkCircle" size={17} />
          </span>
          <div>
            <strong className="tabular">{done.length}</strong>
            <span>已完成</span>
          </div>
        </div>
        <div className="queue-stat">
          <span className="stat-icon tone-err">
            <Icon name="alertCircle" size={17} />
          </span>
          <div>
            <strong className="tabular">{failed.length}</strong>
            <span>需要处理</span>
          </div>
        </div>
      </div>

      {queue.paused && totalLive ? (
        <div className="banner warn-banner">
          <Icon name="pause" size={15} />
          队列已暂停。当前任务结束后不会再开始新任务，点击「继续队列」恢复。
        </div>
      ) : null}

      {active ? (
        <section className="queue-section">
          <div className="section-head">
            <h2>
              <span className="live-dot" /> 正在处理
            </h2>
            <span className="section-note">关闭窗口不影响本地转写</span>
          </div>
          <QueueRow job={active} onOpen={() => selectJob(active.id)} />
        </section>
      ) : null}

      <section className="queue-section">
        <div className="section-head">
          <h2>
            等待中 <span className="count-pill">{pending.length}</span>
          </h2>
          <span className="section-note">按加入顺序自动开始</span>
        </div>
        {pending.length ? (
          <div className="queue-list">
            {pending.map((job, i) => (
              <QueueRow key={job.id} job={job} index={i + 1} onOpen={() => selectJob(job.id)} />
            ))}
          </div>
        ) : active ? (
          <p className="section-empty">没有其他排队中的任务。</p>
        ) : (
          <button type="button" className="queue-drop-hint" onClick={onImport}>
            <span className="queue-drop-icon">
              <Icon name="plus" size={18} />
            </span>
            <span className="queue-drop-copy">
              <strong>队列空闲</strong>
              <em>导入音视频，自动加入队列按顺序转录</em>
            </span>
            <span className="queue-drop-cta">
              选择文件 <kbd>Ctrl O</kbd>
            </span>
          </button>
        )}
      </section>

      {failed.length ? (
        <section className="queue-section">
          <div className="section-head">
            <h2>
              失败 <span className="count-pill err">{failed.length}</span>
            </h2>
            <span className="section-note">查看原因后可重试</span>
          </div>
          <div className="queue-list">
            {failed.map((job) => (
              <QueueRow key={job.id} job={job} onOpen={() => selectJob(job.id)} />
            ))}
          </div>
        </section>
      ) : null}

      {canceled.length ? (
        <section className="queue-section">
          <div className="section-head">
            <h2>
              已取消 <span className="count-pill">{canceled.length}</span>
            </h2>
            {canceled.length > 5 ? (
              <button
                type="button"
                className="text-btn"
                onClick={() => setShowAllCanceled((v) => !v)}
              >
                {showAllCanceled ? "收起" : "查看全部"}
                <Icon name={showAllCanceled ? "chevronUp" : "chevronDown"} size={12} />
              </button>
            ) : (
              <span className="section-note">可重新加入队列或删除</span>
            )}
          </div>
          <div className="queue-list">
            {visibleCanceled.map((job) => (
              <QueueRow key={job.id} job={job} onOpen={() => selectJob(job.id)} />
            ))}
          </div>
        </section>
      ) : null}

      <section className="queue-section">
        <div className="section-head">
          <h2>
            最近完成 <span className="count-pill">{done.length}</span>
          </h2>
          {done.length > 5 ? (
            <button
              type="button"
              className="text-btn"
              onClick={() => setShowAllDone((v) => !v)}
            >
              {showAllDone ? "收起" : "查看全部"}
              <Icon name={showAllDone ? "chevronUp" : "chevronDown"} size={12} />
            </button>
          ) : (
            <span className="section-note">点击可打开校对</span>
          )}
        </div>
        {visibleDone.length ? (
          <div className="queue-list">
            {visibleDone.map((job) => (
              <QueueRow key={job.id} job={job} onOpen={() => selectJob(job.id)} />
            ))}
          </div>
        ) : (
          <p className="section-empty">完成的转录会出现在这里。</p>
        )}
      </section>

      <p className="queue-footnote">
        <Icon name="lock" size={12} /> 所有任务均在本机串行处理，媒体不会离开设备。
      </p>
    </div>
  );
}
