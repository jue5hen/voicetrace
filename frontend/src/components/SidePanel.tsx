import type { ReactNode } from "react";
import { Icon } from "./Icon";

export type SidePanelTab = "subs" | "proof";

interface SidePanelProps {
  tab: SidePanelTab;
  onTab: (tab: SidePanelTab) => void;
  pendingCount: number;
  subCount: number;
  children: ReactNode;
}

/** 编辑器右栏：字幕工作区与校对建议共用同一块面板，顶部切换。 */
export function SidePanel({ tab, onTab, pendingCount, subCount, children }: SidePanelProps) {
  return (
    <aside className="proof side-panel" aria-label="字幕调整">
      <header className="panel-head">
        <div className="panel-tabs" role="tablist" aria-label="字幕调整模块">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "subs"}
            className={`panel-tab ${tab === "subs" ? "is-on" : ""}`}
            onClick={() => onTab("subs")}
          >
            <Icon name="fileText" size={13} />
            字幕
            <span className="count-pill">{subCount}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "proof"}
            className={`panel-tab ${tab === "proof" ? "is-on" : ""}`}
            onClick={() => onTab("proof")}
          >
            <Icon name="sparkle" size={13} />
            校对建议
            {pendingCount > 0 ? <span className="count-pill accent">{pendingCount}</span> : null}
          </button>
        </div>
      </header>
      {children}
    </aside>
  );
}
