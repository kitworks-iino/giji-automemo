from __future__ import annotations

import asyncio
import copy
import platform
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .lifecycle import MeetingLifecycle, match_meeting
from .store import Secrets, Store


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_session(session: dict, compact: bool = False) -> dict:
    result = {k: v for k, v in session.items() if k != "directory"}
    if compact:
        result.pop("segments", None)
        result.pop("minutes", None)
    return result


class GijiService:
    def __init__(self, store: Store, secrets: Secrets | None = None, *, calendar=None,
                 detector=None, recorder=None, models=None, pipeline=None, teams_connection=None):
        from .audio import AudioRecorder
        from .calendar import CalendarRouter
        from .models import ModelManager
        from .pipeline import Pipeline
        from .teams import TeamsDetector
        from .teams_connection import TeamsConnectionManager

        self.store = store
        self.secrets = secrets or Secrets()
        self.teams_connection = teams_connection or TeamsConnectionManager(store.data_dir)
        self.calendar = calendar or CalendarRouter(
            self.secrets, self.store.settings, teams_connection=self.teams_connection)
        self.detector = detector or TeamsDetector()
        self.recorder = recorder or AudioRecorder()
        self.models = models or ModelManager(store.data_dir)
        self.pipeline = pipeline or Pipeline(self.models, self.secrets, settings_provider=self.store.settings)
        self.lifecycle = MeetingLifecycle()
        self.active_id: str | None = None
        self.armed_id: str | None = None
        self.last_calendar_ids: set[str] = set()
        self.tasks: list[asyncio.Task] = []
        self.login_task: asyncio.Task | None = None
        self.login_generation = 0
        self.model_task: asyncio.Task | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.pending: set[str] = set()
        self.record_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.health = {
            "platform": platform.system(),
            "teams": {"state": "unknown", "detail": "Teamsを確認しています"},
            "calendar": {"state": "disconnected", "detail": "Teamsで今日のカレンダーを日表示で一度開いてください。前面に置き続ける必要はありません。",
                         "last_sync": None, "last_attempt": None, "last_read_succeeded": False},
            "pipeline": {"state": "idle", "detail": "待機中"},
            "warnings": [],
        }
        self.model_progress: dict | None = None

    async def start(self):
        # v0.1 caches have no provider/profile provenance. Reacquire calendar
        # data each launch so old Graph/account events cannot be relabelled as
        # desktop events or silently matched to a new recording.
        self.store.replace_meetings([])
        # Never silently lose an interrupted recording or issue a second LLM call
        # after an unclean shutdown. The user can explicitly retry these jobs.
        for s in self.store.sessions():
            if s["status"] in ("recording", "transcribing", "diarizing", "summarizing"):
                self.store.patch_session(s["id"], {
                    "status": "interrupted", "ended_at": s.get("ended_at") or utcnow(),
                    "error": "前回の処理が中断されました。保存済み音声から再処理できます。",
                })
        self.tasks = [asyncio.create_task(self._monitor()),
                      asyncio.create_task(self._sync_loop()),
                      asyncio.create_task(self._worker())]

    async def close(self):
        self.stop_event.set()
        self.login_generation += 1
        self.calendar.cancel_login()
        if hasattr(self.calendar, "close"):
            self.calendar.close()
        try:
            self.teams_connection.close()
        except Exception:
            # Cleanup failure must not strand a microphone capture or prevent
            # saving audio. The journal/helper can retry exact-value restoration.
            self.health["warnings"].append("Teamsの一時設定を復元できませんでした。gijiを開き直して連携を解除してください。")
        if hasattr(self.detector, "close"):
            self.detector.close()
        if self.active_id:
            async with self.record_lock:
                sid = self.active_id
                try:
                    result = await asyncio.to_thread(self.recorder.stop)
                except Exception:
                    result = {}
                self.store.patch_session(sid, {
                    **result, "status": "interrupted", "ended_at": utcnow(),
                    "error": "アプリ終了のため録音を保存しました。再処理で議事録を生成できます。",
                })
                self.active_id = None
        # A cancelled to_thread task does not stop its thread. Keep SQLite open
        # until process exit, so a finishing pipeline can still persist progress.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def public_settings(self) -> dict:
        return {**self.store.settings(),
                "has_api_key": bool(self.secrets.get("llm_api_key")),
                "has_microsoft_login": self.store.settings()["calendar_source"] == "graph"
                    and self.calendar.is_connected()}

    def snapshot(self) -> dict:
        health = copy.deepcopy(self.health)
        health["models"] = self.model_progress or self.models.status(self.store.settings())
        health["recording"] = (
            {**self.recorder.status(), "session_id": self.active_id} if self.active_id else None
        )
        connection = {
            "provider": self.store.settings()["calendar_source"],
            "state": health["calendar"]["state"],
            "connected": health["calendar"]["state"] in {"connected", "syncing"}
                and health["calendar"].get("last_read_succeeded", False),
            "detail": health["calendar"]["detail"],
            "coverage": health["calendar"].get("coverage", "unknown"),
            "warnings": health["calendar"].get("warnings", []),
        }
        return {"meetings": self.today_meetings(), "microsoft": connection,
                "teams_connection": self.teams_connection.status(), "armed_meeting_id": self.armed_id,
                "sessions": [public_session(s, compact=True) for s in self.store.sessions()],
                "settings": self.public_settings(), "health": health, "now": utcnow()}

    def today_meetings(self, now: datetime | None = None) -> list[dict]:
        from .desktop_calendar import local_day_bounds

        start, end = local_day_bounds(now)
        meetings = []
        for meeting in self.store.meetings():
            try:
                begins = datetime.fromisoformat(meeting["start"])
                ends = datetime.fromisoformat(meeting["end"])
                if begins.tzinfo and ends.tzinfo and begins < end and ends > start:
                    meetings.append(meeting)
            except (KeyError, TypeError, ValueError):
                continue
        return meetings

    def matchable_meetings(self) -> list[dict]:
        if self.armed_id:
            # An explicit card selection may use today's cache during an outage.
            return self.today_meetings()
        calendar = self.health["calendar"]
        if (calendar["state"] not in {"connected", "syncing"} or not calendar["last_sync"]
                or not calendar.get("last_read_succeeded", False)):
            return []
        synced = datetime.fromisoformat(calendar["last_sync"]).astimezone()
        if synced.date() != datetime.now().astimezone().date():
            return []
        meetings = self.today_meetings()
        if self.store.settings()["calendar_source"] == "teams_desktop":
            from .microsoft import valid_teams_url

            # Cached, off-screen items may have been moved or cancelled. They
            # remain viewable, but only the latest observation can auto-match.
            # A visible calendar also contains offline appointments. An actual
            # Teams link or explicit selection is needed to bind a recording.
            return [m for m in meetings if m["id"] in self.last_calendar_ids
                    and valid_teams_url(m.get("join_url", ""))]
        return meetings

    async def update_settings(self, patch: dict):
        patch = dict(patch)
        secret = patch.pop("api_key", None)
        previous = self.store.settings()
        settings = {**previous, **patch}
        if settings["auto_record"] and not settings["recording_consent"]:
            raise ValueError("自動録音を有効にするには録音への同意を設定してください。")
        if secret is not None:
            if secret.strip():
                self.secrets.set("llm_api_key", secret.strip())
            else:
                self.secrets.delete("llm_api_key")
        source_changed = settings["calendar_source"] != previous["calendar_source"]
        mode_changed = settings["teams_calendar_mode"] != previous["teams_calendar_mode"]
        account_changed = (settings["client_id"], settings["tenant_id"]) != (
            previous["client_id"], previous["tenant_id"])
        if source_changed or account_changed or mode_changed:
            self.login_generation += 1
            self.calendar.cancel_login()
            async with self.sync_lock:
                if (source_changed or mode_changed) and previous["calendar_source"] == "teams_desktop":
                    await asyncio.to_thread(self.teams_connection.disconnect)
                if account_changed:
                    await asyncio.to_thread(self.calendar.logout)
                self.store.replace_meetings([])
                self.armed_id = None
                self.last_calendar_ids.clear()
                self.health["calendar"] = {
                    "state": "disconnected", "last_sync": None, "last_attempt": None, "last_read_succeeded": False,
                    "detail": ("Teamsで今日のカレンダーを日表示で一度開いてください。最小化・背面のまま取得を試みます。"
                               if settings["calendar_source"] == "teams_desktop"
                               else "Outlookを起動して予定を同期してください"
                               if settings["calendar_source"] == "outlook_desktop"
                               else "Microsoftアカウントを接続してください"),
                }
                self.store.update_settings(patch)
        else:
            self.store.update_settings(patch)
        return self.public_settings()

    async def sync(self):
        from .microsoft import CalendarError

        async with self.sync_lock:
            settings = self.store.settings()
            attempted_at = utcnow()
            self.health["calendar"]["last_attempt"] = attempted_at
            if settings["calendar_source"] == "graph" and not self.calendar.is_connected():
                self.last_calendar_ids.clear()
                self.health["calendar"].update(state="disconnected", last_read_succeeded=False,
                                               detail="Microsoftアカウントを接続してください")
                return
            detail = "今日の予定を読み取り中"
            if self.health["calendar"]["last_sync"] and not self.health["calendar"].get("last_read_succeeded", False):
                detail = "前回取得した予定を保持して、読み取りを再試行中です。最新の取得はまだ確認できていません。"
            self.health["calendar"].update(state="syncing", detail=detail)
            try:
                meetings = await asyncio.to_thread(self.calendar.fetch_today, settings)
                self.last_calendar_ids = {m["id"] for m in meetings}
                provider_status = self.calendar.status()
                coverage = provider_status.get("coverage", "unknown")
                if settings["calendar_source"] == "teams_desktop" and coverage != "complete":
                    # Virtualized Teams calendars do not expose every event.
                    # Keep earlier observations; an off-screen/deleted event
                    # cannot be distinguished until the user reconnects.
                    merged = {m["id"]: m for m in self.today_meetings()}
                    merged.update({m["id"]: {**m, "observed_at": utcnow()} for m in meetings})
                    self.store.replace_meetings(list(merged.values()))
                else:
                    self.store.replace_meetings(meetings)
                self.health["calendar"] = {
                    "state": "connected", "detail": provider_status.get("detail") or "同期済み",
                    "coverage": coverage, "warnings": provider_status.get("warnings", []),
                    "last_sync": utcnow(), "last_attempt": attempted_at, "last_read_succeeded": True,
                }
            except CalendarError as exc:
                self.last_calendar_ids.clear()
                self.health["calendar"].update(state="error", detail=str(exc), last_read_succeeded=False,
                                               coverage="unknown")
            except Exception:
                self.last_calendar_ids.clear()
                self.health["calendar"].update(
                    state="error", last_read_succeeded=False, coverage="unknown", detail=(
                        "予定を同期できません。従来版Outlookを起動し、同じWindowsユーザーでやり直してください。"
                        if settings["calendar_source"] == "outlook_desktop"
                        else "Teamsの予定を読み取れません。設定で接続状態を確認し、Teamsの今日のカレンダーを開いてください。"
                        if settings["calendar_source"] == "teams_desktop"
                        else "予定を同期できません。接続とMicrosoftの権限を確認し、必要なら再接続してください。"))

    async def login(self) -> dict:
        if self.store.settings()["calendar_source"] != "graph":
            raise ValueError("デスクトップ連携ではMicrosoftへの追加サインイン不要です。カレンダーを開いて予定を同期してください。")
        if self.login_task and not self.login_task.done():
            raise ValueError("Microsoftサインインを処理中です。画面のコードで認証してください。")
        details = await asyncio.to_thread(self.calendar.begin_login, self.store.settings())
        self.health["calendar"].update(state="authenticating", detail="ブラウザーで認証してください")
        self.login_generation += 1
        self.login_task = asyncio.create_task(self._complete_login(self.login_generation))
        return details

    async def _complete_login(self, generation: int):
        try:
            success = await asyncio.to_thread(self.calendar.complete_login)
            if generation != self.login_generation:
                return
            if success:
                await self.sync()
            else:
                self.health["calendar"].update(state="disconnected", detail="サインインが完了しませんでした。再度接続してください。")
        except Exception:
            if generation == self.login_generation:
                self.health["calendar"].update(state="error", detail="Microsoft認証に失敗しました。アプリ登録と組織の許可を確認してください。")

    async def logout(self):
        self.login_generation += 1
        self.calendar.cancel_login()
        # Let any current sync finish before clearing private calendar data.
        async with self.sync_lock:
            await asyncio.to_thread(self.calendar.logout)
            self.store.replace_meetings([])
            self.armed_id = None
            self.health["calendar"] = {"state": "disconnected", "detail": "未接続", "last_sync": None}

    async def prepare_teams(self) -> dict:
        from .teams_connection import TeamsConnectionError

        settings = self.store.settings()
        if settings["calendar_source"] != "teams_desktop" or settings["teams_calendar_mode"] != "cdp":
            raise ValueError("CDP接続を使う場合は、設定でTeamsの詳細接続を選んで保存してください。通常の画面読み取りには準備不要です。")
        if self.active_id or self.health["teams"]["state"] == "in_meeting":
            raise ValueError("会議と録音が終わってからTeams連携を準備してください。")
        async with self.sync_lock:
            try:
                result = await asyncio.to_thread(self.teams_connection.prepare)
            except TeamsConnectionError as exc:
                raise ValueError(str(exc)) from None
            self.store.replace_meetings([])
            self.armed_id = None
            self.health["calendar"] = {"state": "disconnected", "last_sync": None, "detail": result["detail"]}
            return result

    async def disconnect_teams(self) -> dict:
        from .teams_connection import TeamsConnectionError

        async with self.sync_lock:
            try:
                result = await asyncio.to_thread(self.teams_connection.disconnect)
            except TeamsConnectionError as exc:
                raise ValueError(str(exc)) from None
            if self.store.settings()["calendar_source"] == "teams_desktop":
                self.store.replace_meetings([])
                self.armed_id = None
                self.health["calendar"] = {"state": "disconnected", "last_sync": None, "detail": result["detail"]}
            return result

    def arm_meeting(self, meeting_id: str) -> None:
        meeting = next((m for m in self.today_meetings() if m["id"] == meeting_id), None)
        if not meeting or meeting_id.startswith("demo-") or meeting.get("cancelled"):
            raise ValueError("今日の有効な会議を選択してください。")
        self.armed_id = meeting_id

    async def start_recording(self, meeting_id: str | None = None, title: str | None = None,
                              source: str = "manual") -> dict:
        async with self.record_lock:
            if self.active_id:
                raise ValueError("すでに録音中です。")
            settings = self.store.settings()
            if not settings["recording_consent"]:
                raise ValueError("設定で録音の同意を有効にしてください。")
            meeting = next((m for m in self.store.meetings() if m["id"] == meeting_id), None)
            if meeting_id and (not meeting or meeting_id.startswith("demo-") or meeting.get("cancelled")):
                raise ValueError("この会議では録音を開始できません。")
            sid = uuid.uuid4().hex
            directory = self.store.data_dir / "recordings" / sid
            directory.mkdir(parents=True)
            session = {
                "id": sid, "meeting_id": meeting_id, "title": (meeting or {}).get("title") or title or "Teams会議（予定未紐付け）",
                "status": "recording", "started_at": utcnow(), "ended_at": None,
                "duration_seconds": 0, "error": None, "source": source,
                "directory": str(directory), "segments": [], "speaker_names": {"SELF": "自分"}, "minutes": None,
            }
            # Journal before opening devices, retaining failures for diagnosis.
            self.store.put_session(session)
            try:
                await asyncio.to_thread(self.recorder.start, directory, settings)
            except Exception as exc:
                self.store.patch_session(sid, {"status": "error", "ended_at": utcnow(),
                    "error": "録音デバイスを開けません。設定のマイクと出力デバイスを確認してください。"})
                raise ValueError("録音デバイスを開けません。Windows 11でマイクと出力デバイスを確認してください。") from exc
            self.active_id = sid
            self.armed_id = None
            self.lifecycle.on_started(self.health["teams"]["state"] == "in_meeting")
            return public_session(session)

    async def stop_recording(self, failed: bool = False) -> dict:
        async with self.record_lock:
            if not self.active_id:
                raise ValueError("録音中の会議がありません。")
            sid = self.active_id
            try:
                result = await asyncio.to_thread(self.recorder.stop)
                if self.recorder.status().get("error"):
                    failed = True
            except Exception:
                result = {}
                failed = True
            self.active_id = None
            self.lifecycle.on_stopped()
            patch = {**result, "ended_at": utcnow(), "status": "error" if failed else "transcribing",
                     "error": "録音デバイスが切断されました。保存済み音声を確認して再処理してください。" if failed else None}
            session = self.store.patch_session(sid, patch)
            if not failed:
                await self.enqueue(sid)
            return public_session(session)

    async def enqueue(self, sid: str):
        if sid in self.pending:
            raise ValueError("この会議はすでに処理中です。")
        self.pending.add(sid)
        self.queue.put_nowait(sid)

    async def retry(self, sid: str) -> dict:
        session = self.store.session(sid)
        if not session:
            raise KeyError(sid)
        if sid == self.active_id or sid in self.pending or sid.startswith("demo-"):
            raise ValueError("この会議は再処理できません。")
        self.store.patch_session(sid, {"status": "transcribing", "error": None})
        await self.enqueue(sid)
        return public_session(self.store.session(sid))

    async def _worker(self):
        while True:
            sid = await self.queue.get()
            try:
                session = self.store.session(sid)
                self.health["pipeline"] = {"state": "running", "detail": f"「{session['title']}」を処理中"}
                def update(patch: dict):
                    self.store.patch_session(sid, patch)
                result = await asyncio.to_thread(self.pipeline.process, session, self.store.settings(), update)
                self.store.patch_session(sid, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                current = self.store.session(sid)
                self.store.patch_session(sid, {"status": "error", "error": current.get("error") or
                    "処理できませんでした。音声モデル・API設定・送信同意を確認し、再処理してください。"})
            finally:
                self.health["pipeline"] = {"state": "idle", "detail": "待機中"}
                self.pending.discard(sid)
                self.queue.task_done()

    async def _sync_loop(self):
        while True:
            await self.sync()
            await asyncio.sleep(self.store.settings()["sync_interval_seconds"])

    async def _monitor(self):
        while True:
            try:
                state = await asyncio.to_thread(self.detector.poll)
                self.health["teams"] = state
                settings = self.store.settings()
                if self.active_id and self.recorder.status().get("error"):
                    await self.stop_recording(failed=True)
                action = self.lifecycle.step(
                    state["state"], bool(self.active_id),
                    settings["auto_record"] and settings["recording_consent"],
                    time.monotonic(), settings["end_grace_seconds"],
                )
                if action == "start":
                    meeting = match_meeting(self.matchable_meetings(), datetime.now(timezone.utc), self.armed_id)
                    try:
                        await self.start_recording(meeting["id"] if meeting else None, source="automatic")
                    except ValueError as exc:
                        self.lifecycle.on_stopped()
                        self.health["warnings"] = [str(exc)]
                elif action == "stop":
                    await self.stop_recording()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health["teams"] = {"state": "unknown", "detail": "Teamsを読み取れません。手動操作が利用できます。"}
                self.lifecycle.step("unknown", bool(self.active_id), False, time.monotonic(), 12)
            await asyncio.sleep(2)

    async def download_models(self):
        if self.model_task and not self.model_task.done():
            raise ValueError("モデルをダウンロード中です。")
        if self.pending:
            raise ValueError("音声処理が完了してからモデルを準備してください。")
        settings = self.store.settings()
        self.model_progress = {"ready": False, "detail": "モデルをダウンロードしています"}
        def progress(message):
            self.model_progress = {"ready": False, "detail": str(message)}
        async def run():
            try:
                await asyncio.to_thread(self.models.download, settings, progress)
                self.model_progress = None
            except Exception:
                self.model_progress = {"ready": False, "detail": "ダウンロードに失敗しました。接続と空き容量を確認して再実行してください。"}
        self.model_task = asyncio.create_task(run())

    def delete_session(self, sid: str):
        if sid == self.active_id or sid in self.pending:
            raise ValueError("処理中の会議は削除できません。")
        session = self.store.session(sid)
        if not session:
            raise KeyError(sid)
        if not sid.startswith("demo-"):
            folder = self.safe_directory(session)
            if folder.exists():
                shutil.rmtree(folder)
        self.store.delete_session(sid)

    def safe_directory(self, session: dict) -> Path:
        expected = (self.store.data_dir / "recordings" / session["id"]).resolve()
        supplied = Path(session["directory"]).resolve()
        if supplied != expected or not expected.is_relative_to((self.store.data_dir / "recordings").resolve()):
            raise ValueError("保存先が正しくありません。")
        return expected

    def seed_demo(self):
        now = datetime.now().astimezone()
        start = now.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        for i, title in enumerate(["プロダクト定例", "デザインレビュー", "来週のスプリント計画"]):
            when = start + timedelta(hours=i * 2)
            self.store.add_meeting({"id": f"demo-{i}", "title": f"【デモ】{title}",
                "start": when.isoformat(), "end": (when + timedelta(minutes=45)).isoformat(),
                "join_url": "", "organizer": "デモ", "attendees": ["田中", "佐藤", "自分"], "cancelled": False})
        self.store.put_session({
            "id": "demo-session", "meeting_id": "demo-0", "title": "【デモ】プロダクト定例",
            "status": "ready", "started_at": start.isoformat(), "ended_at": (start + timedelta(minutes=42)).isoformat(),
            "duration_seconds": 2520, "source": "manual", "directory": "", "error": None,
            "speaker_names": {"SELF": "自分", "SPEAKER_00": "田中"},
            "segments": [
                {"start": 0, "end": 5, "speaker": "SELF", "text": "今日はオンボーディング改善の進め方を確認しましょう。"},
                {"start": 5, "end": 12, "speaker": "SPEAKER_00", "text": "最初の画面を簡潔にして、設定を三つのステップに分けたいです。"},
                {"start": 12, "end": 18, "speaker": "SELF", "text": "では私が次回までにワイヤーフレームを作成します。"},
            ],
            "minutes": {"summary": "オンボーディング改善の方針を確認しました。これは画面確認用のデモで、実際の録音ではありません。",
                "decisions": ["初期設定を3ステップに整理する"],
                "action_items": [{"task": "ワイヤーフレームを作成する", "owner": "自分", "due_date": "次回まで"}],
                "open_questions": ["ユーザーテストの日程"], "topics": [{"title": "初回体験の改善", "notes": ["最初の画面の情報量を減らす"]}]},
        })
