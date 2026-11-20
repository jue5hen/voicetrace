import {
  closeWindow,
  minimizeWindow,
  startDraggingWindow,
  toggleMaximizeWindow,
} from "../lib/tauri";

/**
 * 自绘窗口标题栏（仅 Tauri 桌面端渲染）。
 * 左半区作为拖动区，右侧是最小化 / 最大化 / 关闭。
 */
export function WindowBar() {
  return (
    <div className="winbar">
      <div
        className="winbar-drag"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          void startDraggingWindow();
        }}
        onDoubleClick={() => void toggleMaximizeWindow()}
      />
      <div className="winbar-actions">
        <button
          type="button"
          className="winbar-btn"
          aria-label="最小化"
          title="最小化"
          onClick={() => void minimizeWindow()}
        >
          <span className="glyph-minus" aria-hidden="true" />
        </button>
        <button
          type="button"
          className="winbar-btn"
          aria-label="最大化 / 还原"
          title="最大化 / 还原"
          onClick={() => void toggleMaximizeWindow()}
        >
          <span className="glyph-max" aria-hidden="true" />
        </button>
        <button
          type="button"
          className="winbar-btn winbar-close"
          aria-label="关闭"
          title="关闭"
          onClick={() => void closeWindow()}
        >
          <span className="glyph-close" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
