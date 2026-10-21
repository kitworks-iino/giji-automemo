import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Pause, Play, RefreshCw, Volume2 } from "lucide-react";
import { PcmPlayback, type PlaybackState } from "./pcmAudio";

const initial: PlaybackState = {
  loaded: false,
  loading: true,
  playing: false,
  buffering: false,
  position: 0,
  duration: 0,
  error: "",
};
const clock = (seconds: number) => {
  const value = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(value / 3600);
  return `${hours ? `${hours}:` : ""}${String(Math.floor(value / 60) % 60).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
};

export function PcmPlayer({
  url,
  title,
  onError,
}: {
  url: string;
  title: string;
  onError: (message: string) => void;
}) {
  const playback = useRef<PcmPlayback | null>(null);
  const dragging = useRef(false);
  const errorHandler = useRef(onError);
  const [state, setState] = useState(initial);
  const [revision, setRevision] = useState(0);
  const [seeking, setSeeking] = useState<number | null>(null);
  const [volume, setVolume] = useState(1);
  errorHandler.current = onError;
  useEffect(() => {
    setState(initial);
    setSeeking(null);
    dragging.current = false;
    let lastError = "";
    let engine: PcmPlayback | null = null;
    try {
      engine = new PcmPlayback(url, (next) => {
        setState(next);
        if (next.error && next.error !== lastError)
          errorHandler.current(next.error);
        lastError = next.error;
      });
      playback.current = engine;
      void engine.load();
    } catch (error) {
      const message = (error as Error).message;
      setState({ ...initial, loading: false, error: message });
      errorHandler.current(message);
    }
    return () => {
      engine?.dispose();
      playback.current = null;
    };
  }, [url, revision]);
  useEffect(() => {
    playback.current?.setVolume(volume);
  }, [volume, revision, url]);

  const commitSeek = (seconds: number) => {
    playback.current?.seek(seconds);
    setSeeking(null);
  };
  const position = seeking ?? state.position;
  return (
    <div className="pcm-player" role="group" aria-label={`${title}の再生`}>
      <div className="pcm-controls">
        <button
          className="button secondary small pcm-play"
          disabled={!state.loaded || state.loading}
          aria-label={`${title}を${state.playing ? "一時停止" : "再生"}`}
          onClick={() => {
            if (state.playing) playback.current?.pause();
            else void playback.current?.play();
          }}
        >
          {state.loading || state.buffering ? (
            <LoaderCircle size={16} className="spin" />
          ) : state.playing ? (
            <Pause size={16} />
          ) : (
            <Play size={16} />
          )}
        </button>
        <label className="pcm-seek">
          <span className="sr-only">{title}の再生位置</span>
          <input
            type="range"
            min="0"
            max={state.duration || 1}
            step="0.1"
            value={position}
            disabled={!state.loaded}
            aria-valuetext={`${clock(position)} / ${clock(state.duration)}`}
            onPointerDown={() => {
              dragging.current = true;
            }}
            onChange={(event) => {
              const value = Number(event.target.value);
              if (dragging.current) setSeeking(value);
              else commitSeek(value);
            }}
            onPointerUp={(event) => {
              dragging.current = false;
              commitSeek(Number(event.currentTarget.value));
            }}
            onPointerCancel={() => {
              dragging.current = false;
              setSeeking(null);
            }}
            onBlur={(event) => {
              dragging.current = false;
              if (seeking !== null)
                commitSeek(Number(event.currentTarget.value));
            }}
          />
        </label>
        <span className="pcm-time">
          {clock(position)} / {clock(state.duration)}
        </span>
        <label className="pcm-volume">
          <Volume2 size={15} />
          <span className="sr-only">{title}の音量</span>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={volume}
            onChange={(event) => setVolume(Number(event.target.value))}
          />
        </label>
      </div>
      {(state.loading || state.buffering) && (
        <p className="pcm-message" role="status">
          {state.loading ? "音声の情報を確認しています…" : "音声を読み込み中…"}
        </p>
      )}
      {state.error && (
        <div className="pcm-error" role="alert">
          <p>{state.error}</p>
          <button
            className="text-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            <RefreshCw size={13} />
            再読み込み
          </button>
        </div>
      )}
    </div>
  );
}
