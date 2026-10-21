import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  AudioLines,
  CalendarDays,
  Check,
  CheckCheck,
  ChevronRight,
  CircleHelp,
  Clock3,
  Cloud,
  ExternalLink,
  FileText,
  FolderOpen,
  Headphones,
  LayoutDashboard,
  LoaderCircle,
  Mic,
  Monitor,
  MoreHorizontal,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Users,
  Waves,
  X,
} from "lucide-react";
import {
  getBlob,
  post,
  request,
  type Meeting,
  type MicrosoftConnection,
  type Session,
  type Settings,
  type Snapshot,
  type TeamsConnection,
} from "./api";

const statusNames: Record<string, string> = {
  recording: "録音中",
  transcribing: "文字起こし中",
  diarizing: "話者を分離中",
  summarizing: "議事録を作成中",
  ready: "作成完了",
  error: "確認が必要",
  interrupted: "録音が中断されました",
};
const activeStatuses = [
  "recording",
  "transcribing",
  "diarizing",
  "summarizing",
];
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
const date = (iso: string) =>
  new Date(iso).toLocaleDateString("ja-JP", {
    month: "long",
    day: "numeric",
    weekday: "short",
  });
const duration = (seconds = 0) =>
  `${Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0")}`;
const initials = (name: string) => name.replace(/\s/g, "").slice(0, 1);
const speakerLabel = (id: string) =>
  id === "SELF"
    ? "自分"
    : id === "SPEAKER_UNKNOWN"
      ? "話者不明"
      : /^SPEAKER_\d+$/.test(id)
        ? `話者 ${Number(id.split("_")[1]) + 1}`
        : id;
const cx = (...items: (string | false | undefined)[]) =>
  items.filter(Boolean).join(" ");

function calendarConnection(snapshot: Snapshot | null): MicrosoftConnection {
  const provider = snapshot?.settings.calendar_source ?? "teams_desktop";
  if (snapshot?.microsoft) return snapshot.microsoft;
  const connected =
    provider === "graph"
      ? !!snapshot?.settings.has_microsoft_login
      : snapshot?.health.calendar.state === "connected";
  return {
    provider,
    connected,
    state: connected ? "connected" : "disconnected",
    detail:
      snapshot?.health.calendar.detail ??
      "Teamsで一度、本日のカレンダーを日表示で開いてください。予定表を保持していれば、最小化・バックグラウンドでも読み取ります。",
  };
}

const teamsStateNames: Record<TeamsConnection["state"], string> = {
  needs_setup: "未設定",
  waiting_for_restart: "Teamsの再起動待ち",
  connected: "接続済み",
  blocked: "操作を確認",
  unavailable: "利用できません",
  disconnected: "未接続",
};

function teamsConnection(snapshot: Snapshot | null): TeamsConnection {
  return (
    snapshot?.teams_connection ?? {
      provider: "teams_desktop",
      state: "needs_setup",
      connected: false,
      detail:
        "Teamsの連携設定を有効にしてから、会議中でないときにTeamsを完全終了し、もう一度起動してください。",
      needs_restart: false,
      policy_restored: true,
      debug_active: false,
      expires_at: null,
    }
  );
}

function CalendarReadStatus({ snapshot }: { snapshot: Snapshot }) {
  const source = calendarConnection(snapshot);
  const calendar = snapshot.health.calendar;
  const teams = source.provider === "teams_desktop";
  const reading = calendar.state === "syncing";
  const failed =
    calendar.last_read_succeeded === false ||
    !["connected", "syncing"].includes(calendar.state);
  const retained = snapshot.meetings.some(
    (meeting) => !meeting.cancelled && !meeting.id.startsWith("demo-"),
  );
  const cached = retained && failed;
  const timestamp = (value: string) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? "時刻を確認できません"
      : parsed.toLocaleString("ja-JP", {
          year: "numeric",
          month: "numeric",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        });
  };
  return (
    <div
      className={cx("calendar-read-status", cached && "cached")}
      role="status"
    >
      <div className="calendar-read-heading">
        {reading ? (
          <LoaderCircle size={14} className="spin" />
        ) : cached ? (
          <CircleHelp size={14} />
        ) : (
          <Clock3 size={14} />
        )}
        <strong>
          {reading
            ? "予定を読み取り中"
            : cached
              ? "前回取得した予定を表示しています"
              : failed
                ? "予定の読み取りを確認できません"
                : "予定を読み取りました"}
        </strong>
      </div>
      {cached && (
        <p className="calendar-cache-warning">
          直近の読み取りを確認できないため、予定の変更・取消が反映されていない可能性があります。
        </p>
      )}
      <p>{calendar.detail || source.detail}</p>
      <dl className="calendar-read-times">
        <div>
          <dt>{teams ? "最終読み取り（PC内）" : "最終取得"}</dt>
          <dd>
            {calendar.last_sync ? timestamp(calendar.last_sync) : "未取得"}
          </dd>
        </div>
        {failed && calendar.last_attempt && (
          <div>
            <dt>最終試行</dt>
            <dd>{timestamp(calendar.last_attempt)}</dd>
          </div>
        )}
      </dl>
      {teams && (
        <p className="calendar-freshness-note">
          この時刻はPC内のTeamsから読み取れた時刻です。Microsoft側の最新予定との同期を確認するものではありません。
        </p>
      )}
      {[...new Set(source.warnings ?? [])].map((warning) => (
        <p className="calendar-source-warning" key={warning}>
          {warning}
        </p>
      ))}
    </div>
  );
}

