const fragment = new URLSearchParams(location.hash.slice(1));
const initialToken = fragment.get("token");
if (initialToken) {
  sessionStorage.setItem("giji-token", initialToken);
  history.replaceState(null, "", location.pathname + location.search);
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      Authorization: `Bearer ${sessionStorage.getItem("giji-token") ?? ""}`,
      ...options.headers,
    },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `操作に失敗しました (${response.status})`,
    );
  }
  return response.json() as Promise<T>;
}

export function post<T>(path: string, body?: unknown) {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function getBlob(path: string): Promise<Blob> {
  const response = await fetch("/api" + path, {
    headers: {
      Authorization: `Bearer ${sessionStorage.getItem("giji-token") ?? ""}`,
    },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "ファイルを読み込めませんでした",
    );
  }
  return response.blob();
}

export type Meeting = {
  id: string;
  title: string;
  start: string;
  end: string;
  join_url: string;
  organizer: string;
  attendees: string[];
  cancelled: boolean;
};
export type Segment = {
  start: number;
  end: number;
  speaker: string;
  text: string;
};
export type Minutes = {
  summary: string;
  decisions: string[];
  action_items: {
    task: string;
    owner: string | null;
    due_date: string | null;
  }[];
  open_questions: string[];
  topics: { title: string; notes: string[] }[];
};
export type Session = {
  id: string;
  meeting_id: string | null;
  title: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number;
  error: string | null;
  source: string;
  segments?: Segment[];
  speaker_names?: Record<string, string>;
  minutes?: Minutes | null;
  audio_urls?: { microphone?: string; system?: string };
};
export type Settings = {
  calendar_source: "teams_desktop" | "outlook_desktop" | "graph";
  teams_calendar_mode: "uia" | "cdp";
  client_id: string;
  tenant_id: string;
  auto_record: boolean;
  recording_consent: boolean;
  cloud_consent: boolean;
  microphone_id: number | null;
  loopback_id: number | null;
  whisper_model: string;
  language: string;
  diarization: boolean;
  llm_base_url: string;
  llm_model: string;
  sync_interval_seconds: number;
  end_grace_seconds: number;
  has_api_key: boolean;
  has_microsoft_login: boolean;
  api_key?: string;
};
export type MicrosoftConnection = {
  state:
    | "connected"
    | "disconnected"
    | "error"
    | "unconfigured"
    | "needs_setup"
    | "waiting_for_restart"
    | "blocked"
    | "unavailable"
    | "syncing"
    | "authenticating";
  provider: "teams_desktop" | "outlook_desktop" | "graph";
  connected: boolean;
  detail: string;
  account?: string | null;
  coverage?: string;
  warnings?: string[];
};
export type TeamsConnection = {
  provider: "teams_desktop";
  state:
    | "needs_setup"
    | "waiting_for_restart"
    | "connected"
    | "blocked"
    | "unavailable"
    | "disconnected";
  connected: boolean;
  detail: string;
  needs_restart: boolean;
  policy_restored: boolean;
  debug_active: boolean;
  expires_at: number | null;
};
export type Health = {
  platform: string;
  teams: { state: string; detail: string };
  calendar: {
    state: string;
    detail: string;
    last_sync: string | null;
    last_attempt?: string | null;
    last_read_succeeded?: boolean;
  };
  pipeline: { state: string; detail: string };
  recording: null | {
    session_id: string;
    seconds: number;
    microphone_level: number;
    loopback_level: number;
  };
  models: { ready: boolean; detail: string };
  warnings: string[];
};
export type Snapshot = {
  meetings: Meeting[];
  sessions: Session[];
  settings: Settings;
  microsoft?: MicrosoftConnection;
  teams_connection?: TeamsConnection;
  armed_meeting_id?: string | null;
  health: Health;
  now: string;
};
