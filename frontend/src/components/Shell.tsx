import { useEffect, useRef } from "react";
import { useApp } from "../lib/store";
import { EmptyState } from "./EmptyState";
import { Icon } from "./Icon";
import { QueueView } from "./QueueView";
import { SettingsPage } from "./SettingsPage";
import { TaskEditor } from "./TaskEditor";
import { JobErrorView, ProcessingView, TranscriptSkeleton } from "./ProcessingViews";

export function Shell({ onImport }: { onImport: () => void }) {
  const {
    jobs,
    view,
    setView,
    selectedJobId,
    selectedJob,
    transcript,
    transcriptLoading,
    transcriptError,
    clearSelection,
    connection,
    system,
    toast,
  } = useApp();

  const liveTotal = jobs.filter(
    (j) => j.status === "processing" || j.status === "queued",
  ).length;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      const typing =
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        (e.target as HTMLElement)?.isContentEditable;
      if (typing) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === ",") {
        e.preventDefault();
        setView("settings");
      } else if (e.key.toLowerCase() === "o") {
        e.preventDefault();
        onImport();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onImport, setView]);

  const connLabel =
    connection === "online"
      ? "引擎在线"
      : connection === "polling"
        ? "重连中"
        : "正在连接";

  const modelsMissing = Boolean(
    system?.models_present &&
      (!system.models_present.aed ||
        !system.models_present.vad ||
        !system.models_present.punc),
  );
  const runtimeMissing = system?.runtime_setup
    ? !system.runtime_setup.torch_ready
    : false;
  const envMissing = modelsMissing || runtimeMissing;
  const warnedRef = useRef(false);

  // 首次发现运行环境缺失时提示一次；之后由设置页的小红点持续提醒。
  useEffect(() => {
    if (!envMissing || warnedRef.current) return;
    warnedRef.current = true;
    toast(
      "warn",
      "运行环境未就绪：请到「设置」安装推理组件并下载离线模型后再开始转录。",
    );
  }, [envMissing, toast]);

  const goHome = () => {
    setView("workspace");
    clearSelection();
  };

  return (
    <div className="shell app-shell">
      <nav className="app-nav" aria-label="主导航">
        <div className="app-nav-head">
          <strong>工作区</strong>
          <span>本地语音转写</span>
        </div>

        <div className="app-nav-group">
          <button
            type="button"
            className={`app-nav-item ${view === "workspace" && !selectedJobId ? "is-active" : ""}`}
            onClick={goHome}
          >
            <Icon name="wave" size={18} />
            <span className="app-nav-label">
              <strong>转录</strong>
              <em>单文件 · 实时进度</em>
            </span>
          </button>
          <button
            type="button"
            className={`app-nav-item ${view === "queue" ? "is-active" : ""}`}
            onClick={() => setView("queue")}
          >
            <Icon name="queue" size={18} />
            <span className="app-nav-label">
              <strong>批量转录</strong>
              <em>按队列串行处理</em>
            </span>
            {liveTotal ? <span className="app-nav-badge tabular">{liveTotal}</span> : null}
          </button>
        </div>

        <div className="app-nav-spacer" />

        <button
          type="button"
          className={`app-nav-item app-nav-settings ${view === "settings" ? "is-active" : ""}`}
          onClick={() => setView("settings")}
        >
          <Icon name="settings" size={18} />
          <span className="app-nav-label">
            <strong>设置</strong>
            <em>引擎、字幕与存储</em>
          </span>
          {envMissing ? (
            <span className="app-nav-badge tabular" title="运行环境未就绪">
              !
            </span>
          ) : null}
        </button>

        <div
          className="app-nav-status"
          title={`${connLabel}${system?.device ? ` · ${String(system.device).toUpperCase()}` : ""}`}
        >
          <span className={`conn-dot ${connection}`} />
          <span>{connLabel}</span>
          <span className="app-nav-device">
            {system?.device ? String(system.device).toUpperCase() : "本地"}
          </span>
        </div>
      </nav>

      <main className="main-area">
        {view === "settings" ? (
          <SettingsPage />
        ) : view === "queue" ? (
          <QueueView onImport={onImport} />
        ) : selectedJob ? (
          selectedJob.status === "processing" || selectedJob.status === "queued" ? (
            <ProcessingView job={selectedJob} />
          ) : (selectedJob.status === "error" || selectedJob.status === "canceled") &&
            !selectedJob.has_transcript ? (
            <JobErrorView job={selectedJob} />
          ) : transcriptLoading && !transcript ? (
            <TranscriptSkeleton job={selectedJob} />
          ) : transcriptError ? (
            <JobErrorView job={{ ...selectedJob, status: "error", error: transcriptError }} />
          ) : (
            <TaskEditor key={selectedJob.id} job={selectedJob} />
          )
        ) : (
          <EmptyState onImport={onImport} />
        )}
      </main>
    </div>
  );
}
