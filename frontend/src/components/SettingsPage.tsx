import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ModelDownloadStatus, RuntimeSetupStatus, Settings, SystemInfo } from "../types";
import { DEFAULT_SETTINGS, useApp } from "../lib/store";
import { api } from "../lib/engine";
import { fmtBytes } from "../lib/format";
import { isTauriRuntime, pickDirectory } from "../lib/tauri";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

/** 模型加载状态文案（F3：按需加载，不再暗示「启动即加载完」）。 */
function modelStateText(sys: SystemInfo | null): string {
  const state = sys?.model_state;
  if (state === "ready") return "模型已就绪";
  if (state === "loading") return "正在加载模型（首次 20–90 秒）";
  if (state === "failed") return "模型加载失败";
  if (state === "idle") return "模型按需加载（首次转录 20–90 秒）";
  return "模型状态未知";
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`switch ${checked ? "is-on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-knob" />
    </button>
  );
}

function Field({
  label,
  hint,
  children,
  wide,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={`set-field ${wide ? "is-wide" : ""}`}>
      <div className="set-field-label">
        <span>{label}</span>
        {hint ? <small>{hint}</small> : null}
      </div>
      <div className="set-field-control">{children}</div>
    </div>
  );
}

function NumberBox({
  value,
  min,
  max,
  step = 1,
  suffix,
  onChange,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (next: number) => void;
}) {
  return (
    <span className="num-box">
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (!Number.isFinite(n)) return;
          onChange(Math.min(max, Math.max(min, n)));
        }}
      />
      {suffix ? <em>{suffix}</em> : null}
    </span>
  );
}

function Section({
  icon,
  accent,
  title,
  desc,
  children,
}: {
  icon: IconName;
  accent?: boolean;
  title: string;
  desc: string;
  children: ReactNode;
}) {
  return (
    <section className="settings-card">
      <div className="settings-card-head">
        <span className={`set-section-icon ${accent ? "accent" : ""}`}>
          <Icon name={icon} size={15} />
        </span>
        <div>
          <h3>{title}</h3>
          <p>{desc}</p>
        </div>
      </div>
      <div className="settings-card-body">{children}</div>
    </section>
  );
}

const MODEL_KEYS = ["aed", "vad", "punc"] as const;
const MODEL_FALLBACK: Record<string, string> = {
  aed: "语音识别模型",
  vad: "语音活动检测模型",
  punc: "标点模型",
};

/** CUDA 推理运行时：安装包不带 torch，首次使用需下载并安装。 */
function RuntimeSection() {
  const { system, toast } = useApp();
  const [status, setStatus] = useState<RuntimeSetupStatus | null>(
    system?.runtime_setup ?? null,
  );
  const [starting, setStarting] = useState(false);
  const [mirror, setMirror] = useState(
    () => window.localStorage.getItem("vt.runtimeMirror") === "1",
  );
  const [proxy, setProxy] = useState(
    () => window.localStorage.getItem("vt.runtimeProxy") ?? "",
  );

  useEffect(() => {
    if (system?.runtime_setup) setStatus(system.runtime_setup);
  }, [system]);

  const running = status?.state === "running";

  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void api
        .runtime()
        .then((next) => {
          if (!cancelled) setStatus(next);
        })
        .catch(() => {
          /* 引擎短暂不可用时保留上次进度 */
        });
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running]);

  const ready = status?.torch_ready === true;
  const installing = status?.phase === "install";
  const pct = Math.round((status?.progress ?? 0) * 100);
  const approxTotal = status?.total_bytes || 2_726_626_124;
  const supported = status?.supported !== false;

  const start = async () => {
    if (starting || running) return;
    window.localStorage.setItem("vt.runtimeMirror", mirror ? "1" : "0");
    window.localStorage.setItem("vt.runtimeProxy", proxy.trim());
    setStarting(true);
    try {
      const next = await api.installRuntime({ proxy: proxy.trim(), mirror });
      setStatus(next);
      toast("info", "推理组件开始下载，完成后建议重启引擎再转录。");
    } catch (err) {
      toast("error", err instanceof Error ? err.message : "推理组件安装启动失败");
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    try {
      setStatus(await api.cancelRuntime());
    } catch {
      toast("error", "取消安装失败，请稍后重试。");
    }
  };

  return (
    <Section
      icon="cpu"
      title="推理组件"
      desc="CUDA 版 PyTorch，安装后才能开始转录；安装包不内置以控制体积"
    >
      <div className="model-list">
        <div className={`model-row ${ready ? "is-ready" : ""}`}>
          <span className="model-row-name">
            {ready ? (
              <Icon name="checkCircle" size={14} />
            ) : running ? (
              <span className="spinner tiny" />
            ) : (
              <Icon name="download" size={14} />
            )}
            PyTorch 2.1.0 + CUDA 11.8
          </span>
          <span className="model-row-state">
            {ready ? "已安装" : running ? (installing ? "安装中" : "下载中") : "未安装"}
          </span>
        </div>
      </div>

      {running ? (
        <div className="model-progress">
          <div className="model-progress-head">
            <span>{installing ? "正在安装到运行时目录…" : "正在下载 CUDA 运行时"}</span>
            <span className="tabular">{installing ? "" : `${pct}%`}</span>
          </div>
          <div className="model-track">
            <div
              className="model-fill"
              style={{ width: `${installing ? 100 : Math.max(2, pct)}%` }}
            />
          </div>
          <div className="model-progress-meta">
            <span className="tabular">
              {installing
                ? "安装期间请勿关闭应用"
                : `${fmtBytes(status?.downloaded_bytes ?? 0)} / ${fmtBytes(status?.total_bytes ?? 0)}`}
            </span>
            <button type="button" className="text-btn" onClick={() => void cancel()}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="model-actions">
          <button
            type="button"
            className="btn primary"
            disabled={starting || ready || !supported}
            onClick={() => void start()}
          >
            <Icon name="download" size={14} />
            {ready
              ? "推理组件已安装"
              : `下载并安装（约 ${fmtBytes(approxTotal)}）`}
          </button>
          {status?.error ? (
            <span className="model-error" title={status.error}>
              {status.error}
            </span>
          ) : null}
        </div>
      )}

      {supported ? (
        <div className="model-advanced">
          <label className="model-check">
            <input
              type="checkbox"
              checked={mirror}
              disabled={running}
              onChange={(e) => setMirror(e.target.checked)}
            />
            优先使用阿里云镜像
          </label>
          <input
            className="model-proxy"
            value={proxy}
            disabled={running}
            spellCheck={false}
            placeholder="代理（可选），如 http://127.0.0.1:7890"
            onChange={(e) => setProxy(e.target.value)}
          />
        </div>
      ) : (
        <p className="model-hint">源码开发模式：请直接在 venv 中执行 pip install -r requirements.txt。</p>
      )}

      <p className="model-hint">
        下载源为 PyTorch 官方仓库（可切换阿里云镜像），安装后建议在启动页点一次「重启引擎」。
      </p>
    </Section>
  );
}

/** 离线模型：一键从 Hugging Face 下载 FireRedASR2S 开源权重。 */
function ModelSection() {
  const { system, toast } = useApp();
  const [status, setStatus] = useState<ModelDownloadStatus | null>(
    system?.models_download ?? null,
  );
  const [starting, setStarting] = useState(false);
  const [mirror, setMirror] = useState(
    () => window.localStorage.getItem("vt.modelMirror") === "1",
  );
  const [proxy, setProxy] = useState(
    () => window.localStorage.getItem("vt.modelProxy") ?? "",
  );

  useEffect(() => {
    if (system?.models_download) setStatus(system.models_download);
  }, [system]);

  const running = status?.state === "running";

  // 下载中本地加密轮询：/api/system 每 8 秒刷新一次太慢，进度条会显得卡住。
  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void api
        .models()
        .then((next) => {
          if (!cancelled) setStatus(next);
        })
        .catch(() => {
          /* 引擎短暂不可用时保留上次进度 */
        });
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running]);

  const present = system?.models_present;
  const readyOf = (key: string) =>
    status?.models?.[key]?.ready ?? (present ? present[key as keyof typeof present] : false);
  const missing = MODEL_KEYS.filter((key) => !readyOf(key));
  const pct = Math.round((status?.progress ?? 0) * 100);
  const approxTotal = status?.total_bytes || 5_560_000_000;

  const start = async () => {
    if (starting || running) return;
    window.localStorage.setItem("vt.modelMirror", mirror ? "1" : "0");
    window.localStorage.setItem("vt.modelProxy", proxy.trim());
    setStarting(true);
    try {
      const next = await api.downloadModels({
        proxy: proxy.trim(),
        endpoint: mirror ? "https://hf-mirror.com" : "",
      });
      setStatus(next);
      toast("info", "模型下载已开始，可以继续使用其他功能。");
    } catch (err) {
      toast("error", err instanceof Error ? err.message : "模型下载启动失败");
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    try {
      setStatus(await api.cancelModelDownload());
    } catch {
      toast("error", "取消下载失败，请稍后重试。");
    }
  };

  return (
    <Section
      icon="download"
      accent
      title="离线模型"
      desc="首次使用需下载开源模型；下载完成后可完全离线转录"
    >
      <div className="model-list">
        {MODEL_KEYS.map((key) => {
          const ready = readyOf(key);
          const active = running && status?.current === key;
          return (
            <div key={key} className={`model-row ${ready ? "is-ready" : ""}`}>
              <span className="model-row-name">
                {ready ? (
                  <Icon name="checkCircle" size={14} />
                ) : active ? (
                  <span className="spinner tiny" />
                ) : (
                  <Icon name="download" size={14} />
                )}
                {status?.models?.[key]?.label ?? MODEL_FALLBACK[key]}
              </span>
              <span className="model-row-state">
                {ready ? "已就绪" : active ? "下载中" : "未下载"}
              </span>
            </div>
          );
        })}
      </div>

      {running ? (
        <div className="model-progress">
          <div className="model-progress-head">
            <span>
              正在下载
              {status?.current ? ` · ${MODEL_FALLBACK[status.current] ?? status.current}` : ""}
            </span>
            <span className="tabular">{pct}%</span>
          </div>
          <div className="model-track">
            <div className="model-fill" style={{ width: `${Math.max(2, pct)}%` }} />
          </div>
          <div className="model-progress-meta">
            <span className="tabular">
              {fmtBytes(status?.downloaded_bytes ?? 0)} / {fmtBytes(status?.total_bytes ?? 0)}
            </span>
            <button type="button" className="text-btn" onClick={() => void cancel()}>
              取消下载
            </button>
          </div>
        </div>
      ) : (
        <div className="model-actions">
          <button
            type="button"
            className="btn primary"
            disabled={starting || missing.length === 0}
            onClick={() => void start()}
          >
            <Icon name="download" size={14} />
            {missing.length === 0
              ? "模型已全部就绪"
              : `下载缺失模型（约 ${fmtBytes(approxTotal)}）`}
          </button>
          {status?.error ? (
            <span className="model-error" title={status.error}>
              {status.error}
            </span>
          ) : null}
        </div>
      )}

      <div className="model-advanced">
        <label className="model-check">
          <input
            type="checkbox"
            checked={mirror}
            disabled={running}
            onChange={(e) => setMirror(e.target.checked)}
          />
          使用 hf-mirror.com 镜像
        </label>
        <input
          className="model-proxy"
          value={proxy}
          disabled={running}
          spellCheck={false}
          placeholder="代理（可选），如 http://127.0.0.1:7890"
          onChange={(e) => setProxy(e.target.value)}
        />
      </div>

      <p className="model-hint">
        模型来自 FireRedASR2S 开源项目（Hugging Face），保存位置：
        <code>{system?.paths?.models_dir || "程序目录/models"}</code>
      </p>
    </Section>
  );
}

export function SettingsPage() {
  const { settings, saveSettings, system, toast, setView } = useApp();
  const [draft, setDraft] = useState<Settings>(settings);
  const [saving, setSaving] = useState(false);
  /** 上一次与服务端对齐的草稿基线，用来判断本地有没有未保存修改。 */
  const baselineRef = useRef<Settings>(settings);
  /** 本次提交的草稿快照：只有草稿仍是它时，保存回包才允许覆盖（C1）。 */
  const pendingRef = useRef<Settings | null>(null);

  // C1：保存成功后服务端会回写 settings。只有本地草稿没有继续改动时才跟随服务端；
  // 否则保留保存期间的新输入，并继续保持「有未保存的更改」，绝不静默清空。
  useEffect(() => {
    const expected = pendingRef.current ?? baselineRef.current;
    baselineRef.current = settings;
    pendingRef.current = null;
    setDraft((cur) => (JSON.stringify(cur) === JSON.stringify(expected) ? settings : cur));
  }, [settings]);

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(settings), [draft, settings]);

  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
  };

  const submit = async () => {
    if (!dirty || saving) return;
    const submitted = draft;
    pendingRef.current = submitted;
    setSaving(true);
    const ok = await saveSettings(submitted);
    setSaving(false);
    if (!ok) {
      // C2：失败保留草稿与脏标记；清掉待采纳标记，避免之后被服务端回包覆盖。
      pendingRef.current = null;
    }
  };

  const browse = async () => {
    if (!isTauriRuntime()) {
      toast("info", "浏览器开发模式无法访问本地目录，请在桌面版中选择。");
      return;
    }
    const dir = await pickDirectory();
    if (dir) set("export_dir", dir);
  };

  const reset = () => setDraft({ ...DEFAULT_SETTINGS, export_dir: draft.export_dir });

  return (
    <div className="settings-screen">
      <div className="page-scroll settings-page">
      <header className="settings-head">
        <div>
          <span className="eyebrow">PREFERENCES</span>
          <h1>设置</h1>
          <p>
            修改会自动写入本机 <code>data/settings.json</code>，重启软件后仍然保留。
          </p>
        </div>
        <div className="settings-head-meta">
          <span>
            <Icon name="cpu" size={12} />
            {system?.version ? `声迹 ${system.version}` : "声迹"}
            {system?.device ? ` · ${String(system.device).toUpperCase()}` : ""}
            {system?.gpu ? ` · ${system.gpu}` : ""}
          </span>
          <span>
            <Icon name="layers" size={12} />
            {modelStateText(system)}
            {system?.sample_rate ? ` · ${(system.sample_rate / 1000).toFixed(1)} kHz` : ""}
          </span>
          <span>
            <Icon name="lock" size={12} /> 设置仅保存在本机
          </span>
        </div>
      </header>

      <div className="settings-grid">
        <RuntimeSection />
        <ModelSection />

        <Section
          icon="cpu"
          title="识别引擎"
          desc="控制识别质量、显存占用与长音频切分"
        >
          <Field label="计算设备" hint="更改后需重启软件，引擎会重新加载模型">
            <div className="segment">
              {(
                [
                  ["auto", "自动选择"],
                  ["cuda", "GPU"],
                  ["cpu", "CPU"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={draft.device === value ? "is-active" : ""}
                  onClick={() => set("device", value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </Field>
          <Field label="半精度推理" hint="FP16，显存占用约减半，质量几乎无差别">
            <Toggle checked={draft.half} onChange={(v) => set("half", v)} label="半精度推理" />
          </Field>
          <Field label="搜索束宽" hint="越大越准，速度略慢">
            <NumberBox
              value={draft.beam_size}
              min={1}
              max={8}
              onChange={(v) => set("beam_size", v)}
            />
          </Field>
          <Field label="批大小" hint="一次送入的音频片段数">
            <NumberBox
              value={draft.batch_size}
              min={1}
              max={16}
              onChange={(v) => set("batch_size", v)}
            />
          </Field>
          <Field label="长音频窗口" hint="超过该长度会分段处理">
            <NumberBox
              value={draft.window_minutes}
              min={5}
              max={120}
              suffix="分钟"
              onChange={(v) => set("window_minutes", v)}
            />
          </Field>
        </Section>

        <Section
          icon="sparkle"
          accent
          title="字幕与校对"
          desc="控制断句、口语清理与建议密度"
        >
          <Field label="字幕单行字数" hint="断句与导出换行的依据">
            <NumberBox
              value={draft.subtitle_max_chars}
              min={10}
              max={48}
              suffix="字"
              onChange={(v) => set("subtitle_max_chars", v)}
            />
          </Field>
          <Field label="字幕最大行数">
            <NumberBox
              value={draft.subtitle_max_lines}
              min={1}
              max={3}
              suffix="行"
              onChange={(v) => set("subtitle_max_lines", v)}
            />
          </Field>
          <Field label="合并停顿" hint="停顿短于该值的相邻短句会合并成一条字幕">
            <NumberBox
              value={draft.subtitle_merge_gap_ms}
              min={200}
              max={2000}
              step={100}
              suffix="毫秒"
              onChange={(v) => set("subtitle_merge_gap_ms", v)}
            />
          </Field>
          <Field
            label="清理重复口吃"
            hint="去掉「老老师→老师」「这个这个→这个」这类重复音节，识别后即生效"
          >
            <Toggle
              checked={draft.subtitle_clean_stutter}
              onChange={(v) => set("subtitle_clean_stutter", v)}
              label="清理重复口吃"
            />
          </Field>
          <Field
            label="清理口语语气词"
            hint="去掉句首句尾的「嗯、呃、诶、啊」等，不误伤「哎呀、天啊」"
          >
            <Toggle
              checked={draft.subtitle_clean_fillers}
              onChange={(v) => set("subtitle_clean_fillers", v)}
              label="清理口语语气词"
            />
          </Field>
          <Field label="标点转为空格" hint="导出与预览时将「，。、」替换为空格">
            <Toggle
              checked={draft.subtitle_punct_space}
              onChange={(v) => set("subtitle_punct_space", v)}
              label="标点转为空格"
            />
          </Field>
          <Field label="重复片段阈值" hint="相同短语出现次数达到该值才提示">
            <NumberBox
              value={draft.min_ngram_count}
              min={2}
              max={10}
              onChange={(v) => set("min_ngram_count", v)}
            />
          </Field>
          <Field label="拼音近似匹配" hint="同音字建议更积极，可能增加误报">
            <Toggle
              checked={draft.fuzzy_pinyin}
              onChange={(v) => set("fuzzy_pinyin", v)}
              label="拼音近似匹配"
            />
          </Field>
        </Section>

        <Section icon="folder" title="文件与存储" desc="中间文件、自动导出与缓存">
          <Field label="保留中间 WAV" hint="便于排查问题，会占用更多磁盘">
            <Toggle checked={draft.keep_wav} onChange={(v) => set("keep_wav", v)} label="保留中间 WAV" />
          </Field>
          <Field
            label="转录后自动导出 SRT"
            hint="转录完成后把同名字幕写到视频所在目录；已有同名文件时追加序号，不覆盖"
          >
            <Toggle
              checked={draft.auto_export}
              onChange={(v) => set("auto_export", v)}
              label="转录后自动导出 SRT"
            />
          </Field>
          <Field label="默认导出目录" hint="手动导出时的默认目录；留空则用系统视频文件夹" wide>
            <div className="path-row">
              <span className="path-value" title={draft.export_dir || "系统视频文件夹"}>
                {draft.export_dir || "系统视频文件夹"}
              </span>
              <button type="button" className="btn small ghost" onClick={() => void browse()}>
                <Icon name="folder" size={13} /> 选择
              </button>
              {draft.export_dir ? (
                <button
                  type="button"
                  className="icon-btn"
                  aria-label="清除导出目录"
                  data-tip="恢复默认"
                  onClick={() => set("export_dir", "")}
                >
                  <Icon name="close" size={13} />
                </button>
              ) : null}
            </div>
          </Field>
        </Section>

        <Section icon="keyboard" title="外观与交互" desc="界面动效与操作习惯">
          <Field label="减少动效" hint="关闭页面过渡与播放器动画，低配设备更流畅">
            <Toggle
              checked={draft.reduce_motion}
              onChange={(v) => set("reduce_motion", v)}
              label="减少动效"
            />
          </Field>
          <div className="settings-tips">
            <div>
              <kbd>Ctrl</kbd> + <kbd>O</kbd> 打开文件
            </div>
            <div>
              <kbd>Ctrl</kbd> + <kbd>,</kbd> 打开设置
            </div>
            <div>
              <kbd>Space</kbd> 播放 / 暂停
            </div>
            <div>
              <kbd>↑</kbd> <kbd>↓</kbd> 切换字幕
            </div>
          </div>
        </Section>
      </div>
      </div>

      <footer className="settings-actions">
        <button type="button" className="text-btn" onClick={reset} disabled={!dirty}>
          恢复默认
        </button>
        <span className={`settings-dirty ${dirty ? "is-dirty" : ""}`}>
          {dirty ? "有未保存的更改" : "所有更改已保存"}
        </span>
        <span className="foot-spacer" />
        <button type="button" className="btn ghost" onClick={() => setView("workspace")}>
          返回转录
        </button>
        <button
          type="button"
          className="btn primary"
          onClick={() => void submit()}
          disabled={!dirty || saving}
        >
          {saving ? <span className="spinner tiny" /> : <Icon name="check" size={14} />}
          {saving ? "保存中" : "保存设置"}
        </button>
      </footer>
    </div>
  );
}
