import { Component, useEffect, useRef, useState } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { AppProvider, useApp } from "./lib/store";
import { isTauriRuntime, listenTauriDragDrop, pickMediaFiles } from "./lib/tauri";
import { BootScreen } from "./components/BootScreen";
import { Shell } from "./components/Shell";
import { Toasts, ConfirmDialog } from "./components/Feedback";
import { WindowBar } from "./components/WindowBar";
import { Icon } from "./components/Icon";

const ACCEPT =
  "video/*,audio/*,.mp4,.mkv,.mov,.avi,.flv,.webm,.ts,.m4v,.mp3,.wav,.m4a,.aac,.flac,.ogg";

function DropOverlay() {
  return (
    <div className="drop-overlay" aria-hidden="true">
      <div className="drop-overlay-inner">
        <span className="drop-overlay-icon">
          <span className="drop-overlay-pulse" />
          <Icon name="import" size={30} />
        </span>
        <strong>松开鼠标，开始转写</strong>
        <span>视频或音频 · 支持一次拖入多个文件</span>
      </div>
    </div>
  );
}

function Workspace() {
  const { importFiles, importPaths, toast } = useApp();
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const depth = useRef(0);

  const openPicker = async () => {
    if (isTauriRuntime()) {
      const files = await pickMediaFiles();
      if (files.length) await importPaths(files);
    } else {
      inputRef.current?.click();
    }
  };

  useEffect(() => {
    if (isTauriRuntime()) {
      return listenTauriDragDrop({
        onEnter: () => setDragging(true),
        onLeave: () => setDragging(false),
        onDrop: (paths) => {
          setDragging(false);
          if (paths.length) void importPaths(paths);
        },
      });
    }

    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current += 1;
      setDragging(true);
    };
    const onOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setDragging(false);
      const files = e.dataTransfer?.files;
      if (files?.length) void importFiles(files);
      else toast("info", "没有读取到文件，请重新拖入音视频文件。");
    };

    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [importFiles, importPaths, toast]);

  const desktop = isTauriRuntime();

  return (
    <div className={`app-root${desktop ? " has-winbar" : ""}`}>
      {desktop ? <WindowBar /> : null}
      <Shell onImport={() => void openPicker()} />
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT}
        className="visually-hidden"
        onChange={(e) => {
          if (e.target.files?.length) void importFiles(e.target.files);
          e.target.value = "";
        }}
      />
      {dragging ? <DropOverlay /> : null}
    </div>
  );
}

function Root() {
  const { phase } = useApp();
  return (
    <>
      {phase === "boot" ? <BootScreen /> : <Workspace />}
      <Toasts />
      <ConfirmDialog />
    </>
  );
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[voicetrace] render error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="crash-screen">
          <span className="crash-icon">
            <Icon name="alertCircle" size={26} />
          </span>
          <h1>界面遇到了一点问题</h1>
          <p>{this.state.error.message}</p>
          <button type="button" className="btn primary" onClick={() => window.location.reload()}>
            重新加载界面
          </button>
          <small>任务与转录数据保存在本地，不会丢失。</small>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppProvider>
        <Root />
      </AppProvider>
    </ErrorBoundary>
  );
}
