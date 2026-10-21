# Internal implementation contract

Windows 11 local application, Python 3.11+, FastAPI loopback server, React/Vite UI.
Meeting/session timestamps are ISO8601 UTC; the CDP preparation `expires_at`
is a Unix timestamp in seconds. Public API paths `/api`. Request authentication:
`Authorization: Bearer <runtime token>`; token supplied to SPA in URL fragment
`#token=...`. No secret is committed. UI language Japanese.

## Shared entities

Meeting: `{id,title,start,end,join_url:string,organizer,attendees:[string],cancelled:bool,
observed_at?:string}`. Missing participation URLs are the empty string; do not
invent URLs or participants. `observed_at` records individual Teams observations.
Session: `{id,meeting_id:null|string,title,status,started_at,ended_at:null|string,
duration_seconds:number,error:null|string,source:"manual"|"automatic",
segments:[{start:number,end:number,speaker:string,text:string}],
speaker_names:{[label]:name},minutes:null|object}`
Statuses: recording, transcribing, diarizing, summarizing, ready, error, interrupted.
Minutes object: `{summary:string,decisions:[string],action_items:[{task:string,
owner:string|null,due_date:string|null}],open_questions:[string],topics:[{title:string,
notes:[string]}]}`.

## Settings (public)

`calendar_source:"teams_desktop"|"outlook_desktop"|"graph"="teams_desktop",
teams_calendar_mode:"uia"|"cdp"="uia",
client_id:string,tenant_id:string="organizations",auto_record:bool=false,
recording_consent:bool=false,cloud_consent:bool=false,
microphone_id:null|number,loopback_id:null|number,
whisper_model:string="small",language:string="ja",diarization:bool=true,
llm_base_url:string="https://api.openai.com/v1",llm_model:string="gpt-4.1-mini",
sync_interval_seconds:number=60,end_grace_seconds:number=12,
has_api_key:bool,has_microsoft_login:bool`.
`POST /settings` accepts subset and optional `api_key` (never returned).
Secrets stored via OS keyring; public settings via SQLite.
`has_microsoft_login` refers only to optional Graph authentication. Desktop
calendar read status is reported through `snapshot.microsoft`, including
coverage and warnings. Existing saved provider choices survive an upgrade;
the new default applies only when the setting is absent. No automatic fallback
between calendar sources or Teams reader modes.

## HTTP API

* GET `/snapshot`: `{meetings:[],sessions:[],settings:{},
  microsoft:{provider:"teams_desktop"|"outlook_desktop"|"graph",state,connected:bool,
  detail,coverage:"unknown"|"visible_only"|"complete",warnings:[string]},
  teams_connection:TeamsConnection,armed_meeting_id:null|string,
  health:{platform,
  teams:{state:"in_meeting"|"idle"|"unknown"|"unavailable",detail:string},
  calendar:{state,detail,last_sync:null|string,coverage?:string,warnings?:[string]},
  pipeline:{state,detail},
  recording:null|{session_id,seconds,microphone_level,loopback_level},
  models:{ready:bool,detail:string},warnings:[string]},now:string}`
* POST `/settings` → settings; GET `/devices` → `{microphones:[{id,name}],
  loopbacks:[{id,name}]}`
* POST `/auth/login` → `{user_code,verification_uri,message}` (optional Graph only;
  desktop source returns a message that sign-in is unnecessary)
* POST `/auth/logout`, POST `/sync` → `{ok:true}`. Sync completion is not a
  guarantee of success: inspect the calendar state/coverage in the snapshot.
* POST `/teams/prepare` → TeamsConnection (Teams source and CDP mode only;
  rejected while recording or when a Teams call is detected)
* POST `/teams/disconnect` → TeamsConnection (stops CDP reads and restores own
  temporary policy; does not stop a recording or close the running Teams port)