function Status({ status }: { status: string }) {
  return (
    <span className={cx("status", status)}>
      <span className="status-dot" />
      {statusNames[status] ?? status}
    </span>
  );
}
function Empty({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  );
}
function Toggle({
  checked,
  onChange,
  label,
  detail,
  disabled = false,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  detail?: string;
  disabled?: boolean;
}) {
  return (
    <label className={cx("toggle-row", disabled && "disabled")}>
      <span>
        <strong>{label}</strong>
        {detail && <small>{detail}</small>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="toggle" />
    </label>
  );
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [page, setPage] = useState<"home" | "library" | "settings">("home");
  const [selected, setSelected] = useState<string | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [offline, setOffline] = useState(false);
  const [busy, setBusy] = useState("");
  const [showStart, setShowStart] = useState(false);
  const [recordTitle, setRecordTitle] = useState("");
  const [recordMeeting, setRecordMeeting] = useState("");
  const [detailTab, setDetailTab] = useState("minutes");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [speakerEdit, setSpeakerEdit] = useState(false);
  const [speakerNames, setSpeakerNames] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await request<Snapshot>("/snapshot"));
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!selected) {
      setSession(null);
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const data = await request<Session>(
          `/sessions/${encodeURIComponent(selected!)}`,
        );
        if (!cancelled) setSession(data);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    }
    void load();
    const timer = setInterval(() => void load(), 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [selected]);
  useEffect(() => {
    if (toast) {
      const t = setTimeout(() => setToast(""), 5000);
      return () => clearTimeout(t);
    }
  }, [toast]);

  async function act<T>(
    key: string,
    fn: () => Promise<T>,
    message?: string,
  ): Promise<T | undefined> {
    setBusy(key);
    setError("");
    try {
      const result = await fn();
      await refresh();
      if (message) setToast(message);
      return result;
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    } finally {
      setBusy("");
    }
  }
  function navigate(next: typeof page) {
    setPage(next);
    setSelected(null);
    setSearch("");
  }
  function openSession(id: string) {
    setSelected(id);
    setSession(null);
    setDetailTab("minutes");
    setDeleteConfirm(false);
  }
  function openRecordingDialog() {
    const armed = snapshot?.armed_meeting_id;
    const available = snapshot?.meetings.some(
      (meeting) =>
        meeting.id === armed &&
        !meeting.cancelled &&
        !meeting.id.startsWith("demo-"),
    );
    setRecordMeeting(available && armed ? armed : "");
    setRecordTitle("");
    setShowStart(true);
  }
  async function startRecording() {
    const result = await act<Session>("start", () =>
      post("/recordings/start", {
        ...(recordMeeting ? { meeting_id: recordMeeting } : {}),
        ...(recordTitle ? { title: recordTitle } : {}),
      }),
    );
    if (result) {
      setShowStart(false);
      setRecordTitle("");
      setRecordMeeting("");
      openSession(result.id);
    }
  }
  async function join(meeting: Meeting) {
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    const result = await act<{ join_url: string }>(`join-${meeting.id}`, () =>
      post(`/meetings/${encodeURIComponent(meeting.id)}/join`),
    );
    if (result) {
      const url = new URL(result.join_url);
      if (url.protocol === "https:") {
        if (popup) popup.location.href = url.href;
        else {
          setError(
            "ブラウザーが新しいウィンドウをブロックしました。ポップアップを許可して、もう一度参加してください。",
          );
        }
      } else popup?.close();
    } else popup?.close();
  }
  async function armMeeting(meeting: Meeting) {
    await act(
      `arm-${meeting.id}`,
      () => post(`/meetings/${encodeURIComponent(meeting.id)}/arm`),
      "録音対象に選択しました。Teamsでこの会議に参加してください。自動録音を使わない場合は「録音を開始」を押してください。",
    );
  }
  async function exportSession() {
    if (!session) return;
    await act(
      "export",
      async () => {
        const blob = await getBlob(
          `/sessions/${encodeURIComponent(session.id)}/export`,
        );
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${session.title.replace(/[<>:"/\\|?*]/g, "_")}.md`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
      "Markdownを書き出しました",
    );
  }
  const meetings = (snapshot?.meetings ?? []).filter((m) => !m.cancelled);
  const sessions = snapshot?.sessions ?? [];
  const recording = snapshot?.health.recording;
  const completed = sessions.filter((s) => s.status === "ready");
  const pending = sessions.filter(
    (s) => activeStatuses.includes(s.status) && s.status !== "recording",
  );
  const today = snapshot?.now ?? new Date().toISOString();
  const todaySessions = sessions.filter(
    (s) =>
      new Date(s.started_at).toDateString() === new Date(today).toDateString(),
  );
  const microsoft = calendarConnection(snapshot);
  const desktopCalendar = microsoft.provider === "outlook_desktop";
  const teamsCalendar = microsoft.provider === "teams_desktop";
  const teamsUiaCalendar =
    teamsCalendar &&
    (snapshot?.settings.teams_calendar_mode ?? "uia") === "uia";
  const partialCalendar = teamsCalendar;
  const isConfigured =
    microsoft.connected &&
    snapshot?.settings.has_api_key &&
    snapshot?.health.models.ready &&
    snapshot?.settings.recording_consent &&
    snapshot?.settings.cloud_consent &&
    snapshot?.settings.auto_record;
  const filteredSessions = sessions.filter((s) =>
    s.title.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          className="brand"
          onClick={() => navigate("home")}
          aria-label="giji ホーム"
        >
          <span className="brand-icon">
            <AudioLines size={23} />
          </span>
          giji<span className="brand-period">.</span>
        </button>
        <div className="workspace-label">PERSONAL WORKSPACE</div>
        <nav aria-label="メインナビゲーション">
          <button
            className={cx("nav-item", page === "home" && !selected && "active")}
            onClick={() => navigate("home")}
          >
            <LayoutDashboard size={19} />
            今日の会議<span className="nav-count">{meetings.length}</span>
          </button>
          <button
            className={cx(
              "nav-item",
              (page === "library" || !!selected) && "active",
            )}
            onClick={() => navigate("library")}
          >
            <FolderOpen size={19} />
            議事録ライブラリ
          </button>
          <button
            className={cx(
              "nav-item",
              page === "settings" && !selected && "active",
            )}
            onClick={() => navigate("settings")}
          >
            <Settings2 size={19} />
            設定・連携
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="local-card">
            <ShieldCheck size={22} />
            <strong>あなたのPCが、保存先。</strong>
            <p>
              録音と文字起こしをローカルで。
              <br />
              会議の記録を、手元に。
            </p>
            <span>
              <span className="green-dot" />
              LOCAL FIRST
            </span>
          </div>
          <a
            className="help-link"
            href="https://github.com/kitworks-iino/giji-automemo#readme"
            target="_blank"
            rel="noreferrer"
          >
            <CircleHelp size={17} />
            使い方とセットアップ
            <ExternalLink size={13} />
          </a>
          <div className="profile">
            <div className="avatar user-avatar">Y</div>
            <div>
              <strong>My workspace</strong>
              <small>Windows · ローカル保存</small>
            </div>
            <MoreHorizontal size={17} />
          </div>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <span>My workspace</span>
            <ChevronRight size={14} />
            <strong>
              {selected
                ? "会議の記録"
                : page === "home"
                  ? "今日の会議"
                  : page === "library"
                    ? "議事録ライブラリ"
                    : "設定・連携"}
            </strong>
          </div>
          <div className="topbar-actions">
            <span className={cx("connection", offline && "offline")}>
              <span className="green-dot" />
              {offline ? "接続を確認中" : "ローカル接続"}
            </span>
            <span className="topbar-divider" />
            <button
              className="icon-button"
              title="最新の状態に更新"
              onClick={() => void refresh()}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </header>
        <main>
          {offline && (
            <div className="notice error-notice">
              <Monitor size={18} />
              <div>
                <strong>アプリに接続できません</strong>
                <p>
                  giji
                  を起動し、アプリが開いた画面を使用してください。接続すると自動で復帰します。
                </p>
              </div>
            </div>
          )}
          {error && (
            <div className="notice error-notice" role="alert">
              <CircleHelp size={18} />
              <span>{error}</span>
              <button
                className="icon-button"
                onClick={() => setError("")}
                aria-label="エラーを閉じる"
              >
                <X size={16} />
              </button>
            </div>
          )}

          {!!recording && (selected || page !== "home") && (
            <div className="global-recording">
              <span className="live-dot" />
              <div>
                <strong>録音中</strong>
                <span>{duration(recording.seconds)}</span>
              </div>
              <button
                className="button stop-button"
                disabled={!!busy}
                onClick={() =>
                  void act(
                    "stop",
                    () => post("/recordings/stop"),
                    "録音を保存し、処理を開始しました",
                  )
                }
              >
                <Square size={12} fill="currentColor" />
                録音を終了
              </button>
            </div>
          )}
          {snapshot &&
            (snapshot.health.warnings.length > 0 ||
              (recording &&
                ["unknown", "unavailable"].includes(
                  snapshot.health.teams.state,
                ))) && (
              <div className="notice warning-notice runtime-warnings">
                <CircleHelp size={17} />
                <div>
                  {snapshot.health.warnings.map((warning, i) => (
                    <p key={i}>{warning}</p>
                  ))}
                  {recording &&
                    ["unknown", "unavailable"].includes(
                      snapshot.health.teams.state,
                    ) && (
                      <p>
                        Teamsの状態を確認できません。録音は継続しています。会議が終了したら「録音を終了」を押してください。
                      </p>
                    )}
                </div>
              </div>
            )}
          {snapshot?.health.calendar.state === "error" && (
            <div className="notice warning-notice">
              <CalendarDays size={17} />
              <span>{snapshot.health.calendar.detail}</span>
            </div>
          )}
          {selected ? (
            <>
              <button className="back-link" onClick={() => setSelected(null)}>
                <ArrowLeft size={16} />
                一覧に戻る
              </button>
              {!session ? (
                <div className="loading-state">
                  <LoaderCircle className="spin" />
                  会議の記録を読み込んでいます
                </div>
              ) : (
                <>
                  <div className="page-heading detail-heading">
                    <div>
                      <div className="eyebrow">MEETING NOTES</div>
                      <h1>{session.title}</h1>
                      <div className="detail-meta">
                        <span>
                          <CalendarDays size={15} />
                          {date(session.started_at)}
                        </span>
                        <span>
                          <Clock3 size={15} />
                          {time(session.started_at)} ·{" "}
                          {Math.round(session.duration_seconds / 60)} 分
                        </span>
                        <Status status={session.status} />
                      </div>
                    </div>
                    <button
                      className="button secondary"
                      disabled={!!busy}
                      onClick={() => void exportSession()}
                    >
                      <ArrowDownToLine size={16} />
                      書き出し
                    </button>
                  </div>
                  {session.error && (
                    <div className="notice warning-notice">
                      <CircleHelp size={18} />
                      <div>
                        <strong>処理の確認が必要です</strong>
                        <p>{session.error}</p>
                      </div>
                      <button
                        className="button secondary small"
                        disabled={!!busy}
                        onClick={() =>
                          void act(
                            "retry",
                            () => post(`/sessions/${session.id}/retry`),
                            "再処理を開始しました",
                          )
                        }
                      >
                        再試行
                      </button>
                    </div>
                  )}
                  <div
                    className="detail-tabs"
                    role="tablist"
                    aria-label="会議記録の表示"
                  >
                    {[
                      ["minutes", "議事録", <Sparkles size={17} />],
                      ["transcript", "文字起こし", <FileText size={17} />],
                      ["audio", "録音データ", <Headphones size={17} />],
                    ].map(([id, name, icon]) => (
                      <button
                        key={id as string}
                        role="tab"
                        aria-selected={detailTab === id}
                        className={detailTab === id ? "active" : ""}
                        onClick={() => setDetailTab(id as string)}
                      >
                        {icon}
                        {name}
                      </button>
                    ))}
                  </div>
                  {detailTab === "minutes" &&
                    (session.minutes ? (
                      <div className="minutes-layout">
                        <div className="minutes-main">
                          <section className="summary-card">
                            <div className="section-label">
                              <Sparkles size={17} />
                              会議のサマリー
                            </div>
                            <p>{session.minutes.summary}</p>
                          </section>
                          <section className="content-card">
                            <h2>
                              <CheckCheck size={21} />
                              決定事項
                            </h2>
                            {session.minutes.decisions.length ? (
                              <ul className="decision-list">
                                {session.minutes.decisions.map((d, i) => (
                                  <li key={i}>
                                    <span>
                                      <Check size={14} />
                                    </span>
                                    {d}
                                  </li>
                                ))}
                              </ul>
                            ) : (
                              <p className="muted">
                                記録された決定事項はありません。
                              </p>
                            )}
                          </section>
                          {session.minutes.topics.map((topic, i) => (
                            <section className="content-card" key={i}>
                              <h2>
                                <span className="topic-number">
                                  {String(i + 1).padStart(2, "0")}
                                </span>
                                {topic.title}
                              </h2>
                              <ul className="plain-list">
                                {topic.notes.map((note, j) => (
                                  <li key={j}>{note}</li>
                                ))}
                              </ul>
                            </section>
                          ))}
                          <p className="ai-note">
                            <Sparkles size={13} />
                            AIが作成した議事録です。共有前に内容をご確認ください。
                          </p>
                        </div>
                        <div className="minutes-aside">
                          <section className="content-card">
                            <h2>
                              <Square size={18} />
                              次のアクション
                              <span className="count-pill">
                                {session.minutes.action_items.length}
                              </span>
                            </h2>
                            {session.minutes.action_items.length ? (
                              session.minutes.action_items.map((item, i) => (
                                <div className="action-item" key={i}>
                                  <span className="action-checkbox" />
                                  <div>
                                    <p>{item.task}</p>
                                    <div>
                                      <span className="owner-chip">
                                        {item.owner || "担当未定"}
                                      </span>
                                      {item.due_date && (
                                        <small>
                                          <Clock3 size={12} />
                                          {item.due_date}
                                        </small>
                                      )}
                                    </div>
                                  </div>
                                </div>
                              ))
                            ) : (
                              <p className="muted">
                                記録されたアクションはありません。
                              </p>
                            )}
                          </section>
                          <section className="content-card">
                            <h2>
                              <CircleHelp size={18} />
                              確認したいこと
                            </h2>
                            {session.minutes.open_questions.length ? (
                              <ul className="plain-list">
                                {session.minutes.open_questions.map((q, i) => (
                                  <li key={i}>{q}</li>
                                ))}
                              </ul>
                            ) : (
                              <p className="muted">
                                未解決の質問はありません。
                              </p>
                            )}
                          </section>
                        </div>
                      </div>
                    ) : (
                      <div className="content-card">
                        <Empty
                          icon={
                            activeStatuses.includes(session.status) ? (
                              <LoaderCircle className="spin" />
                            ) : (
                              <Sparkles />
                            )
                          }
                          title={
                            activeStatuses.includes(session.status)
                              ? statusNames[session.status]
                              : "議事録はまだありません"
                          }
                        >
                          <p>
                            {session.status === "recording"
                              ? "会議が終了すると、文字起こしと議事録作成が始まります。"
                              : activeStatuses.includes(session.status)
                                ? "この画面は自動で更新されます。別の会議も確認できます。"
                                : "設定のAPIキーと送信への同意を確認してから再試行できます。"}
                          </p>
                          {!activeStatuses.includes(session.status) && (
                            <button
                              className="button secondary"
                              onClick={() =>
                                void act("retry", () =>
                                  post(`/sessions/${session.id}/retry`),
                                )
                              }
                              disabled={!!busy}
                            >
                              <RefreshCw size={15} />
                              処理を再試行
                            </button>
                          )}
                        </Empty>
                      </div>
                    ))}
                  {detailTab === "transcript" && (
                    <section className="content-card transcript-card">
                      <div className="section-heading">
                        <h2>
                          会話の記録
                          <span className="count-pill">
                            {session.segments?.length ?? 0}
                          </span>
                        </h2>
                        <button
                          className="button secondary small"
                          disabled={!session.segments?.length}
                          onClick={() => {
                            setSpeakerNames(session.speaker_names ?? {});
                            setSpeakerEdit(true);
                          }}
                        >
                          <Users size={15} />
                          話者名を編集
                        </button>
                      </div>
                      <p className="subtle-note">
                        話者は音声から分離しています。実名は「話者名を編集」で対応付けできます。
                      </p>
                      {session.segments?.length ? (
                        session.segments.map((seg, i) => (
                          <div className="transcript-line" key={i}>
                            <span className="transcript-time">
                              {duration(seg.start)}
                            </span>
                            <div
                              className={cx(
                                "avatar",
                                seg.speaker === "SELF" ? "self" : "remote",
                              )}
                            >
                              {initials(
                                session.speaker_names?.[seg.speaker] ||
                                  speakerLabel(seg.speaker),
                              )}
                            </div>
                            <div>
                              <strong>
                                {session.speaker_names?.[seg.speaker] ||
                                  speakerLabel(seg.speaker)}
                              </strong>
                              <p>{seg.text}</p>
                            </div>
                          </div>
                        ))
                      ) : (
                        <Empty
                          icon={<FileText />}
                          title="文字起こしはまだありません"
                        >
                          <p>録音終了後、PC内の音声モデルで処理します。</p>
                        </Empty>
                      )}
                    </section>
                  )}
                  {detailTab === "audio" && (
                    <section className="content-card">
                      <div className="section-heading">
                        <h2>2つの音声トラック</h2>
                        <span className="local-label">
                          <ShieldCheck size={14} />
                          PC内に保存
                        </span>
                      </div>
                      <p className="subtle-note">
                        マイク音声と出力音声を分けて保存しています。出力トラックには同じデバイスの他アプリの音も含まれます。
                      </p>
                      {session.status === "recording" ? (
                        <Empty icon={<Radio />} title="録音中です">
                          <p>録音を終了すると、ここで再生できます。</p>
                        </Empty>
                      ) : (
                        <>
                          <AudioTrack
                            sessionId={session.id}
                            track="microphone"
                            audioUrl={session.audio_urls?.microphone}
                            title="自分の声"
                            subtitle="マイク・ヘッドセット"
                            onError={setError}
                          />
                          <AudioTrack
                            sessionId={session.id}
                            track="system"
                            audioUrl={session.audio_urls?.system}
                            title="相手の声"
                            subtitle="Teamsで使用している出力デバイス"
                            onError={setError}
                          />
                        </>
                      )}
                    </section>
                  )}
                  {session.status === "ready" &&
                    !session.id.startsWith("demo-") && (
                      <div className="regenerate-area">
                        <p>
                          話者名やAIの設定を変更した後は、議事録を作り直せます。
                        </p>
                        <button
                          className="button secondary small"
                          disabled={!!busy}
                          onClick={() =>
                            void act(
                              "retry",
                              () => post(`/sessions/${session.id}/retry`),
                              "議事録の再生成を開始しました",
                            )
                          }
                        >
                          <RefreshCw size={14} />
                          議事録を再生成
                        </button>
                      </div>
                    )}
                  {!activeStatuses.includes(session.status) && (
                    <div className="delete-area">
                      <button
                        className="text-button danger"
                        onClick={() => setDeleteConfirm(true)}
                      >
                        <Trash2 size={15} />
                        この会議の記録を削除
                      </button>
                    </div>
                  )}
                </>
              )}
            </>
          ) : page === "settings" ? (
            <SettingsPage snapshot={snapshot} act={act} busy={busy} />
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">
                    {page === "home"
                      ? "YOUR DAY, IN FOCUS"
                      : "YOUR CONVERSATIONS, ORGANIZED"}
                  </div>
                  <h1>
                    {page === "home" ? "今日の会議" : "議事録ライブラリ"}
                    <span className="heading-dot">.</span>
                  </h1>
                  <p>
                    {page === "home"
                      ? "会話に集中。記録は、giji. に。"
                      : "話したことも、決めたことも。いつでも、ここから。"}
                  </p>
                </div>
                <button
                  className="button primary"
                  onClick={openRecordingDialog}
                  disabled={!!recording || offline}
                >
                  <Plus size={18} />
                  録音を開始
                </button>
              </div>
              {page === "home" ? (
                <>
                  <div className="overview-row">
                    <div className="date-pill">
                      <CalendarDays size={17} />
                      {date(today)}
                      <span>TODAY</span>
                    </div>
                    <div className="overview-stats">
                      <span>
                        <strong>{meetings.length}</strong>
                        {partialCalendar ? "件を取得" : "件の会議"}
                      </span>
                      <span>
                        <strong>
                          {
                            todaySessions.filter((s) => s.status === "ready")
                              .length
                          }
                        </strong>
                        件の議事録
                      </span>
                      <span>
                        <strong>
                          {Math.round(
                            todaySessions.reduce(
                              (n, s) => n + (s.duration_seconds ?? 0),
                              0,
                            ) / 60,
                          )}
                        </strong>
                        分の記録
                      </span>
                    </div>
                  </div>
                  <section
                    className={cx(
                      "recorder-card",
                      !!recording && "is-recording",
                    )}
                  >
                    <div className="recorder-copy">
                      <span
                        className={cx("recorder-emblem", !!recording && "live")}
                      >
                        <AudioLines size={29} />
                      </span>
                      <div>
                        <div className="recorder-kicker">
                          {recording ? (
                            <>
                              <span className="live-dot" />
                              RECORDING IN PROGRESS
                            </>
                          ) : snapshot?.settings.auto_record ? (
                            "READY WHEN YOU ARE"
                          ) : (
                            "YOUR MEETING COMPANION"
                          )}
                        </div>
                        <h2>
                          {recording
                            ? "会話を、記録しています。"
                            : snapshot?.settings.auto_record
                              ? "次の会議も、取りこぼさずに。"
                              : "次の会話を、残しましょう。"}
                        </h2>
                        <p>
                          {recording
                            ? "予定時刻を過ぎても、退出するまで録音を続けます。"
                            : snapshot?.settings.auto_record
                              ? "Teamsの参加を検知すると、自動で録音を始めます。"
                              : "手動で録音、または設定から自動録音を有効にできます。"}
                        </p>
                        {recording && (
                          <div className="audio-meters">
                            <AudioMeter
                              label="マイク"
                              icon={<Mic size={13} />}
                              level={recording.microphone_level}
                            />
                            <AudioMeter
                              label="出力音声"
                              icon={<Headphones size={13} />}
                              level={recording.loopback_level}
                            />
                          </div>
                        )}
                      </div>
                    </div>
                    {recording ? (
                      <div className="recording-controls">
                        <strong className="recording-timer">
                          {duration(recording.seconds)}
                        </strong>
                        <button
                          className="button stop-button"
                          disabled={!!busy}
                          onClick={() =>
                            void act(
                              "stop",
                              () => post("/recordings/stop"),
                              "録音を保存し、処理を開始しました",
                            )
                          }
                        >
                          <Square size={14} fill="currentColor" />
                          録音を終了
                        </button>
                      </div>
                    ) : (
                      <div className="recorder-art" aria-hidden="true">
                        <div className="art-orbit" />
                        <div className="art-orbit inner" />
                        <div className="wave-bars">
                          {[12, 22, 37, 55, 32, 46, 65, 45, 29, 43, 25, 14].map(
                            (h, i) => (
                              <i key={i} style={{ height: h }} />
                            ),
                          )}
                        </div>
                        <span className="art-sparkle">✦</span>
                      </div>
                    )}
                    <div className="recorder-footer">
                      <span>
                        <span
                          className={cx(
                            "tiny-dot",
                            snapshot?.health.teams.state === "in_meeting" &&
                              "coral",
                          )}
                        />
                        {snapshot?.health.teams.state === "in_meeting"
                          ? "Teams 参加中"
                          : snapshot?.health.teams.state === "idle"
                            ? "Teams 待機中"
                            : "Teams 検知を確認"}
                        <span className="footer-divider" />
                        {snapshot?.settings.auto_record
                          ? "自動録音 ON"
                          : "自動録音 OFF"}
                      </span>
                      <button
                        className="text-button"
                        onClick={() => navigate("settings")}
                      >
                        設定を確認
                        <ArrowRight size={14} />
                      </button>
                    </div>
                  </section>
                  {!isConfigured && snapshot && (
                    <div className="setup-banner">
                      <span className="setup-icon">
                        <Sparkles size={19} />
                      </span>
                      <div>
                        <strong>最初の準備を、ひとつずつ。</strong>
                        <p>
                          {teamsUiaCalendar
                            ? "初回はTeamsで本日のカレンダーを日表示にし、予定を読み取りましょう。その後は最小化・バックグラウンドで利用できます。録音デバイス・音声モデル・LLM APIキーを確認し、自動録音を有効にしてください。"
                            : teamsCalendar
                              ? "Teamsの連携準備・録音デバイス・音声モデル・LLM APIキーを確認しましょう。Microsoftのアプリ登録は不要です。予定取得が使えない場合も、手動で録音できます。"
                              : desktopCalendar
                                ? "Outlookの起動・録音デバイス・音声モデル・LLM APIキーを確認して、自動録音を有効にしましょう。Microsoftのアプリ登録は不要です。"
                                : "カレンダー接続・録音デバイス・音声モデル・LLM APIキーを確認して、自動録音を有効にしましょう。"}
                        </p>
                      </div>
                      <button
                        className="button secondary small"
                        onClick={() => navigate("settings")}
                      >
                        セットアップ
                        <ArrowRight size={15} />
                      </button>
                    </div>
                  )}
                  {pending.length > 0 && (
                    <div className="processing-strip">
                      <LoaderCircle size={16} className="spin" />
                      <span>{pending.length}件の会議を処理中</span>
                      <button
                        className="text-button"
                        onClick={() => openSession(pending[0].id)}
                      >
                        {pending[0].title}
                        <ArrowRight size={14} />
                      </button>
                    </div>
                  )}
                  <div className="home-columns">
                    <section className="meetings-section">
                      <div className="section-heading">
                        <h2>
                          本日のスケジュール
                          <span className="count-pill">{meetings.length}</span>
                        </h2>
                        <button
                          className="text-button muted"
                          disabled={
                            !!busy ||
                            !snapshot ||
                            (microsoft.provider === "graph" &&
                              !microsoft.connected)
                          }
                          onClick={() =>
                            void act(
                              "sync",
                              () => post("/sync"),
                              "予定の取得状況を確認しました",
                            )
                          }
                        >
                          <RefreshCw
                            size={14}
                            className={busy === "sync" ? "spin" : ""}
                          />
                          {teamsCalendar ? "読み取る" : "同期"}
                        </button>
                      </div>
                      {partialCalendar && (
                        <div className="calendar-coverage" role="note">
                          <Monitor size={16} />
                          <div>
                            <strong>
                              Teamsから読み取れた予定（一部） · プレビュー
                            </strong>
                            <p>
                              Teamsが保持する予定表から取得します。最小化・バックグラウンドでも読み取れますが、予定表が停止・破棄された場合は取得できません。本日の全件取得は保証しません。
                            </p>
                          </div>
                        </div>
                      )}
                      {snapshot && <CalendarReadStatus snapshot={snapshot} />}
                      <div className="meeting-list">
                        {meetings.length ? (
                          meetings.map((meeting) => {
                            const linked = sessions.find(
                              (s) => s.meeting_id === meeting.id,
                            );
                            const ended =
                              new Date(meeting.end) < new Date(today);
                            const upcoming =
                              new Date(meeting.start) > new Date(today);
                            return (
                              <article
                                className={cx(
                                  "meeting-card",
                                  linked?.status === "recording" &&
                                    "meeting-live",
                                )}
                                key={meeting.id}
                              >
                                <div className="meeting-time">
                                  <strong>{time(meeting.start)}</strong>
                                  <span>{time(meeting.end)}</span>
                                </div>
                                <div className="meeting-body">
                                  <div className="meeting-top">
                                    <span className="teams-icon">T</span>
                                    <span className="meeting-provider">
                                      Microsoft Teams
                                    </span>
                                    {linked ? (
                                      <Status status={linked.status} />
                                    ) : (
                                      <span className="schedule-state">
                                        {ended
                                          ? "予定終了"
                                          : upcoming
                                            ? "予定"
                                            : "開催時間中"}
                                      </span>
                                    )}
                                  </div>
                                  <h3>{meeting.title}</h3>
                                  <div className="meeting-bottom">
                                    <div className="meeting-people">
                                      <div className="avatar-stack">
                                        {meeting.attendees
                                          .slice(0, 3)
                                          .map((person, i) => (
                                            <span
                                              className={`tiny-avatar color-${i}`}
                                              title={person}
                                              key={i}
                                            >
                                              {initials(person)}
                                            </span>
                                          ))}
                                      </div>
                                      <span>
                                        {meeting.attendees.length
                                          ? `${meeting.attendees.length} 人の参加予定`
                                          : meeting.organizer ||
                                            "参加者情報なし"}
                                      </span>
                                    </div>
                                    {linked ? (
                                      <button
                                        className="text-button"
                                        onClick={() => openSession(linked.id)}
                                      >
                                        記録を見る
                                        <ChevronRight size={15} />
                                      </button>
                                    ) : meeting.join_url ? (
                                      <button
                                        className="button secondary small"
                                        disabled={!!busy || !meeting.join_url}
                                        onClick={() => void join(meeting)}
                                      >
                                        参加する
                                        <ExternalLink size={13} />
                                      </button>
                                    ) : (
                                      <button
                                        className="button secondary small"
                                        disabled={
                                          !!busy ||
                                          meeting.id.startsWith("demo-") ||
                                          snapshot?.armed_meeting_id ===
                                            meeting.id
                                        }
                                        title="録音対象を選択してから、Teamsでこの会議に参加してください"
                                        onClick={() => void armMeeting(meeting)}
                                      >
                                        {snapshot?.armed_meeting_id ===
                                        meeting.id ? (
                                          <>
                                            <Check size={13} />
                                            選択済み
                                          </>
                                        ) : (
                                          <>
                                            録音対象に選択
                                            <Mic size={13} />
                                          </>
                                        )}
                                      </button>
                                    )}
                                  </div>
                                </div>
                              </article>
                            );
                          })
                        ) : (
                          <div className="content-card">
                            <Empty
                              icon={<CalendarDays />}
                              title={
                                teamsCalendar
                                  ? microsoft.connected
                                    ? "読み取れた本日の予定はありません"
                                    : teamsUiaCalendar
                                      ? "まずTeamsで本日のカレンダーを開く"
                                      : "Teams連携を準備しましょう"
                                  : microsoft.connected
                                    ? "本日のTeams会議はありません"
                                    : desktopCalendar
                                      ? "Outlookを起動して、予定を同期"
                                      : "選択したカレンダー接続を確認"
                              }
                            >
                              <p>
                                {teamsCalendar
                                  ? microsoft.connected
                                    ? "Teamsで本日のカレンダーを日表示にして、もう一度読み取ってください。Teamsが保持していない予定は取得できません。読み取り後は最小化して利用できます。"
                                    : teamsUiaCalendar
                                      ? "初回はTeamsでカレンダーを開き、「今日」・「日」表示にして読み取ってください。その後は最小化・バックグラウンドで利用できます。トレイに格納した際は、予定表が保持されている場合に限り読み取れます。"
                                      : "設定からTeamsデスクトップとの連携を準備できます。予定取得が使えない場合も、手動録音は利用できます。"
                                  : microsoft.connected
                                    ? "予定は自動で同期されます。予定外の会議も手動で録音できます。"
                                    : desktopCalendar
                                      ? "同じWindowsユーザーでOutlook（クラシック）を開き、サインイン済みの予定表を確認してください。Microsoftのアプリ登録やAPIキーは不要です。"
                                      : "設定・連携で、選択した接続の状態を確認してください。"}
                              </p>
                              {!microsoft.connected && (
                                <button
                                  className="text-button coral-text"
                                  disabled={!!busy}
                                  onClick={() => {
                                    if (desktopCalendar || teamsUiaCalendar) {
                                      void act(
                                        "sync",
                                        () => post("/sync"),
                                        "予定の取得状況を確認しました",
                                      );
                                    } else navigate("settings");
                                  }}
                                >
                                  {teamsUiaCalendar
                                    ? "Teamsの予定を読み取る"
                                    : teamsCalendar
                                      ? "Teams連携の設定へ"
                                      : desktopCalendar
                                        ? "Outlookの予定を同期"
                                        : "接続の設定を確認"}
                                  {desktopCalendar || teamsUiaCalendar ? (
                                    <RefreshCw
                                      size={14}
                                      className={busy === "sync" ? "spin" : ""}
                                    />
                                  ) : (
                                    <ArrowRight size={14} />
                                  )}
                                </button>
                              )}
                              {teamsCalendar && (
                                <button
                                  className="text-button manual-recording-link"
                                  disabled={!!recording || offline}
                                  onClick={openRecordingDialog}
                                >
                                  <Mic size={14} />
                                  予定がなくても手動で録音
                                </button>
                              )}
                            </Empty>
                          </div>
                        )}
                      </div>
                      <p className="sync-caption">
                        <RefreshCw size={11} />
                        {snapshot
                          ? `${snapshot.settings.sync_interval_seconds}秒ごとに${teamsCalendar ? "PC内のTeamsの予定表を読み取ります" : "予定の取得を試みます"}`
                          : "予定の取得状況を確認しています"}
                      </p>
                    </section>
                    <section className="recent-section">
                      <div className="section-heading">
                        <h2>最近の議事録</h2>
                        <button
                          className="text-button muted"
                          onClick={() => navigate("library")}
                        >
                          すべて
                          <ArrowRight size={14} />
                        </button>
                      </div>
                      <div className="recent-list">
                        {completed.slice(0, 4).map((item, i) => (
                          <button
                            className="recent-card"
                            key={item.id}
                            onClick={() => openSession(item.id)}
                          >
                            <span className={`document-icon tone-${i % 3}`}>
                              <FileText size={20} />
                            </span>
                            <span className="recent-card-copy">
                              <strong>{item.title}</strong>
                              <span>
                                {date(item.started_at)}
                                <i />
                                {Math.round(item.duration_seconds / 60)} 分
                              </span>
                              <span className="recent-complete">
                                <CheckCheck size={12} />
                                議事録を作成しました
                              </span>
                            </span>
                            <ChevronRight size={15} />
                          </button>
                        ))}
                        {!completed.length && (
                          <div className="content-card">
                            <Empty
                              icon={<FileText />}
                              title="記録が、ここに集まります"
                            >
                              <p>
                                会議が終わると、議事録や次のアクションをいつでも振り返れます。
                              </p>
                            </Empty>
                          </div>
                        )}
                      </div>
                      <div className="thought-card">
                        <span>“</span>
                        <p>
                          大切なのは、
                          <br />
                          書き留めることより、
                          <br />
                          <strong>話し合うこと。</strong>
                        </p>
                        <div>LESS NOTE-TAKING. MORE IDEAS.</div>
                        <Waves size={32} />
                      </div>
                    </section>
                  </div>
                </>
              ) : (
                <>
                  <div className="library-toolbar">
                    <label className="search-field">
                      <Search size={18} />
                      <input
                        aria-label="議事録を検索"
                        placeholder="会議のタイトルを検索…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                      />
                      {search && (
                        <button
                          className="icon-button"
                          onClick={() => setSearch("")}
                          aria-label="検索をクリア"
                        >
                          <X size={15} />
                        </button>
                      )}
                    </label>
                    <span>{filteredSessions.length} 件の記録</span>
                  </div>
                  <div className="library-grid">
                    {filteredSessions.map((item) => (
                      <button
                        className="library-card"
                        key={item.id}
                        onClick={() => openSession(item.id)}
                      >
                        <div className="library-card-top">
                          <span className="document-icon">
                            <FileText size={23} />
                          </span>
                          <Status status={item.status} />
                        </div>
                        <h3>{item.title}</h3>
                        <p>
                          {date(item.started_at)} · {time(item.started_at)}
                        </p>
                        <div className="library-card-bottom">
                          <span>
                            <Clock3 size={14} />
                            {Math.round(item.duration_seconds / 60)} 分
                          </span>
                          <ArrowRight size={17} />
                        </div>
                      </button>
                    ))}
                  </div>
                  {!filteredSessions.length && (
                    <div className="content-card">
                      <Empty
                        icon={<FolderOpen />}
                        title={
                          search
                            ? "一致する記録がありません"
                            : "まだ会議の記録がありません"
                        }
                      >
                        <p>
                          {search
                            ? "別のタイトルで検索してください。"
                            : "最初の会議を録音して、あなたのライブラリを作りましょう。"}
                        </p>
                      </Empty>
                    </div>
                  )}
                </>
              )}
              <footer className="page-footer">
                <span>
                  giji.{" "}
                  <span className="footer-tagline">会話を、次の一歩に。</span>
                </span>
                <span>
                  <ShieldCheck size={13} />
                  Designed to stay local
                </span>
              </footer>
            </>
          )}
        </main>
      </div>
      {toast && (
        <div className="toast" role="status">
          <Check size={17} />
          {toast}
          <button onClick={() => setToast("")} aria-label="通知を閉じる">
            <X size={15} />
          </button>
        </div>
      )}
      {showStart && (
        <Modal title="会議の録音を開始" onClose={() => setShowStart(false)}>
          <p className="modal-description">
            マイクと出力デバイスの音声をPC内に保存します。参加者への録音の周知・同意を確認してください。
          </p>
          <label className="field">
            <span>関連する会議</span>
            <select
              value={recordMeeting}
              onChange={(e) => setRecordMeeting(e.target.value)}
            >
              <option value="">予定外の会議</option>
              {meetings
                .filter((m) => !m.id.startsWith("demo-"))
                .map((m) => (
                  <option key={m.id} value={m.id}>
                    {time(m.start)} {m.title}
                  </option>
                ))}
            </select>
          </label>
          {!recordMeeting && (
            <label className="field">
              <span>会議のタイトル</span>
              <input
                autoFocus
                placeholder="例：プロジェクト定例ミーティング"
                value={recordTitle}
                onChange={(e) => setRecordTitle(e.target.value)}
              />
            </label>
          )}
          {!snapshot?.settings.recording_consent && (
            <div className="notice warning-notice">
              設定で「録音の周知・同意を確認しました」を有効にしてください。
            </div>
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setShowStart(false)}
            >
              キャンセル
            </button>
            <button
              className="button primary"
              disabled={!!busy || !snapshot?.settings.recording_consent}
              onClick={() => void startRecording()}
            >
              <Mic size={16} />
              録音を開始
            </button>
          </div>
        </Modal>
      )}
      {deleteConfirm && session && (
        <Modal
          title="この会議の記録を削除しますか？"
          onClose={() => setDeleteConfirm(false)}
        >
          <p className="modal-description">
            「{session.title}
            」の録音・文字起こし・議事録をPCから削除します。この操作は取り消せません。
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setDeleteConfirm(false)}
            >
              キャンセル
            </button>
            <button
              className="button danger-button"
              disabled={!!busy}
              onClick={async () => {
                const result = await act(
                  "delete",
                  () =>
                    request(`/sessions/${session.id}`, { method: "DELETE" }),
                  "会議の記録を削除しました",
                );
                if (result) {
                  setDeleteConfirm(false);
                  setSelected(null);
                }
              }}
            >
              削除する
            </button>
          </div>
        </Modal>
      )}
      {speakerEdit && session && (
        <Modal title="話者の名前を設定" onClose={() => setSpeakerEdit(false)}>
          <p className="modal-description">
            音声だけでは実名を特定できません。会話を確認して、話者ラベルに名前を対応付けてください。
          </p>
          {[...new Set(session.segments?.map((s) => s.speaker) ?? [])].map(
            (label) => (
              <label className="field" key={label}>
                <span>{speakerLabel(label)}</span>
                <input
                  value={speakerNames[label] ?? ""}
                  placeholder="名前を入力（任意）"
                  onChange={(e) =>
                    setSpeakerNames({
                      ...speakerNames,
                      [label]: e.target.value,
                    })
                  }
                />
              </label>
            ),
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setSpeakerEdit(false)}
            >
              キャンセル
            </button>
            <button
              className="button primary"
              disabled={!!busy}
              onClick={async () => {
                const result = await act<Session>(
                  "speakers",
                  () =>
                    post(`/sessions/${session.id}/speakers`, {
                      speaker_names: Object.fromEntries(
                        Object.entries(speakerNames).filter(([, name]) =>
                          name.trim(),
                        ),
                      ),
                    }),
                  "話者名を保存しました",
                );
                if (result) {
                  setSession(result);
                  setSpeakerEdit(false);
                }
              }}
            >
              名前を保存
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function AudioMeter({
  label,
  level,
  icon,
}: {
  label: string;
  level: number;
  icon: ReactNode;
}) {
  return (
    <span className="audio-meter">
      {icon}
      <span>{label}</span>
      <span className="meter-track">
        <i style={{ width: `${Math.min(100, Math.max(0, level * 100))}%` }} />
      </span>
    </span>
  );
}
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab") {
        const elements = Array.from(
          ref.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
          ) ?? [],
        );
        const first = elements[0],
          last = elements[elements.length - 1];
        if (
          e.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === ref.current)
        ) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={ref}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="ダイアログを閉じる"
          >
            <X size={19} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function AudioTrack({
  sessionId,
  track,
  title,
  subtitle,
  onError,
  audioUrl,
}: {
  sessionId: string;
  track: string;
  title: string;
  subtitle: string;
  onError: (error: string) => void;
  audioUrl?: string;
}) {
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setLoaded(false);
  }, [sessionId]);
  const safeUrl = audioUrl?.startsWith("/api/sessions/") ? audioUrl : undefined;
  return (
    <div className="audio-track">
      <span className="audio-track-icon">
        {track === "microphone" ? <Mic size={22} /> : <Headphones size={22} />}
      </span>
      <div>
        <h3>{title}</h3>
        <p>{subtitle}</p>
        {loaded && safeUrl ? (
          <>
            <audio
              controls
              src={safeUrl}
              preload="metadata"
              onError={() =>
                onError(
                  "録音データを再生できません。ファイルの状態を確認してください。",
                )
              }
            />
            <a
              className="text-button audio-download"
              href={safeUrl}
              download={`${track}.wav`}
            >
              <ArrowDownToLine size={13} />
              WAVをダウンロード
            </a>
          </>
        ) : (
          <button
            className="button secondary small"
            disabled={!safeUrl}
            onClick={() => setLoaded(true)}
          >
            <Play size={15} />
            {safeUrl ? "録音を読み込む" : "音声データはありません"}
          </button>
        )}
      </div>
    </div>
  );
}

type Act = <T>(
  key: string,
  fn: () => Promise<T>,
  message?: string,
) => Promise<T | undefined>;

function TeamsRetainedCalendar({
  snapshot,
  act,
  busy,
  savePending,
}: {
  snapshot: Snapshot;
  act: Act;
  busy: string;
  savePending: () => Promise<boolean>;
}) {
  const calendar = calendarConnection(snapshot);
  const selected =
    snapshot.settings.calendar_source === "teams_desktop" &&
    (snapshot.settings.teams_calendar_mode ?? "uia") === "uia";
  return (
    <div className="teams-setup teams-visible-calendar">
      <div className="no-registration-note">
        <ShieldCheck size={16} />
        <span>Microsoftのアプリ登録・Outlook・Teamsの再起動は不要です。</span>
      </div>
      <ol className="teams-setup-steps">
        <li>
          <span>1</span>
          <p>
            <strong>Teamsのカレンダーを開く</strong>
            このPCのTeamsデスクトップで、いつものアカウントにサインインしてください。
          </p>
        </li>
        <li>
          <span>2</span>
          <p>
            <strong>「今日」・「日」表示で、本日の予定を読み込む</strong>
            初回に本日のカレンダーを開いてください。Teamsが読み込んだ予定だけが対象です。必要に応じてご自身でスクロールしてください。
          </p>
        </li>
        <li>
          <span>3</span>
          <p>
            <strong>予定を読み取る</strong>
            読み取り後はTeamsを最小化したり、別のアプリを前面で使ったりできます。gijiはTeamsを前面に出さず、保持されている予定表を定期的に読み取ります。
          </p>
        </li>
      </ol>
      <div className="teams-preview-note">
        <CircleHelp size={16} />
        <div>
          <strong>
            最小化・バックグラウンドでも、予定表が保持されていれば取得。
          </strong>
          <p>
            トレイに格納した場合も、Teamsが予定表の情報を保持している間だけ読み取れます。停止・破棄されると取得できず、本日の予定がすべて揃うとも限りません。gijiがTeamsを前面に移動したり、クリック・画面遷移したりすることはありません。
          </p>
        </div>
      </div>
      <div className="connection-actions">
        <button
          className="button secondary"
          disabled={!!busy}
          onClick={async () => {
            if (!(await savePending())) return;
            await act(
              "sync",
              () => post("/sync"),
              "予定の読み取り結果を確認してください",
            );
          }}
        >
          <RefreshCw size={15} className={busy === "sync" ? "spin" : ""} />
          Teamsの予定を読み取る
        </button>
        <span className="muted" role="status">
          {selected
            ? calendar.detail
            : "保存して、Teamsが保持する予定表を読み取る方式に切り替えます。"}
        </span>
      </div>
      <p className="setting-hint teams-fallback-note">
        会議名を録音に引き継ぐには、ホームで「録音対象に選択」を押してからTeamsで参加してください。予定には対面会議なども含まれる場合があります。取得できない場合も手動録音を利用できます。
      </p>
    </div>
  );
}

function TeamsSetup({
  snapshot,
  act,
  busy,
  savePending,
}: {
  snapshot: Snapshot;
  act: Act;
  busy: string;
  savePending: () => Promise<boolean>;
}) {
  const connection = teamsConnection(snapshot);
  const callActive =
    !!snapshot.health.recording || snapshot.health.teams.state === "in_meeting";
  const setupActive =
    connection.connected ||
    connection.debug_active ||
    connection.needs_restart ||
    !connection.policy_restored ||
    connection.expires_at !== null;
  const expiresAt =
    connection.expires_at === null
      ? null
      : new Date(
          connection.expires_at < 1_000_000_000_000
            ? connection.expires_at * 1000
            : connection.expires_at,
        );
  return (
    <div className="teams-setup">
      <div className="teams-preview-note">
        <CircleHelp size={16} />
        <div>
          <strong>Teamsが保持する予定表を、前面に移動せず取得します。</strong>
          <p>
            初回に本日の予定表を開いてください。カレンダーを別ウィンドウで開いて保持する方法も利用できます。最小化・バックグラウンド・トレイ格納時も予定表が読み込み済みで残っていれば取得しますが、停止・破棄された予定表は取得できません。
          </p>
        </div>
      </div>
      <ol className="teams-setup-steps">
        <li>
          <span>1</span>
          <p>
            <strong>会議が終わってから、連携設定を有効にする</strong>
            下の説明を確認し、「Teams連携を準備」を押します。
          </p>
        </li>
        <li>
          <span>2</span>
          <p>
            <strong>Teamsを完全に終了して、もう一度起動</strong>
            ウィンドウを閉じるだけではなく、タスクトレイのTeamsアイコンから終了してください。終了・起動はご自身で行います。
          </p>
        </li>
        <li>
          <span>3</span>
          <p>
            <strong>Teamsのカレンダーで、本日の予定を読み込む</strong>
            準備から10分以内に通常の方法でTeamsを起動してください。「接続済み」になったら今日のカレンダーを一度開き、予定を読み取ります。その後は最小化できます。
          </p>
        </li>
      </ol>
      <div className="teams-access-notice">
        <ShieldCheck size={18} />
        <div>
          <strong>有効にする前に、ローカル接続について</strong>
          <p>
            連携中は、このPC上の別のアプリからも、予定表以外を含むTeams内の情報を読み取ったり操作したりできる接続口が開きます。信頼できるPCで、必要な間だけ有効にしてください。
          </p>
          <p>
            準備用の一時設定は最大10分です。設定が復元済みでも接続口はTeamsの完全終了まで残ります。使い終わったら連携を解除し、会議終了後にTeamsを完全終了して通常起動してください。
          </p>
        </div>
      </div>
      <div
        className={cx(
          "teams-connection-status",
          connection.connected && "connected",
        )}
        role="status"
      >
        <div className="teams-status-heading">
          <span className="tiny-dot" />
          <strong>{teamsStateNames[connection.state]}</strong>
          {connection.policy_restored && setupActive && (
            <span className="connection-pill">一時設定は復元済み</span>
          )}
        </div>
        <p>{connection.detail}</p>
        {expiresAt && !Number.isNaN(expiresAt.getTime()) && (
          <small>
            一時設定の期限：
            {expiresAt.toLocaleTimeString("ja-JP", {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </small>
        )}
        {connection.needs_restart && (
          <p className="teams-restart-note">
            会議・録音が終了してから、Teamsを完全終了し、通常の方法で起動してください。
          </p>
        )}
        {connection.debug_active && connection.policy_restored && (
          <p className="teams-restart-note">
            ローカル接続口はまだ有効です。閉じるにはTeamsの完全終了が必要です。
          </p>
        )}
      </div>
      {callActive && (
        <p className="teams-call-notice">
          会議・録音中は新たな連携準備を行えません。終了後に操作してください。設定の解除は録音を停止しません。
        </p>
      )}
      <div className="teams-setup-actions">
        <button
          className="button secondary"
          disabled={
            !!busy ||
            callActive ||
            connection.connected ||
            connection.state === "waiting_for_restart"
          }
          onClick={async () => {
            if (!(await savePending())) return;
            await act(
              "teams-prepare",
              () => post("/teams/prepare"),
              "Teams連携の準備を確認しました。表示された状態と再起動の案内をご確認ください。",
            );
          }}
        >
          {busy === "teams-prepare" ? (
            <LoaderCircle size={15} className="spin" />
          ) : (
            <Monitor size={15} />
          )}
          Teams連携を準備
        </button>
        <button
          className="button secondary"
          disabled={!!busy || !connection.connected}
          onClick={async () => {
            if (!(await savePending())) return;
            await act(
              "sync",
              () => post("/sync"),
              "予定の読み取り結果を確認してください",
            );
          }}
        >
          <RefreshCw size={15} className={busy === "sync" ? "spin" : ""} />
          予定を読み取る
        </button>
        <button
          className="text-button teams-disconnect"
          disabled={!!busy || !setupActive}
          onClick={() =>
            void act(
              "teams-disconnect",
              () => post("/teams/disconnect"),
              "Teams連携の設定を解除しました。表示された再起動の案内を確認してください。",
            )
          }
        >
          連携を解除
        </button>
      </div>
      <p className="setting-hint teams-fallback-note">
        連携できない場合も、ホームの「録音を開始」でマイクと相手の声を記録できます。Outlook（クラシック）があれば、下の取得元へ切り替えることもできます。
      </p>
    </div>
  );
}

function SettingsPage({
  snapshot,
  act,
  busy,
}: {
  snapshot: Snapshot | null;
  act: Act;
  busy: string;
}) {
  const [draft, setDraft] = useState<Settings | null>(null);
  const [devices, setDevices] = useState<{
    microphones: { id: number; name: string }[];
    loopbacks: { id: number; name: string }[];
  }>({ microphones: [], loopbacks: [] });
  const [deviceError, setDeviceError] = useState("");
  const [devicesLoaded, setDevicesLoaded] = useState(false);
  const [login, setLogin] = useState<{
    user_code: string;
    verification_uri: string;
    message: string;
  } | null>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (snapshot && !draft)
      setDraft({
        ...snapshot.settings,
        calendar_source: snapshot.settings.calendar_source ?? "teams_desktop",
        teams_calendar_mode: snapshot.settings.teams_calendar_mode ?? "uia",
        api_key: "",
      });
  }, [snapshot, draft]);
  const loadDevices = useCallback(() => {
    request<typeof devices>("/devices")
      .then((data) => {
        setDevices(data);
        setDeviceError("");
        setDevicesLoaded(true);
      })
      .catch((e) => {
        setDeviceError((e as Error).message);
        setDevicesLoaded(true);
      });
  }, []);
  useEffect(() => {
    loadDevices();
  }, [loadDevices]);
  useEffect(() => {
    if (snapshot?.settings.has_microsoft_login) setLogin(null);
  }, [snapshot?.settings.has_microsoft_login]);
  const microsoft = calendarConnection(snapshot);
  const teams = teamsConnection(snapshot);
  const microphoneReady =
    devicesLoaded &&
    !deviceError &&
    devices.microphones.length > 0 &&
    (snapshot?.settings.microphone_id == null ||
      devices.microphones.some(
        (d) => d.id === snapshot.settings.microphone_id,
      ));
  const outputReady =
    devicesLoaded &&
    !deviceError &&
    devices.loopbacks.length > 0 &&
    (snapshot?.settings.loopback_id == null ||
      devices.loopbacks.some((d) => d.id === snapshot.settings.loopback_id));
  if (!draft || !snapshot)
    return (
      <div className="loading-state">
        <LoaderCircle className="spin" />
        設定を読み込んでいます
      </div>
    );
  const selectedModelsReady =
    snapshot.health.models.ready &&
    draft.whisper_model === snapshot.settings.whisper_model &&
    draft.diarization === snapshot.settings.diarization;
  const setupSteps = [
    {
      label:
        microsoft.provider === "teams_desktop"
          ? "Teamsの予定の読み取りを確認"
          : microsoft.provider === "outlook_desktop"
            ? "Outlook・予定表を検出"
            : "Graphカレンダーに接続",
      ready: microsoft.connected,
      section: "calendar-settings",
    },
    {
      label: "マイク・出力デバイスを検出",
      ready: microphoneReady && outputReady,
      section: "audio-settings",
    },
    {
      label: "音声モデルの準備",
      ready: snapshot.health.models.ready,
      section: "model-settings",
    },
    {
      label: "LLM APIキーを登録",
      ready: snapshot.settings.has_api_key,
      section: "llm-settings",
    },
    {
      label: "録音の周知・同意",
      ready: snapshot.settings.recording_consent,
      section: "audio-settings",
    },
    {
      label: "AIへの送信に同意",
      ready: snapshot.settings.cloud_consent,
      section: "llm-settings",
    },
    {
      label: "自動録音を有効化",
      ready: snapshot.settings.auto_record,
      section: "audio-settings",
    },
  ];
  function update<K extends keyof Settings>(name: K, value: Settings[K]) {
    setDraft((d) => (d ? { ...d, [name]: value } : d));
    setDirty(true);
  }
  async function save(): Promise<boolean> {
    if (!draft) return false;
    const {
      has_api_key: _has,
      has_microsoft_login: _login,
      api_key,
      ...publicSettings
    } = draft;
    const result = await act<Settings>(
      "settings",
      () =>
        post("/settings", {
          ...publicSettings,
          ...(api_key ? { api_key } : {}),
        }),
      "設定を保存しました",
    );
    if (result) {
      setDraft({
        ...result,
        calendar_source: result.calendar_source ?? "teams_desktop",
        teams_calendar_mode: result.teams_calendar_mode ?? "uia",
        api_key: "",
      });
      setDirty(false);
      return true;
    }
    return false;
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">MAKE IT YOURS</div>
          <h1>
            設定・連携<span className="heading-dot">.</span>
          </h1>
          <p>このPCのTeamsから、会議の記録を始めましょう。</p>
        </div>
        <button
          className="button primary"
          disabled={!!busy || !dirty}
          onClick={() => void save()}
        >
          <Check size={17} />
          設定を保存{dirty && <span className="unsaved-dot" />}
        </button>
      </div>
      {dirty && (
        <div className="notice info-notice">
          <span>
            変更はまだ保存されていません。設定を保存して反映してください。
          </span>
        </div>
      )}
      <div className="settings-layout">
        <div className="settings-main">
          <section
            id="calendar-settings"
            className="content-card settings-card calendar-settings"
          >
            <div className="settings-heading">
              <span className="settings-section-icon purple">
                <CalendarDays size={23} />
              </span>
              <div>
                <h2>Teamsから、会議を取得</h2>
                <p>
                  最初に今日のカレンダーを開き、その後は最小化・バックグラウンドで利用できます。
                </p>
              </div>
              <span
                className={cx(
                  "connection-pill",
                  microsoft.connected && "connected",
                )}
              >
                {microsoft.connected
                  ? "検出済み"
                  : microsoft.state === "error"
                    ? "要確認"
                    : "確認待ち"}
              </span>
            </div>
            <label
              className={cx(
                "calendar-source-option teams-source-option",
                draft.calendar_source === "teams_desktop" && "selected",
              )}
            >
              <input
                type="radio"
                name="calendar-source"
                value="teams_desktop"
                checked={draft.calendar_source === "teams_desktop"}
                onChange={() => {
                  update("calendar_source", "teams_desktop");
                  setLogin(null);
                }}
              />
              <span className="teams-source-icon">T</span>
              <span>
                <strong>
                  Teams デスクトップ
                  <span className="preview-badge">プレビュー</span>
                </strong>
                <small>
                  Teamsが保持する予定表から取得します。Microsoftのアプリ登録は不要です。
                </small>
              </span>
              {draft.calendar_source === "teams_desktop" && (
                <Check size={18} className="source-selected-check" />
              )}
            </label>
            {draft.calendar_source === "teams_desktop" && (
              <>
                {draft.teams_calendar_mode === "uia" && (
                  <TeamsRetainedCalendar
                    snapshot={snapshot}
                    act={act}
                    busy={busy}
                    savePending={async () => !dirty || (await save())}
                  />
                )}
                <details
                  className="advanced-calendar teams-mode-settings"
                  open={draft.teams_calendar_mode === "cdp" ? true : undefined}
                >
                  <summary>詳細設定：Teamsの接続方法</summary>
                  <p className="setting-hint">
                    通常は「Teamsが保持する予定表を読み取る」を使用してください。詳細接続は、ローカル接続の公開範囲を確認して利用する任意の機能です。
                  </p>
                  <div className="teams-mode-options">
                    <label
                      className={cx(
                        "calendar-source-option",
                        draft.teams_calendar_mode === "uia" && "selected",
                      )}
                    >
                      <input
                        type="radio"
                        name="teams-calendar-mode"
                        value="uia"
                        checked={draft.teams_calendar_mode === "uia"}
                        onChange={() => update("teams_calendar_mode", "uia")}
                      />
                      <span>
                        <strong>
                          Teamsが保持する予定表を読み取る
                          <span className="recommended-badge">標準</span>
                        </strong>
                        <small>
                          追加設定は不要です。Teamsを前面に移動せず、読み取れる予定表を確認します。
                        </small>
                      </span>
                    </label>
                    <label
                      className={cx(
                        "calendar-source-option",
                        draft.teams_calendar_mode === "cdp" && "selected",
                      )}
                    >
                      <input
                        type="radio"
                        name="teams-calendar-mode"
                        value="cdp"
                        checked={draft.teams_calendar_mode === "cdp"}
                        onChange={() => update("teams_calendar_mode", "cdp")}
                      />
                      <span>
                        <strong>
                          詳細接続（CDP）
                          <span className="preview-badge">任意</span>
                        </strong>
                        <small>
                          連携準備とTeamsの完全終了・再起動が必要です。このPCに接続口を開きます。
                        </small>
                      </span>
                    </label>
                  </div>
                  {draft.teams_calendar_mode === "cdp" && (
                    <TeamsSetup
                      snapshot={snapshot}
                      act={act}
                      busy={busy}
                      savePending={async () => !dirty || (await save())}
                    />
                  )}
                </details>
                {microsoft.provider === "teams_desktop" && (
                  <CalendarReadStatus snapshot={snapshot} />
                )}
              </>
            )}
            {(draft.calendar_source !== "teams_desktop" ||
              draft.teams_calendar_mode !== "cdp") &&
              (teams.debug_active ||
                teams.needs_restart ||
                !teams.policy_restored) && (
                <div className="teams-other-source-notice">
                  <ShieldCheck size={16} />
                  <div>
                    <strong>Teamsのローカル接続設定を確認してください</strong>
                    <p>
                      取得方法を切り替えても、詳細接続で開いたTeamsの接続口は完全終了するまで残る場合があります。不要になったら設定を解除し、会議終了後にTeamsを完全終了して通常起動してください。
                    </p>
                    <button
                      className="text-button"
                      disabled={!!busy}
                      onClick={() =>
                        void act(
                          "teams-disconnect",
                          () => post("/teams/disconnect"),
                          "Teams連携の設定を解除しました。状態の案内を確認してください。",
                        )
                      }
                    >
                      Teams連携の設定を解除
                    </button>
                  </div>
                </div>
              )}
            <label
              className={cx(
                "calendar-source-option optional-calendar-source",
                draft.calendar_source === "outlook_desktop" && "selected",
              )}
            >
              <input
                type="radio"
                name="calendar-source"
                value="outlook_desktop"
                checked={draft.calendar_source === "outlook_desktop"}
                onChange={() => {
                  update("calendar_source", "outlook_desktop");
                  setLogin(null);
                }}
              />
              <Monitor size={23} />
              <span>
                <strong>
                  Outlook デスクトップ
                  <span className="recommended-badge">任意</span>
                </strong>
                <small>
                  Outlook（クラシック）の予定表を、このPCから読み取ります。
                </small>
              </span>
              {draft.calendar_source === "outlook_desktop" && (
                <Check size={18} className="source-selected-check" />
              )}
            </label>
            {draft.calendar_source === "outlook_desktop" && (
              <div className="desktop-calendar-guide">
                <div className="no-registration-note">
                  <ShieldCheck size={16} />
                  <span>Microsoftのアプリ登録・APIキーは不要です。</span>
                </div>
                <ol>
                  <li>
                    <span>1</span>
                    <p>
                      <strong>Outlook（クラシック）を起動</strong>giji
                      と同じWindowsユーザーで開いてください。
                    </p>
                  </li>
                  <li>
                    <span>2</span>
                    <p>
                      <strong>いつもの予定表を確認</strong>
                      Outlookでサインインし、Teams会議が予定表に表示されていることを確認します。
                    </p>
                  </li>
                  <li>
                    <span>3</span>
                    <p>
                      <strong>予定を同期</strong>
                      以降はgijiの起動中に自動で更新します。
                    </p>
                  </li>
                </ol>
                <div className="connection-actions">
                  <button
                    className="button secondary"
                    disabled={!!busy}
                    onClick={async () => {
                      if (dirty && !(await save())) return;
                      await act(
                        "sync",
                        () => post("/sync"),
                        "Outlookの予定の取得状況を確認しました",
                      );
                    }}
                  >
                    <RefreshCw
                      size={15}
                      className={busy === "sync" ? "spin" : ""}
                    />
                    Outlookの予定を同期
                  </button>
                  <span className="muted">
                    {microsoft.provider === "outlook_desktop"
                      ? microsoft.detail
                      : "保存してOutlookの予定取得に切り替えます。"}
                  </span>
                </div>
                {microsoft.provider === "outlook_desktop" &&
                  microsoft.account && (
                    <p className="calendar-account">
                      使用中のアカウント：{microsoft.account}
                    </p>
                  )}
                <p className="setting-hint compatibility-note">
                  「新しいOutlook」やブラウザー版のOutlookには対応していません。Outlook（クラシック）を使用してください。
                </p>
              </div>
            )}
            <details
              className="advanced-calendar"
              open={draft.calendar_source === "graph" ? true : undefined}
            >
              <summary>詳細設定：別のカレンダー接続を使う</summary>
              <p className="setting-hint">
                組織で既存のMicrosoft
                Graph接続が用意されている場合だけ利用する、任意の設定です。通常はTeamsデスクトップ、またはOutlookデスクトップを選択してください。
              </p>
              <label
                className={cx(
                  "calendar-source-option advanced-source",
                  draft.calendar_source === "graph" && "selected",
                )}
              >
                <input
                  type="radio"
                  name="calendar-source"
                  value="graph"
                  checked={draft.calendar_source === "graph"}
                  onChange={() => update("calendar_source", "graph")}
                />
                <Cloud size={20} />
                <span>
                  <strong>Microsoft Graph（既存の接続）</strong>
                  <small>利用を許可されたクライアントIDを持つ場合のみ。</small>
                </span>
              </label>
              {draft.calendar_source === "graph" && (
                <div className="graph-settings">
                  <div className="field-grid">
                    <label className="field">
                      <span>アプリケーション（クライアント）ID</span>
                      <input
                        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                        value={draft.client_id}
                        onChange={(e) => update("client_id", e.target.value)}
                        autoComplete="off"
                      />
                    </label>
                    <label className="field">
                      <span>テナントID</span>
                      <input
                        placeholder="organizations"
                        value={draft.tenant_id}
                        onChange={(e) => update("tenant_id", e.target.value)}
                        autoComplete="off"
                      />
                    </label>
                  </div>
                  <div className="connection-actions">
                    {microsoft.provider === "graph" && microsoft.connected ? (
                      <button
                        className="button secondary"
                        disabled={!!busy}
                        onClick={() =>
                          void act(
                            "logout",
                            () => post("/auth/logout"),
                            "Microsoft Graphの接続を解除しました",
                          )
                        }
                      >
                        Graphの接続を解除
                      </button>
                    ) : (
                      <button
                        className="button secondary"
                        disabled={!!busy || !draft.client_id}
                        onClick={async () => {
                          if (dirty && !(await save())) return;
                          const result = await act<typeof login>("login", () =>
                            post("/auth/login"),
                          );
                          if (result) setLogin(result);
                        }}
                      >
                        <span className="microsoft-mark">
                          <i />
                          <i />
                          <i />
                          <i />
                        </span>
                        Microsoft にサインイン
                        <ExternalLink size={14} />
                      </button>
                    )}
                    <span className="muted">
                      {microsoft.provider === "graph"
                        ? microsoft.detail
                        : "保存してGraph接続に切り替えます。"}
                    </span>
                  </div>
                  {login && (
                    <div className="login-code">
                      <strong>ブラウザーでこのコードを入力してください</strong>
                      <code>{login.user_code}</code>
                      <a
                        className="button secondary small"
                        href={login.verification_uri}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Microsoft の認証ページを開く
                        <ExternalLink size={13} />
                      </a>
                      <p>サインインの完了を自動で確認しています。</p>
                    </div>
                  )}
                </div>
              )}
            </details>
          </section>
          <section id="audio-settings" className="content-card settings-card">
            <div className="settings-heading">
              <span className="settings-section-icon peach">
                <Mic size={23} />
              </span>
              <div>
                <h2>録音と自動検知</h2>
                <p>自分の声と、会議で聞こえる声を別々のトラックに。</p>
              </div>
            </div>
            <div className="field-grid">
              <label className="field">
                <span>自分の声：マイク</span>
                <select
                  value={draft.microphone_id ?? ""}
                  onChange={(e) =>
                    update(
                      "microphone_id",
                      e.target.value === "" ? null : Number(e.target.value),
                    )
                  }
                >
                  <option value="">Windowsの既定デバイス</option>
                  {devices.microphones.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>相手の声：出力デバイス</span>
                <select
                  value={draft.loopback_id ?? ""}
                  onChange={(e) =>
                    update(
                      "loopback_id",
                      e.target.value === "" ? null : Number(e.target.value),
                    )
                  }
                >
                  <option value="">Windowsの既定の出力デバイス</option>
                  {devices.loopbacks.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="device-actions">
              <span className="setting-hint">
                Teamsと同じ出力デバイスを選択してください。他アプリの音も録音に含まれます。マイクはTeamsのミュートとは独立して録音されます。
              </span>
              <button className="text-button" onClick={loadDevices}>
                <RefreshCw size={13} />
                再取得
              </button>
            </div>
            {deviceError && <p className="field-error">{deviceError}</p>}
            {!deviceError && (
              <p className="device-readiness">
                {devicesLoaded
                  ? `${microphoneReady ? "マイクを検出済み" : "マイクを確認してください"} · ${outputReady ? "出力デバイスを検出済み" : "出力デバイスを確認してください"}`
                  : "このPCの録音デバイスを確認しています…"}
              </p>
            )}
            <Toggle
              checked={draft.recording_consent}
              onChange={(v) => update("recording_consent", v)}
              label="録音の周知・同意を確認しました"
              detail="会議参加者に録音を伝え、必要な同意を得て使用します。"
            />
            <Toggle
              checked={draft.auto_record}
              onChange={(v) => update("auto_record", v)}
              label="Teams会議を自動で録音"
              detail="Teamsデスクトップ版の参加・退出を検知。アプリを起動している間に動作します。"
            />
            <div className="field-grid compact-fields">
              <label className="field">
                <span>退出後の確認時間（秒）</span>
                <input
                  type="number"
                  min="4"
                  max="120"
                  value={draft.end_grace_seconds}
                  onChange={(e) =>
                    update("end_grace_seconds", Number(e.target.value))
                  }
                />
                <small>一時的な画面切り替えで録音が止まるのを防ぎます。</small>
              </label>
              <label className="field">
                <span>予定の読み取り間隔（秒）</span>
                <input
                  type="number"
                  min="30"
                  max="3600"
                  value={draft.sync_interval_seconds}
                  onChange={(e) =>
                    update("sync_interval_seconds", Number(e.target.value))
                  }
                />
              </label>
            </div>
            <div className="inline-diagnostic">
              <Monitor size={15} />
              <span>{snapshot.health.teams.detail}</span>
            </div>
          </section>
          <section id="model-settings" className="content-card settings-card">
            <div className="settings-heading">
              <span className="settings-section-icon sage">
                <AudioLines size={23} />
              </span>
              <div>
                <h2>ローカル文字起こし・話者分離</h2>
                <p>同梱の音声モデルを使い、音声をPC内で処理します。</p>
              </div>
            </div>
            <div className="field-grid">
              <label className="field">
                <span>音声認識モデル</span>
                <select
                  value={draft.whisper_model}
                  onChange={(e) => update("whisper_model", e.target.value)}
                >
                  <option value="tiny">tiny · 軽量</option>
                  <option value="base">base · 高速</option>
                  <option value="small">small · バランス（推奨）</option>
                  <option value="medium">medium · 高精度・高負荷</option>
                  <option value="large-v3">large-v3 · 最高精度・高負荷</option>
                </select>
              </label>
              <label className="field">
                <span>会議の言語</span>
                <select
                  value={draft.language}
                  onChange={(e) => update("language", e.target.value)}
                >
                  <option value="ja">日本語</option>
                  <option value="en">English</option>
                </select>
              </label>
            </div>
            <Toggle
              checked={draft.diarization}
              onChange={(v) => update("diarization", v)}
              label="相手の声を話者ごとに分離"
              detail="「話者1・話者2…」として整理します。実名は録音後に対応付けできます。"
            />
            <div className="model-status">
              <div>
                <strong>
                  <span
                    className={cx("tiny-dot", selectedModelsReady && "green")}
                  />
                  {selectedModelsReady
                    ? "音声モデルは準備済みです"
                    : "選択した音声モデルの準備が必要です"}
                </strong>
                <p>
                  {draft.whisper_model === snapshot.settings.whisper_model &&
                  draft.diarization === snapshot.settings.diarization
                    ? snapshot.health.models.detail
                    : "設定を保存してから、選択したモデルを準備します。"}
                </p>
              </div>
              <button
                className="button secondary small"
                disabled={!!busy || selectedModelsReady}
                onClick={async () => {
                  if (dirty && !(await save())) return;
                  await act(
                    "models",
                    () => post("/models/download"),
                    "音声モデルの準備を開始しました",
                  );
                }}
              >
                {selectedModelsReady ? (
                  <Check size={15} />
                ) : (
                  <ArrowDownToLine size={15} />
                )}
                {selectedModelsReady ? "準備済み" : "モデルを準備"}
              </button>
            </div>
            <p className="setting-hint">
              モデル同梱版では「準備済み」なら追加ダウンロードは不要です。別のモデルを選ぶ場合やファイルが不足する場合だけ、モデルを準備してください。
            </p>
          </section>
          <section id="llm-settings" className="content-card settings-card">
            <div className="settings-heading">
              <span className="settings-section-icon peach">
                <Sparkles size={23} />
              </span>
              <div>
                <h2>AIで議事録を作成</h2>
                <p>文字起こしから、要約・決定事項・次のアクションを整理。</p>
              </div>
            </div>
            <label className="field">
              <span>
                議事録生成用のLLM APIキー
                <span className="field-label-note">
                  {snapshot.settings.has_api_key
                    ? "登録済み · 変更時のみ入力"
                    : "未登録"}
                </span>
              </span>
              <input
                type="password"
                placeholder={
                  snapshot.settings.has_api_key
                    ? "••••••••••••••••"
                    : "お持ちのAPIキーを入力"
                }
                autoComplete="new-password"
                value={draft.api_key ?? ""}
                onChange={(e) => update("api_key", e.target.value)}
              />
              <small>
                お持ちのLLMサービスのキーを入力してください。Microsoft用のキーではありません。Windowsの資格情報マネージャーに保存します。
              </small>
            </label>
            <div className="field-grid">
              <label className="field">
                <span>APIベースURL</span>
                <input
                  type="url"
                  value={draft.llm_base_url}
                  onChange={(e) => update("llm_base_url", e.target.value)}
                />
                <small>OpenAI互換のAPIを利用できます。</small>
              </label>
              <label className="field">
                <span>モデル名</span>
                <input
                  value={draft.llm_model}
                  onChange={(e) => update("llm_model", e.target.value)}
                  placeholder="gpt-4.1-mini"
                />
              </label>
            </div>
            <Toggle
              checked={draft.cloud_consent}
              onChange={(v) => update("cloud_consent", v)}
              label="文字起こしを設定したAIサービスへ送信する"
              detail="音声は送信しません。APIの利用料金は、ご利用のサービスで発生します。"
            />
          </section>
          <section className="content-card settings-card demo-settings">
            <div>
              <h2>まずは、画面を試してみる</h2>
              <p>架空の会議と議事録を追加して、使い心地を確認できます。</p>
            </div>
            <div>
              <button
                className="button secondary small"
                disabled={!!busy}
                onClick={() =>
                  void act(
                    "demo",
                    () => post("/demo"),
                    "サンプルデータを追加しました",
                  )
                }
              >
                サンプルを追加
              </button>
              <button
                className="text-button muted"
                disabled={!!busy}
                onClick={() =>
                  void act(
                    "demo-delete",
                    () => request("/demo", { method: "DELETE" }),
                    "サンプルデータを削除しました",
                  )
                }
              >
                サンプルを削除
              </button>
            </div>
          </section>
        </div>
        <aside className="settings-aside">
          <div className="content-card setup-checklist portable-checklist">
            <h3>
              初回チェックリスト{" "}
              <span>
                {setupSteps.filter((step) => step.ready).length} /{" "}
                {setupSteps.length}
              </span>
            </h3>
            {setupSteps.map(({ label, ready, section }) => (
              <button
                className="checklist-step"
                key={label}
                onClick={() =>
                  document
                    .getElementById(section)
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
              >
                <span className={ready ? "check-circle done" : "check-circle"}>
                  {ready ? <Check size={12} /> : ""}
                </span>
                <span>{label}</span>
                <ChevronRight size={11} />
              </button>
            ))}
            <p>
              準備ができたら短い会議で、両方の音声と退出時の停止を確認してください。ここには現在の設定・検出状況を表示しています。
            </p>
            <p>
              ZIP同梱版にはPython
              3.13が含まれます。PCへのPythonの追加設定は不要です。
            </p>
          </div>
          <div className="settings-info">
            <ShieldCheck size={23} />
            <h3>
              保存はローカル。
              <br />
              AIには、必要なテキストだけ。
            </h3>
            <p>
              録音と文字起こしはPC内に保存されます。議事録生成のときだけ、同意した文字起こしが設定先のAPIに送信されます。
            </p>
          </div>
          {snapshot.health.warnings.length > 0 && (
            <div className="diagnostics">
              <h3>確認事項</h3>
              {snapshot.health.warnings.map((w, i) => (
                <p key={i}>{w}</p>
              ))}
            </div>
          )}
        </aside>
      </div>
    </>
  );
}
