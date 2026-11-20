import {
  isTauriRuntime,
  readShellEngineStatus,
  type ShellEnginePhase,
  type ShellEngineStatus,
} from "./engine";

/**
 * Tauri 壳能力封装。所有调用都做运行时判断，
 * 浏览器开发模式下自动降级，不会阻断前端。
 */

export { isTauriRuntime };
export type { ShellEnginePhase, ShellEngineStatus };

export async function invokeSafe<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<T>(command, args);
  } catch {
    // 命令缺失或壳未就绪：调用方拿 null 自行降级，不打印调试噪音。
    return null;
  }
}

/* ---------------- 引擎进程状态（Tauri 壳命令，F5） ---------------- */

/** invoke("engine_status")：类型与整形在 lib/engine.ts（避免循环依赖）。 */
export async function engineStatus(): Promise<ShellEngineStatus | null> {
  return readShellEngineStatus();
}

/** invoke("engine_restart")：幂等重启引擎，成功返回 true。 */
export async function restartEngine(): Promise<boolean> {
  const res = await invokeSafe<{ ok?: boolean }>("engine_restart");
  return res?.ok === true;
}

const VIDEO_EXTS = [
  "mp4",
  "mkv",
  "mov",
  "avi",
  "flv",
  "webm",
  "ts",
  "m4v",
  "mp3",
  "wav",
  "m4a",
  "aac",
  "flac",
  "ogg",
];

export async function pickMediaFiles(): Promise<string[]> {
  if (!isTauriRuntime()) return [];
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const picked = await open({
      multiple: true,
      title: "选择要转录的视频或音频",
      filters: [{ name: "视频 / 音频", extensions: VIDEO_EXTS }],
    });
    if (!picked) return [];
    return Array.isArray(picked) ? picked : [picked];
  } catch {
    return [];
  }
}

/**
 * 解析导出对话框的起始目录。
 * 优先使用用户设置的导出目录；否则落到系统“视频”目录，避免直接写源视频
 * 所在目录时被 Windows 的受保护文件夹策略拦下。
 */
export async function defaultExportDir(preferred?: string): Promise<string> {
  const custom = (preferred || "").trim();
  if (custom) return custom;
  if (!isTauriRuntime()) return "";
  try {
    const { videoDir } = await import("@tauri-apps/api/path");
    return await videoDir();
  } catch {
    return "";
  }
}

/**
 * 打开原生保存框。取消返回 null，对话框自身失败则抛出错误，让上层提示并
 * 自动落到引擎的默认导出目录，而不是静默当作“用户取消”。
 */
/**
 * 直接导出路径：不经过 Windows 原生“另存为”对话框。
 * 该对话框在部分机器上会误判目录权限，导致任何位置都无法保存；这里改为
 * 写入用户设置的目录，未设置时用系统“视频”目录，失败再由引擎兜底。
 */
export async function defaultExportFilePath(
  defaultName: string,
  preferredDir?: string,
): Promise<string | null> {
  const dir = await defaultExportDir(preferredDir);
  if (!dir) return null;
  const { join } = await import("@tauri-apps/api/path");
  return join(dir, defaultName);
}

export async function pickExportPath(
  defaultName: string,
  extension: string,
  label: string,
  defaultDir?: string,
): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { save } = await import("@tauri-apps/plugin-dialog");
  const dir = (defaultDir || "").trim().replace(/[\\/]+$/, "");
  const sep = dir.includes("\\") ? "\\" : "/";
  return save({
    title: "导出字幕",
    defaultPath: dir ? `${dir}${sep}${defaultName}` : defaultName,
    filters: [{ name: label, extensions: [extension] }],
  });
}

export async function pickDirectory(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const dir = await open({
      directory: true,
      multiple: false,
      title: "选择导出目录",
    });
    return typeof dir === "string" ? dir : null;
  } catch {
    return null;
  }
}

/** 在资源管理器中定位文件（壳实现 open_path）。 */
export async function revealPath(path: string): Promise<boolean> {
  const res = await invokeSafe("open_path", { path });
  return res !== null;
}

export async function startDraggingWindow(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().startDragging();
  } catch {
    // 无边框窗口拖动失败不影响主流程。
  }
}

export async function minimizeWindow(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().minimize();
  } catch {
    // 窗口操作失败不影响主流程。
  }
}

export async function toggleMaximizeWindow(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().toggleMaximize();
  } catch {
    // 窗口操作失败不影响主流程。
  }
}

export async function closeWindow(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().close();
  } catch {
    // 关闭失败交给用户再点一次。
  }
}

export interface DragDropHandlers {
  onEnter: () => void;
  onLeave: () => void;
  onDrop: (paths: string[]) => void;
}

/** Tauri 2 的 webview 拖放事件；返回取消订阅函数。 */
export function listenTauriDragDrop(handlers: DragDropHandlers): () => void {
  if (!isTauriRuntime()) return () => undefined;
  let unlisten: (() => void) | null = null;
  let disposed = false;
  void (async () => {
    try {
      const { getCurrentWebview } = await import("@tauri-apps/api/webview");
      const stop = await getCurrentWebview().onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type === "enter" || payload.type === "over") handlers.onEnter();
        else if (payload.type === "leave") handlers.onLeave();
        else if (payload.type === "drop") {
          handlers.onLeave();
          if (payload.paths?.length) handlers.onDrop(payload.paths);
        }
      });
      if (disposed) stop();
      else unlisten = stop;
    } catch {
      // 拖放不可用：界面仍可通过按钮导入。
    }
  })();
  return () => {
    disposed = true;
    unlisten?.();
  };
}
