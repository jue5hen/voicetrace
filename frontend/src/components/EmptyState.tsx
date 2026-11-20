import { useState } from "react";
import { fmtDate, fmtDuration, statusLabel, statusTone } from "../lib/format";
import { useApp } from "../lib/store";
import { Icon } from "./Icon";

const GUIDE_KEY = "voicetrace.guide.seen.v1";

function hasSeenGuide() {
  try {
    return window.localStorage.getItem(GUIDE_KEY) === "1";
  } catch {
    return false;
  }
}

export function EmptyState({ onImport }: { onImport: () => void }) {
  const { jobs, selectJob } = useApp();
  const [showGuide, setShowGuide] = useState(() => !hasSeenGuide());

  const dismissGuide = () => {
    setShowGuide(false);
    try {
      window.localStorage.setItem(GUIDE_KEY, "1");
    } catch {
      // 隐私模式下无法写入 localStorage，忽略即可。
    }
  };

  const recent = jobs.slice(0, 3);

  return (
    <div className="page-scroll empty-page">
      <div className="empty-hero">
        <div className="empty-hero-copy">
          <span className="eyebrow">
            <span className="live-dot is-idle" /> 本地离线 · 中文语音转写
          </span>
          <h1>
            把声音，
            <br />
            <span className="accent-text">变成可读的文字。</span>
          </h1>
          <p>
            导入音视频，自动生成带标点的字幕初稿。逐句校对、一键纠错、导出字幕——
            全程在你的电脑上完成，文件不出本机。
          </p>
          <div className="hero-hint">
            <kbd>Ctrl</kbd>
            <span>+</span>
            <kbd>O</kbd>
            <span className="hero-hint-label">随时打开文件</span>
          </div>
        </div>

        <button type="button" className="dropzone" onClick={onImport}>
          <span className="dropzone-glow" aria-hidden="true" />
          <span className="dropzone-icon">
            <span className="dz-ring" />
            <Icon name="import" size={24} />
          </span>
          <strong>拖入文件，立即开始</strong>
          <span className="dz-sub">或点击选择 · 支持一次导入多个</span>
          <span className="dz-chips">
            <span>视频</span>
            <span>音频</span>
            <span>文件夹拖放</span>
          </span>
          <span className="dz-formats">MP4 · MKV · MOV · MP3 · WAV 等 14 种格式</span>
        </button>
      </div>

      {showGuide ? (
        <div className="guide-card">
          <div className="guide-title">
            <Icon name="sparkle" size={15} />
            <strong>三步开始</strong>
            <span>首次使用指南</span>
          </div>
          <ol className="guide-steps">
            <li>
              <span className="guide-num tabular">01</span>
              <div>
                <strong>导入音视频</strong>
                <p>拖入或选择文件，自动开始转写</p>
              </div>
            </li>
            <li>
              <span className="guide-num tabular">02</span>
              <div>
                <strong>逐句校对</strong>
                <p>双击修改，右侧建议一键替换</p>
              </div>
            </li>
            <li>
              <span className="guide-num tabular">03</span>
              <div>
                <strong>导出字幕</strong>
                <p>SRT / VTT / ASS / TXT / JSON</p>
              </div>
            </li>
          </ol>
          <button type="button" className="icon-btn" aria-label="关闭指南" onClick={dismissGuide}>
            <Icon name="close" size={15} />
          </button>
        </div>
      ) : null}

      <div className="feature-row">
        <div className="feature">
          <span className="feature-icon">
            <Icon name="lock" size={17} />
          </span>
          <div>
            <strong>完全离线</strong>
            <p>音频、字幕与模型都在本机</p>
          </div>
        </div>
        <div className="feature">
          <span className="feature-icon">
            <Icon name="fileText" size={17} />
          </span>
          <div>
            <strong>中文智能标点</strong>
            <p>自动断句、分段与字幕节奏</p>
          </div>
        </div>
        <div className="feature">
          <span className="feature-icon">
            <Icon name="sparkle" size={17} />
          </span>
          <div>
            <strong>逐句校对</strong>
            <p>同音字、重复词自动提示</p>
          </div>
        </div>
      </div>

      {recent.length ? (
        <section className="recent">
          <div className="recent-head">
            <h2>最近任务</h2>
            <span>继续上次的校对，或开始新的转录</span>
          </div>
          <div className="recent-grid">
            {recent.map((job) => (
              <button
                key={job.id}
                type="button"
                className="recent-card"
                onClick={() => selectJob(job.id)}
              >
                <span className="recent-top">
                  <span className={`mini-status tone-${statusTone(job.status)}`}>
                    {statusLabel(job.status)}
                  </span>
                  <span className="recent-date">{fmtDate(job.created_at)}</span>
                </span>
                <strong title={job.name}>{job.name.replace(/\.[^.]+$/, "")}</strong>
                <span className="recent-meta">
                  {fmtDuration(job.duration_ms)}
                  {job.status === "done" ? (
                    <>
                      <i />
                      {job.issues_count > 0 ? `${job.issues_count} 条建议` : "没有待处理建议"}
                    </>
                  ) : job.status === "processing" ? (
                    <>
                      <i />
                      {Math.round(job.progress * 100)}%
                    </>
                  ) : (
                    <>
                      <i />
                      {statusLabel(job.status)}
                    </>
                  )}
                </span>
                <Icon name="arrowRight" size={15} className="recent-arrow" />
              </button>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
