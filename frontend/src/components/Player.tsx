import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Job } from "../types";
import { mediaUrl, thumbUrl } from "../lib/engine";
import { clamp, fmtClock } from "../lib/format";
import { Icon } from "./Icon";

export interface PlayerHandle {
  toggle: () => void;
  play: () => void;
  pause: () => void;
  seek: (ms: number) => void;
}

interface PlayerProps {
  job: Job;
  durationMs: number;
  onTime: (ms: number) => void;
  onPlayingChange: (playing: boolean) => void;
  subtitle?: string[];
}

const RATES = [0.75, 1, 1.25, 1.5, 2];
const VOLUME_KEY = "voicetrace.volume";
const MUTED_KEY = "voicetrace.muted";

function readStoredVolume(): number {
  try {
    const raw = localStorage.getItem(VOLUME_KEY);
    const v = raw == null ? NaN : Number(raw);
    return Number.isFinite(v) ? clamp(v, 0, 1) : 0.8;
  } catch {
    return 0.8;
  }
}

function readStoredMuted(): boolean {
  try {
    return localStorage.getItem(MUTED_KEY) === "1";
  } catch {
    return false;
  }
}

export const Player = forwardRef<PlayerHandle, PlayerProps>(function Player(
  { job, durationMs, onTime, onPlayingChange, subtitle },
  ref,
) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const hideTimer = useRef(0);
  const pendingSeek = useRef<number | null>(null);

  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(durationMs);
  const [volume, setVolume] = useState(readStoredVolume);
  const [muted, setMuted] = useState(readStoredMuted);
  const [rate, setRate] = useState(1);
  const [ready, setReady] = useState(false);
  const [mediaError, setMediaError] = useState(false);
  const [controls, setControls] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    if (!duration && durationMs) setDuration(durationMs);
  }, [duration, durationMs]);

  useEffect(() => {
    const onFs = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  const bumpControls = useCallback(() => {
    setControls(true);
    window.clearTimeout(hideTimer.current);
    const video = videoRef.current;
    if (video && !video.paused) {
      hideTimer.current = window.setTimeout(() => setControls(false), 2400);
    }
  }, []);

  useEffect(() => () => window.clearTimeout(hideTimer.current), []);

  const seek = useCallback(
    (ms: number) => {
      const video = videoRef.current;
      const total = duration || durationMs;
      const next = clamp(ms, 0, Math.max(0, total - 20));
      setCurrent(next);
      onTime(next);
      if (!video || video.readyState < 1) {
        pendingSeek.current = next;
        return;
      }
      video.currentTime = next / 1000;
    },
    [duration, durationMs, onTime],
  );

  const play = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    void video.play().catch(() => setMediaError(false));
  }, []);

  const pause = useCallback(() => {
    videoRef.current?.pause();
  }, []);

  const toggle = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) play();
    else pause();
  }, [play, pause]);

  useImperativeHandle(ref, () => ({ toggle, play, pause, seek }), [
    toggle,
    play,
    pause,
    seek,
  ]);

  const isAudio = !job.width || !job.height;
  const bars = useMemo(() => {
    let seed = 7;
    for (const ch of job.id) seed = (seed * 31 + ch.charCodeAt(0)) % 9973;
    return Array.from({ length: 56 }, (_, i) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      const base = 18 + (seed / 2147483648) * 62;
      const envelope = Math.sin((i / 55) * Math.PI) * 18;
      return Math.round(base + envelope);
    });
  }, [job.id]);

  const onLoaded = () => {
    const video = videoRef.current;
    setReady(true);
    setMediaError(false);
    if (video) {
      const d = Number.isFinite(video.duration) ? video.duration * 1000 : durationMs;
      setDuration(d || durationMs);
      video.playbackRate = rate;
      video.volume = volume;
      video.muted = muted || volume === 0;
      if (pendingSeek.current != null) {
        video.currentTime = pendingSeek.current / 1000;
        pendingSeek.current = null;
      }
    }
    bumpControls();
  };

  const rememberVolume = (v: number, m: boolean) => {
    try {
      localStorage.setItem(VOLUME_KEY, String(v));
      localStorage.setItem(MUTED_KEY, m ? "1" : "0");
    } catch {
      // 隐私模式等场景下无法写入，忽略即可。
    }
  };

  const toggleMute = () => {
    const video = videoRef.current;
    if (muted) {
      const restored = volume > 0 ? volume : 0.8;
      setVolume(restored);
      setMuted(false);
      rememberVolume(restored, false);
      if (video) {
        video.volume = restored;
        video.muted = false;
      }
      return;
    }
    setMuted(true);
    rememberVolume(volume, true);
    if (video) video.muted = true;
  };

  const changeVolume = (v: number) => {
    const next = clamp(v, 0, 1);
    const nextMuted = next === 0;
    setVolume(next);
    setMuted(nextMuted);
    rememberVolume(next, nextMuted);
    const video = videoRef.current;
    if (video) {
      video.volume = next;
      video.muted = nextMuted;
    }
  };

  const cycleRate = () => {
    const idx = RATES.indexOf(rate);
    const next = RATES[(idx + 1) % RATES.length];
    setRate(next);
    if (videoRef.current) videoRef.current.playbackRate = next;
  };

  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (stageRef.current) {
        await stageRef.current.requestFullscreen();
      }
    } catch {
      // 某些 WebView 不支持全屏，保持内嵌播放即可。
    }
  };

  const progress = duration > 0 ? (current / duration) * 100 : 0;

  return (
    <div className="player">
      <div
        ref={stageRef}
        className={`player-stage ${controls ? "show-controls" : ""} ${isAudio ? "is-audio" : ""}`}
        onMouseMove={bumpControls}
        onMouseLeave={() => {
          if (playing) setControls(false);
        }}
      >
        <video
          ref={videoRef}
          className="player-video"
          src={mediaUrl(job.id)}
          poster={isAudio ? undefined : thumbUrl(job)}
          preload="metadata"
          playsInline
          onClick={toggle}
          onLoadedMetadata={onLoaded}
          onTimeUpdate={(e) => {
            const ms = e.currentTarget.currentTime * 1000;
            setCurrent(ms);
            onTime(ms);
          }}
          onPlay={() => {
            setPlaying(true);
            onPlayingChange(true);
            bumpControls();
          }}
          onPause={() => {
            setPlaying(false);
            onPlayingChange(false);
            setControls(true);
            window.clearTimeout(hideTimer.current);
          }}
          onEnded={() => {
            setPlaying(false);
            onPlayingChange(false);
            setControls(true);
          }}
          onError={() => setMediaError(true)}
        />

        {isAudio ? (
          <div className={`player-audio ${playing ? "is-playing" : ""}`} onClick={toggle}>
            <div className="audio-orbit" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <div className="audio-wave" aria-hidden="true">
              {bars.map((h, i) => (
                <i key={i} style={{ height: `${h}%`, animationDelay: `${(i % 11) * 48}ms` }} />
              ))}
            </div>
            <div className="audio-meta">
              <span className="audio-icon"><Icon name="audio" size={15} /></span>
              <strong>{job.name}</strong>
              <span className="faint tabular">{fmtClock(duration, false)} · 音频</span>
            </div>
          </div>
        ) : null}

        {!playing && !mediaError ? (
          <button type="button" className="player-bigplay" onClick={toggle} aria-label="播放">
            <Icon name="play" size={26} />
          </button>
        ) : null}

        {mediaError ? (
          <div className="player-error">
            <Icon name="alertCircle" size={20} />
            <div>
              <strong>无法预览媒体</strong>
              <p>浏览器可能不支持该编码，字幕编辑与导出不受影响。</p>
            </div>
          </div>
        ) : null}

        {!ready && !mediaError ? <div className="player-loading">正在载入媒体…</div> : null}

        {subtitle && subtitle.length > 0 ? (
          <div className="player-subtitle" aria-live="off">
            {subtitle.map((line, index) => (
              <span key={`${index}-${line}`}>{line}</span>
            ))}
          </div>
        ) : null}

        <div className="player-top">
          <span className="player-badge">
            <Icon name={isAudio ? "audio" : "film"} size={13} /> {isAudio ? "音频预览" : "媒体预览"}
          </span>
          {job.width && job.height ? (
            <span className="player-res">
              {job.width} × {job.height}
            </span>
          ) : null}
        </div>

        <div className="player-controls">
          <input
            className="player-seek"
            type="range"
            min={0}
            max={Math.max(1, duration)}
            step={50}
            value={Math.min(current, duration || 1)}
            aria-label="播放进度"
            style={{ ["--pct" as string]: `${progress}%` }}
            onChange={(e) => seek(Number(e.target.value))}
            onMouseDown={() => window.clearTimeout(hideTimer.current)}
          />
          <div className="player-row">
            <button
              type="button"
              className="pbtn"
              onClick={toggle}
              aria-label={playing ? "暂停" : "播放"}
            >
              <Icon name={playing ? "pause" : "play"} size={19} />
            </button>
            <span className="player-time tabular">
              {fmtClock(current, true)}
              <span className="faint"> / {fmtClock(duration, true)}</span>
            </span>
            <div className="player-spacer" />
            <button
              type="button"
              className="pbtn rate-btn tabular"
              onClick={cycleRate}
              data-tip="播放速度"
            >
              {rate.toFixed(rate === 1 ? 1 : 2).replace(/0$/, "")}×
            </button>
            <div className="volume-wrap">
              <button
                type="button"
                className="pbtn"
                onClick={toggleMute}
                aria-label={muted ? "取消静音" : "静音"}
              >
                <Icon name={muted || volume === 0 ? "volumeOff" : "volume"} size={18} />
              </button>
              <input
                className="volume-range"
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={muted ? 0 : volume}
                aria-label="音量"
                onChange={(e) => changeVolume(Number(e.target.value))}
              />
            </div>
            <button
              type="button"
              className="pbtn"
              onClick={() => void toggleFullscreen()}
              aria-label={fullscreen ? "退出全屏" : "全屏"}
              data-tip={fullscreen ? "退出全屏" : "全屏"}
            >
              <Icon name="expand" size={17} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});
