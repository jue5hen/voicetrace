import { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import { engineStatus, restartEngine, type ShellEngineStatus } from "../lib/tauri";
import type { ModelState, ModelsPresent } from "../types";
import { BrandMark, Icon } from "./Icon";

function StepRow({
  state,
  title,
  detail,
}: {
  state: "done" | "active" | "pending" | "error";
  title: string;
  detail: string;
}) {
  return (
    <div className={`boot-step is-${state}`}>
      <span className="boot-step-dot">
        {state === "done" ? (
          <Icon name="check" size={11} strokeWidth={2.4} />
        ) : state === "error" ? (
          <Icon name="alert" size={11} strokeWidth={2.4} />
        ) : state === "active" ? (
          <span className="spinner tiny" />
        ) : null}
      </span>
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}

const PHASE_LABEL: Record<string, string> = {
  starting: "正在启动",
  ready: "运行中",
  failed: "启动失败",
  exited: "已退出",
  unknown: "状态未知",
};

/** 模型状态 → 启动页文案（F3：模型按需加载，不再冒充「已加载」）。 */
function modelSummary(
  state: ModelState | null,
  models: { loaded: boolean }[],
  present: ModelsPresent | null,
): string {
  if (state === "ready") return "模型已就绪，可以开始转录";
  if (state === "loading") return "模型按需加载（首次转录 20–90 秒）";
  if (state === "idle") {
    const ok = present ? [present.aed, present.vad, present.punc].filter(Boolean).length : 0;
    return present
      ? `模型按需加载（首次转录 20–90 秒）· ${ok}/3 模型文件就绪`
      : "模型按需加载（首次转录 20–90 秒）";
  }
  if (state === "failed") return "模型加载失败";
  if (models.length) {
    return models.every((m) => m.loaded)
      ? "模型已全部载入"
      : `${models.filter((m) => m.loaded).length}/${models.length} 个组件就绪`;
  }
  return "等待引擎返回模型状态";
}

export function BootScreen() {
  const { boot, retryBoot, system } = useApp();
  const pct = Math.round(boot.progress * 100);
  const models = boot.models;
  const allModels = models.length > 0 && models.every((m) => m.loaded);
  const failed = Boolean(boot.error);

  // 启动失败时向桌面壳要真实进程状态；浏览器 mock 模式下拿到 null，静默降级。
  const [shell, setShell] = useState<ShellEngineStatus | null>(null);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    if (!failed) {
      setShell(null);
      return;
    }
    let cancelled = false;
    void engineStatus().then((status) => {
      if (!cancelled) setShell(status);
    });
    return () => {
      cancelled = true;
    };
  }, [failed, boot.error]);

  const onRestart = async () => {
    if (restarting) return;
    setRestarting(true);
    // 非 Tauri 环境 restartEngine 返回 false，直接重试启动流程即可。
    await restartEngine();
    setRestarting(false);
    retryBoot();
  };

  /**
   * 「重试启动」：进程已经 failed/exited 时，单纯重跑 bootstrap 只会再失败一次，
   * 先让壳重新拉起引擎；仍是 starting 时只重试前端等待，别杀掉正在加载的进程。
   */
  const onRetry = async () => {
    if (restarting) return;
    const status = (await engineStatus()) ?? shell;
    if (status && (status.phase === "failed" || status.phase === "exited")) {
      setRestarting(true);
      await restartEngine();
      setRestarting(false);
    }
    retryBoot();
  };

  const engineStep: "done" | "active" | "error" = boot.connected
    ? "done"
    : failed
      ? "error"
      : "active";
  const modelStep: "done" | "active" | "pending" | "error" =
    boot.modelState === "failed"
      ? "error"
      : boot.modelState === "ready" || allModels
        ? "done"
        : failed
          ? "pending"
          : "active";
  const dataStep: "done" | "active" | "pending" =
    failed || boot.modelState === "failed"
      ? "pending"
      : modelStep === "done"
        ? "active"
        : "pending";

  const hasShellDetail = Boolean(
    shell && (shell.log_tail || shell.exit_code !== undefined || shell.pid || shell.error),
  );

  return (
    <div className="boot-screen">
      <div className="boot-aurora" aria-hidden="true" />
      <div className="boot-grid" aria-hidden="true" />

      <header className="boot-top">
        <span className="boot-brand">
          <BrandMark size={26} />
          <span>
            声迹 <em>VoiceTrace</em>
          </span>
        </span>
        <span className="boot-top-right">
          <Icon name="lock" size={12} /> 本地离线运行
        </span>
      </header>

      <main className="boot-main">
        <div className="boot-mark">
          <span className="boot-wave" aria-hidden="true">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => (
              <i key={i} style={{ animationDelay: `${i * 90}ms` }} />
            ))}
          </span>
        </div>

        <span className="eyebrow">OFFLINE SPEECH · 中文语音转写</span>
        <h1>
          正在唤醒<span className="accent-text">声迹</span>
        </h1>
        <p className="boot-lead">首次转录时才加载离线模型，加载期间约需 20–90 秒。</p>

        {failed ? (
          <div className="boot-error" role="alert">
            <div className="boot-error-head">
              <span className="boot-error-icon">
                <Icon name="alertCircle" size={18} />
              </span>
              <div>
                <strong>{boot.detail || "启动失败"}</strong>
                <p>{boot.error}</p>
              </div>
            </div>

            {shell ? (
              <dl className="boot-error-meta">
                <div>
                  <dt>进程状态</dt>
                  <dd>{PHASE_LABEL[shell.phase] ?? shell.phase}</dd>
                </div>
                {shell.pid ? (
                  <div>
                    <dt>PID</dt>
                    <dd className="tabular">{shell.pid}</dd>
                  </div>
                ) : null}
                {shell.exit_code !== undefined ? (
                  <div>
                    <dt>退出码</dt>
                    <dd className="tabular">{shell.exit_code}</dd>
                  </div>
                ) : null}
              </dl>
            ) : null}

            {shell?.error ? <p className="boot-error-reason">{shell.error}</p> : null}

            {hasShellDetail && shell?.log_tail ? (
              <details className="boot-error-details">
                <summary>查看引擎日志</summary>
                <pre className="boot-error-log">{shell.log_tail}</pre>
              </details>
            ) : null}

            <div className="boot-error-actions">
              <button
                type="button"
                className="btn primary"
                onClick={() => void onRetry()}
                disabled={restarting}
              >
                <Icon name="retry" size={15} /> 重试启动
              </button>
              <button
                type="button"
                className="btn ghost"
                onClick={() => void onRestart()}
                disabled={restarting}
              >
                <Icon name="retry" size={15} /> {restarting ? "正在重启…" : "重启引擎"}
              </button>
              <span className="boot-error-hint">
                浏览器开发模式请先启动真实引擎（8765），再刷新页面
              </span>
            </div>
          </div>
        ) : (
          <div className="boot-progress">
            <div className="boot-stage-line">
              <span className="boot-stage">
                <span className="spinner" />
                {boot.stage}
              </span>
              <span className="boot-pct tabular">{pct}%</span>
            </div>
            <div className="boot-track">
              <div className="boot-fill" style={{ width: `${Math.max(4, pct)}%` }} />
            </div>
            <div className="boot-detail">
              <span>{boot.detail}</span>
              <span className="tabular">{boot.elapsedS}s</span>
            </div>
          </div>
        )}

        <div className="boot-steps">
          <StepRow
            state={engineStep}
            title="本地推理引擎"
            detail={boot.connected ? "已连接 · 127.0.0.1" : "正在拉起本地进程"}
          />
          <StepRow
            state={modelStep}
            title="语音识别模型"
            detail={modelSummary(boot.modelState, models, boot.modelsPresent)}
          />
          <StepRow
            state={dataStep}
            title="任务与设置"
            detail={dataStep === "active" ? "同步历史任务与偏好" : "等待前一步完成"}
          />
        </div>

        {models.length ? (
          <div className="boot-models">
            {models.slice(0, 5).map((m) => (
              <div key={m.key} className={`boot-model ${m.loaded ? "is-loaded" : ""}`}>
                <span className="boot-model-name">
                  {m.loaded ? <Icon name="check" size={11} strokeWidth={2.4} /> : <span className="boot-model-dot" />}
                  {m.label}
                </span>
                <span className="boot-model-pct tabular">
                  {m.loaded ? "就绪" : `${Math.round((m.progress || 0) * 100)}%`}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </main>

      <footer className="boot-foot">
        <span>
          <Icon name="cpu" size={12} />
          {system?.device ? `${String(system.device).toUpperCase()} · ${system.gpu || "本地推理"}` : "检测推理设备中"}
        </span>
        <span className="boot-version">
          {system?.version ? `声迹 v${system.version.replace(/^v/i, "")}` : "声迹"} · 数据始终留在你的设备上
        </span>
      </footer>
    </div>
  );
}
