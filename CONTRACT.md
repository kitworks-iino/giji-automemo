# Internal implementation contract

Windows 11 local application, Python 3.11+, FastAPI loopback server, React/Vite UI.
All timestamps ISO8601 UTC. Public API paths `/api`. Request authentication:
`Authorization: Bearer <runtime token>`; token supplied to SPA in URL fragment
`#token=...`. No secret is committed. UI language Japanese.

## Shared entities

Meeting: `{id,title,start,end,join_url,organizer,attendees:[string],cancelled:bool}`
Session: `{id,meeting_id:null|string,title,status,started_at,ended_at:null|string,
duration_seconds:number,error:null|string,source:"manual"|"automatic",directory:string,
segments:[{start:number,end:number,speaker:string,text:string}],
speaker_names:{[label]:name},minutes:null|object}`
Statuses: recording, transcribing, diarizing, summarizing, ready, error, interrupted.
Minutes object: `{summary:string,decisions:[string],action_items:[{task:string,
owner:string|null,due_date:string|null}],open_questions:[string],topics:[{title:string,
notes:[string]}]}`.

## Settings (public)

`calendar_source:"outlook_desktop"|"graph"="outlook_desktop",
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
calendar connectivity is reported through `snapshot.microsoft.connected`.

## HTTP API

* GET `/snapshot`: `{meetings:[],sessions:[],settings:{},
  microsoft:{provider:"outlook_desktop"|"graph",state,connected:bool,detail},
  health:{platform,
  teams:{state:"in_meeting"|"idle"|"unknown"|"unavailable",detail:string},
  calendar:{state,detail,last_sync:null|string},pipeline:{state,detail},
  recording:null|{session_id,seconds,microphone_level,loopback_level},
  models:{ready:bool,detail:string},warnings:[string]},now:string}`
* POST `/settings` → settings; GET `/devices` → `{microphones:[{id,name}],
  loopbacks:[{id,name}]}`
* POST `/auth/login` → `{user_code,verification_uri,message}` (optional Graph only;
  desktop source returns a message that sign-in is unnecessary)
* POST `/auth/logout`, POST `/sync` → `{ok:true}`
* POST `/meetings/{id}/join` → `{join_url}` (arms matching session, no automated join)
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

Errors use `{detail:string}` with appropriate HTTP status. No absolute paths
in client responses. Snapshot session list may omit segments and minutes.

## Module ownership and Python interfaces

Parent: `giji/store.py`, `giji/service.py`, `giji/api.py`, `giji/__main__.py`,
`giji/__init__.py`, pyproject, integration tests.
Frontend agent: entire `frontend/`.
Integration agent: `giji/microsoft.py`, `giji/teams.py`, `giji/audio.py`,
tests for those modules.
Pipeline agent: `giji/pipeline.py`, `giji/models.py`, tests for those modules.

Integration:
`CalendarRouter(secrets,settings_provider)` selects the provider explicitly.
`DesktopOutlookCalendar.fetch_today(settings)->list[Meeting]` reads the current
classic Outlook default calendar in a bounded subprocess on its main STA
thread. No Graph credentials, Entra registration, token extraction or private
Teams endpoints. `status(settings)->{state,connected,provider,detail,account}`;
`close()`. Frozen worker command: `GijiAutoMemo.exe --calendar-worker`.
Desktop failure never falls back to Graph. A failed sync retains cached events
and reports the failure. A provider change clears cached events, not recordings.

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
