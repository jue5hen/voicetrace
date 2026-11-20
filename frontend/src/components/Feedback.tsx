import { useEffect, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { useApp } from "../lib/store";
import { Icon } from "./Icon";

export function Toasts() {
  const { toasts, dismissToast } = useApp();

  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast tone-${t.level}`}>
          <span className="toast-icon">
            <Icon
              name={t.level === "success" ? "checkCircle" : t.level === "error" ? "alertCircle" : "info"}
              size={16}
            />
          </span>
          <span className="toast-text">{t.message}</span>
          <button
            type="button"
            className="toast-close"
            aria-label="关闭提示"
            onClick={() => dismissToast(t.id)}
          >
            <Icon name="close" size={12} />
          </button>
        </div>
      ))}
    </div>
  );
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function ConfirmDialog() {
  const { confirmState, resolveConfirm } = useApp();
  const layerRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!confirmState) return;
    const previous = document.activeElement as HTMLElement | null;
    // 默认焦点：危险操作落在「取消」，Enter 只触发默认按钮，而不是恒定确认。
    const initial = confirmState.tone === "danger" ? cancelRef.current : confirmRef.current;
    initial?.focus();
    return () => {
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [confirmState]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!confirmState) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      resolveConfirm(false);
      return;
    }
    if (e.key === "Enter") {
      // 焦点本身在按钮上时交给原生激活（Enter = 当前按钮，不是「确认」）。
      if ((e.target as HTMLElement).tagName === "BUTTON") return;
      e.preventDefault();
      (confirmState.tone === "danger" ? cancelRef.current : confirmRef.current)?.click();
      return;
    }
    if (e.key !== "Tab") return;
    // 焦点陷阱：Tab / Shift+Tab 只在对话框内的可聚焦元素间循环。
    const focusables = Array.from(
      boxRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
    ).filter((el) => el.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (!active || !boxRef.current?.contains(active)) {
      // 焦点跑到框外（例如背景按钮）也要拉回来。
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
      return;
    }
    if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  if (!confirmState) return null;
  const danger = confirmState.tone === "danger";

  return (
    <div
      ref={layerRef}
      className="modal-layer confirm-layer"
      role="presentation"
      onKeyDown={onKeyDown}
    >
      <button
        type="button"
        className="modal-backdrop"
        aria-label="取消"
        onClick={() => resolveConfirm(false)}
      />
      <div
        ref={boxRef}
        className="confirm-box"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-desc"
      >
        <span className={`confirm-icon ${danger ? "is-danger" : ""}`}>
          <Icon name={danger ? "alert" : "info"} size={19} />
        </span>
        <div className="confirm-copy">
          <h3 id="confirm-title">{confirmState.title}</h3>
          <p id="confirm-desc">{confirmState.description}</p>
        </div>
        <div className="confirm-actions">
          <button
            ref={cancelRef}
            type="button"
            className="btn ghost"
            onClick={() => resolveConfirm(false)}
          >
            取消
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`btn ${danger ? "danger" : "primary"}`}
            onClick={() => resolveConfirm(true)}
          >
            {confirmState.confirmText || "确定"}
          </button>
        </div>
      </div>
    </div>
  );
}