* POST `/meetings/{id}/arm` → `{ok:true}` (explicitly select a valid, non-demo,
  uncancelled meeting from today's cache; does not join or start recording)
* POST `/meetings/{id}/join` → `{join_url}` (validates a Teams URL and arms the
  matching meeting; caller opens the returned link; no automated UI click)
* POST `/recordings/start` body `{meeting_id?:string,title?:string}` → session
* POST `/recordings/stop` → session
* GET `/sessions/{id}` → session
* POST `/sessions/{id}/retry` → session
* POST `/sessions/{id}/speakers` body `{speaker_names:{label:name}}` → session
* GET `/sessions/{id}/audio/{track}` track microphone|system → WAV
* GET `/sessions/{id}/export` → Markdown download
* DELETE `/sessions/{id}` → `{ok:true}` (active sessions cannot be deleted)
* POST `/models/download` → `{ok:true}` starts background preparation
* POST `/demo` → `{ok:true}` seeds clearly labelled demo sessions and meetings
* DELETE `/demo` → `{ok:true}` removes demo content

`TeamsConnection` is cached optional-CDP manager state, separate from UIA
calendar read state. Reading it does not probe Teams or mutate settings:
`{provider:"teams_desktop",state:"needs_setup"|"waiting_for_restart"|"connected"|
"blocked"|"unavailable"|"disconnected",connected:bool,detail:string,
needs_restart:bool,policy_restored:bool,debug_active:bool,expires_at:null|number}`.
`policy_restored:true` does not mean a running Teams debug port is closed.

Errors use `{detail:string}` with appropriate HTTP status. No absolute paths
in client responses. Snapshot session list may omit segments and minutes.

## Module ownership and Python interfaces

Parent: `giji/store.py`, `giji/service.py`, `giji/api.py`, `giji/__main__.py`,
`giji/__init__.py`, pyproject, integration tests.
Frontend agent: entire `frontend/`.
Integration agent: `giji/calendar.py`, `giji/desktop_calendar.py`,
`giji/teams_calendar_uia.py`, `giji/teams_calendar.py`,
`giji/teams_calendar_parser.py`, `giji/teams_connection.py`,
`giji/microsoft.py`, `giji/teams.py`, `giji/audio.py`, tests for those modules.
Pipeline agent: `giji/pipeline.py`, `giji/models.py`, tests for those modules.

Integration:
`CalendarRouter(secrets,settings_provider,*,desktop=None,graph=None,teams=None,
teams_uia=None,teams_connection=None)` selects the provider and Teams mode
explicitly. `status()->{coverage,warnings,...}` is used to decide whether a
successful sync replaces or merges the calendar cache.

`TeamsUIACalendar(timeout_seconds=12,*,runner=None)` is the default reader.
`fetch_today(settings)->list[Meeting]`, `status(settings=None)->dict`, `close()`.
It observes existing Teams calendar UIA elements from a bounded subprocess
whose main thread uses COM MTA. Frozen worker:
`GijiAutoMemo.exe --teams-uia-calendar-worker`; source worker:
`python -m giji.teams_calendar_uia --worker`. No registry or debug-port setup,
Teams restart, authentication extraction, UI click, focus change, or Graph/
private API request. Windows timezone is explicit input to the parser, and
status warns that Teams' own display timezone must match. Successful UIA
observations always report `coverage:"visible_only"`.

`TeamsDesktopCalendar(connection,*,timeout_seconds=20,client_factory=CDPClient)`
is the optional CDP reader. `fetch_today(settings)->list[Meeting]`,
`status(settings=None)->dict`, `close()`. It uses only a verified loopback
endpoint owned by the same-user Teams process and its related calendar frames.
CDP commands read existing display information; they do not click, navigate,
join meetings, change browser security, or read authentication material.

`TeamsConnectionManager(data_dir,...)`: `prepare()->TeamsConnection`,
`connect_endpoint()->str`, `status()->TeamsConnection`,
`disconnect()->TeamsConnection`, `close()`. Prepare explicitly installs a
maximum 600-second, current-user, Teams-executable-specific WebView2 startup
setting. Existing managed/debug settings are not overwritten. The user fully
exits and restarts Teams. Own setting is restored on connection, expiration,
disconnect or application exit; a detached cleanup helper also watches parent
exit and expiry. A running port persists until Teams exits. Reusing CDP after
Giji/Teams restart requires preparing and restarting Teams again.

`parse_calendar_snapshot(snapshot,now=None)->{meetings,coverage,warnings,
confirmed_today}` validates independently observed calendar dates and event
endpoints. DOM/AX/UIA fixtures in tests are synthetic, not live Teams captures.
Only an independently dated explicit empty day can yield parser/CDP complete
coverage. Unknown/partial UI observations must not be treated as an empty day.
`DesktopOutlookCalendar.fetch_today(settings)->list[Meeting]` reads the current
classic Outlook default calendar in a bounded subprocess on its main STA
thread. No Graph credentials, Entra registration, token extraction or private
Teams endpoints. `status(settings)->{state,connected,provider,detail,account}`;
`close()`. Frozen worker command: `GijiAutoMemo.exe --calendar-worker`.
No desktop failure falls back to another source or reader. A failed sync retains
cached events and reports the failure. Partial Teams sync merges today's
observations and can retain stale moved/cancelled meetings. Automatic matching
uses only the latest observed IDs with a valid Teams join URL. Calendar items
without visible join links may be offline appointments and cannot be inferred
to be Teams meetings. UIA ordinarily supplies no join URL; explicitly arming
a card binds its title. Unarmed automatic recording still works with an
unassociated meeting title. A user may explicitly arm today's cached meeting.
A previous-day or failed sync supplies no automatic candidates. A provider or
Teams reader-mode change clears cached events and the selected meeting, not
recordings.

`MicrosoftCalendar(secrets)` where secrets `get(name)->str|None`, `set(name,value)`,
`delete(name)`. `begin_login(settings)->dict` device flow details,
`complete_login()->bool` blocking worker; `cancel_login()` cancels polling;
`logout()`; `fetch_today(settings)->list[Meeting]` synchronous, local timezone
range converted to UTC, complete pagination. `is_connected()->bool`.
`TeamsDetector.poll()->dict {state,detail}` synchronous (~1s); no network.
`AudioRecorder.list_devices()->dict`; `start(directory:Path,settings:dict)`
creates microphone.wav and system.wav; `stop()->dict {duration_seconds}`;
`status()->dict {seconds,microphone_level,loopback_level,error:null|str}`.
Audio device failures must surface, partial files retained.

Pipeline:
`ModelManager(data_dir:Path,bundled_models:Path|None=None)`; `status(settings)->{ready,detail}`;
`download(settings,progress_callback)` blocking; all model downloads explicit.
Frozen runs resolve shipped models at `Path(sys.executable).parent / "models"`.
Complete user models take precedence, otherwise complete bundled models are
read directly without copying/downloading. Source runs never implicitly scan
the repository for models.
`Pipeline(model_manager,secrets)`; `process(session:dict,settings:dict,
on_update:Callable[[dict],None])->dict` blocking. Persist partial segments via
callback before LLM. Callback status and optional error/segments/minutes.
`render_markdown(session:dict)->str` safe deterministic export.
Self microphone label SELF; remote labels SPEAKER_00 etc, no inferred names.
`speaker_names` maps names manually; never infer real names from voice.
LLM sends transcript only, only when cloud_consent true and API key exists.
Local speech models default CPU int8. Long transcripts chunked and summarized.
Avoid sending secrets or raw transcripts in exception messages/logs.
