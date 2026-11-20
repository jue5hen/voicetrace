import React from "react";

export type IconName =
  | "wave"
  | "plus"
  | "import"
  | "queue"
  | "settings"
  | "info"
  | "close"
  | "chevronDown"
  | "chevronUp"
  | "chevronLeft"
  | "chevronRight"
  | "play"
  | "pause"
  | "volume"
  | "volumeOff"
  | "expand"
  | "collapse"
  | "check"
  | "checkCircle"
  | "alert"
  | "alertCircle"
  | "undo"
  | "download"
  | "sparkle"
  | "repeat"
  | "gauge"
  | "clock"
  | "trash"
  | "retry"
  | "edit"
  | "film"
  | "audio"
  | "folder"
  | "copy"
  | "lock"
  | "cpu"
  | "search"
  | "more"
  | "keyboard"
  | "eyeOff"
  | "fileText"
  | "arrowRight"
  | "layers";

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
  strokeWidth?: number;
}

/** 全部图标为内联 SVG，随文字颜色变化，不依赖任何图标库。 */
export function Icon({ name, size = 18, className, strokeWidth = 1.7 }: IconProps) {
  const p = iconPath(name);
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {p}
    </svg>
  );
}

function iconPath(name: IconName): React.ReactNode {
  switch (name) {
    case "wave":
      return (
        <>
          <path d="M4 10v4" />
          <path d="M8 7v10" />
          <path d="M12 4v16" />
          <path d="M16 8v8" />
          <path d="M20 11v2" />
        </>
      );
    case "plus":
      return (
        <>
          <path d="M12 5v14" />
          <path d="M5 12h14" />
        </>
      );
    case "import":
      return (
        <>
          <path d="M12 3v12" />
          <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
          <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
        </>
      );
    case "queue":
      return (
        <>
          <path d="M4 6h11" />
          <path d="M4 12h11" />
          <path d="M4 18h7" />
          <path d="m17 15 3 3-3 3" />
        </>
      );
    case "settings":
      return (
        <>
          <path d="M4 7h9" />
          <path d="M17 7h3" />
          <path d="M4 17h3" />
          <path d="M11 17h9" />
          <circle cx="15" cy="7" r="2.2" />
          <circle cx="9" cy="17" r="2.2" />
        </>
      );
    case "info":
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11.2V16" />
          <path d="M12 8.2h.01" />
        </>
      );
    case "close":
      return (
        <>
          <path d="m6.5 6.5 11 11" />
          <path d="m17.5 6.5-11 11" />
        </>
      );
    case "chevronDown":
      return <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />;
    case "chevronUp":
      return <path d="m6.5 14.5 5.5-5.5 5.5 5.5" />;
    case "chevronLeft":
      return <path d="M14.5 6.5 9 12l5.5 5.5" />;
    case "chevronRight":
      return <path d="m9.5 6.5 5.5 5.5-5.5 5.5" />;
    case "play":
      return <path d="M8.4 5.6c0-.8.9-1.3 1.6-.9l8.1 5.5c.6.4.6 1.3 0 1.7l-8.1 5.5c-.7.4-1.6 0-1.6-.9V5.6Z" fill="currentColor" stroke="none" />;
    case "pause":
      return (
        <>
          <rect x="7" y="5" width="3.4" height="14" rx="1.1" fill="currentColor" stroke="none" />
          <rect x="13.6" y="5" width="3.4" height="14" rx="1.1" fill="currentColor" stroke="none" />
        </>
      );
    case "volume":
      return (
        <>
          <path d="M4 9v6h3.5L12 19V5L7.5 9H4Z" />
          <path d="M15.5 9.2a4 4 0 0 1 0 5.6" />
          <path d="M18 6.8a7.5 7.5 0 0 1 0 10.4" />
        </>
      );
    case "volumeOff":
      return (
        <>
          <path d="M4 9v6h3.5L12 19V5L7.5 9H4Z" />
          <path d="m16 9 5 5" />
          <path d="m21 9-5 5" />
        </>
      );
    case "expand":
      return (
        <>
          <path d="M9 4H4v5" />
          <path d="M15 4h5v5" />
          <path d="M9 20H4v-5" />
          <path d="M15 20h5v-5" />
        </>
      );
    case "collapse":
      return (
        <>
          <path d="M4 9h5V4" />
          <path d="M20 9h-5V4" />
          <path d="M4 15h5v5" />
          <path d="M20 15h-5v5" />
        </>
      );
    case "check":
      return <path d="m5 12.5 4.5 4.5L19 7" />;
    case "checkCircle":
      return (
        <>
          <circle cx="12" cy="12" r="8.6" />
          <path d="m8.2 12.2 2.6 2.6 5.2-5.4" />
        </>
      );
    case "alert":
      return (
        <>
          <path d="M12 4 3.3 19.2h17.4L12 4Z" />
          <path d="M12 9.5v4.2" />
          <path d="M12 16.6h.01" />
        </>
      );
    case "alertCircle":
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 8v4.6" />
          <path d="M12 15.9h.01" />
        </>
      );
    case "undo":
      return (
        <>
          <path d="M8.5 5.5 4 10l4.5 4.5" />
          <path d="M4 10h9.5a6 6 0 0 1 0 12H11" />
        </>
      );
    case "download":
      return (
        <>
          <path d="M12 4v10" />
          <path d="m8 10 4 4 4-4" />
          <path d="M5 19h14" />
        </>
      );
    case "sparkle":
      return (
        <>
          <path d="M12 3.5c.6 4.4 2.1 5.9 6.5 6.5-4.4.6-5.9 2.1-6.5 6.5-.6-4.4-2.1-5.9-6.5-6.5 4.4-.6 5.9-2.1 6.5-5.9Z" />
          <path d="M18.5 16.5c.3 1.8.9 2.4 2.7 2.7-1.8.3-2.4.9-2.7 2.7-.3-1.8-.9-2.4-2.7-2.7 1.8-.3 2.4-.9 2.7-2.7Z" />
        </>
      );
    case "repeat":
      return (
        <>
          <path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H18" />
          <path d="m15 3 3 3-3 3" />
          <path d="M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H6" />
          <path d="m9 15-3 3 3 3" />
        </>
      );
    case "gauge":
      return (
        <>
          <path d="M5 17a8 8 0 1 1 14 0" />
          <path d="m12 13 3.2-3.2" />
          <circle cx="12" cy="14" r="1.2" fill="currentColor" stroke="none" />
        </>
      );
    case "clock":
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7.5V12l3 1.8" />
        </>
      );
    case "trash":
      return (
        <>
          <path d="M4 7h16" />
          <path d="M9.5 7V5.2h5V7" />
          <path d="M6.5 7 7.4 19a1.5 1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4L17.5 7" />
          <path d="M10 11v6" />
          <path d="M14 11v6" />
        </>
      );
    case "retry":
      return (
        <>
          <path d="M20 12a8 8 0 1 1-2.7-6" />
          <path d="M20 4v5h-5" />
        </>
      );
    case "edit":
      return (
        <>
          <path d="M14.5 5.5 18.5 9.5" />
          <path d="M4.5 19.5 5.2 15 16 4.2a2 2 0 0 1 2.8 0l1 1a2 2 0 0 1 0 2.8L9 18.8l-4.5.7Z" />
        </>
      );
    case "film":
      return (
        <>
          <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
          <path d="M8 5v14" />
          <path d="M16 5v14" />
          <path d="M3.5 9h4.5" />
          <path d="M3.5 15h4.5" />
          <path d="M16 9h4.5" />
          <path d="M16 15h4.5" />
        </>
      );
    case "audio":
      return (
        <>
          <rect x="9" y="3.5" width="6" height="11" rx="3" />
          <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
          <path d="M12 18v2.5" />
          <path d="M8.5 21h7" />
        </>
      );
    case "folder":
      return (
        <>
          <path d="M3.5 7.5A2.5 2.5 0 0 1 6 5h3.2l2 2.4H18a2.5 2.5 0 0 1 2.5 2.5v7.6A2.5 2.5 0 0 1 18 20H6a2.5 2.5 0 0 1-2.5-2.5v-10Z" />
        </>
      );
    case "copy":
      return (
        <>
          <rect x="8.5" y="8.5" width="11" height="12" rx="2" />
          <path d="M15.5 8.5V6a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2H7" />
        </>
      );
    case "lock":
      return (
        <>
          <rect x="4.5" y="10" width="15" height="10.5" rx="2.5" />
          <path d="M8 10V7.5a4 4 0 0 1 8 0V10" />
          <path d="M12 14v2.6" />
        </>
      );
    case "cpu":
      return (
        <>
          <rect x="6.5" y="6.5" width="11" height="11" rx="2" />
          <rect x="10" y="10" width="4" height="4" rx="1" />
          <path d="M9 3.5v3" />
          <path d="M15 3.5v3" />
          <path d="M9 17.5v3" />
          <path d="M15 17.5v3" />
          <path d="M3.5 9h3" />
          <path d="M3.5 15h3" />
          <path d="M17.5 9h3" />
          <path d="M17.5 15h3" />
        </>
      );
    case "search":
      return (
        <>
          <circle cx="11" cy="11" r="6.5" />
          <path d="m16 16 4 4" />
        </>
      );
    case "more":
      return (
        <>
          <circle cx="5.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="18.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
        </>
      );
    case "keyboard":
      return (
        <>
          <rect x="2.5" y="6" width="19" height="12" rx="2.5" />
          <path d="M6 10h.01" />
          <path d="M9.3 10h.01" />
          <path d="M12.6 10h.01" />
          <path d="M15.9 10h.01" />
          <path d="M18 10h.01" />
          <path d="M7.5 14h9" />
        </>
      );
    case "eyeOff":
      return (
        <>
          <path d="M4 4 20 20" />
          <path d="M9.9 5.2A9.7 9.7 0 0 1 12 5c5 0 8.6 4.2 9.5 7a11 11 0 0 1-2.7 4" />
          <path d="M6.3 7.3A13.3 13.3 0 0 0 2.5 12c.9 2.8 4.5 7 9.5 7a9.6 9.6 0 0 0 4.1-.9" />
          <path d="M10 10a2.8 2.8 0 0 0 4 4" />
        </>
      );
    case "fileText":
      return (
        <>
          <path d="M13 3.5H7A2 2 0 0 0 5 5.5v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9.5l-5-6Z" />
          <path d="M13 3.5v6h6" />
          <path d="M8.5 13h7" />
          <path d="M8.5 16.5h5" />
        </>
      );
    case "arrowRight":
      return (
        <>
          <path d="M4 12h15" />
          <path d="m14 7 5 5-5 5" />
        </>
      );
    case "layers":
      return (
        <>
          <path d="m12 3.5 8.5 4.5L12 12.5 3.5 8 12 3.5Z" />
          <path d="m4.5 12 7.5 4 7.5-4" />
          <path d="m4.5 16 7.5 4 7.5-4" />
        </>
      );
    default:
      return <circle cx="12" cy="12" r="8" />;
  }
}

/** 品牌标记：三段声波 + 一个句点，象征「声音留痕」。 */
export function BrandMark({ size = 34 }: { size?: number }) {
  return (
    <span className="brand-mark" style={{ width: size, height: size, borderRadius: size * 0.3 }}>
      <svg width={size * 0.62} height={size * 0.62} viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="9" width="2.6" height="6" rx="1.3" fill="currentColor" opacity="0.55" />
        <rect x="7.6" y="5.5" width="2.6" height="13" rx="1.3" fill="currentColor" opacity="0.82" />
        <rect x="12.2" y="2.5" width="2.6" height="19" rx="1.3" fill="currentColor" />
        <rect x="16.8" y="7.5" width="2.6" height="9" rx="1.3" fill="currentColor" opacity="0.7" />
        <circle cx="21" cy="18" r="1.6" fill="currentColor" />
      </svg>
    </span>
  );
}
