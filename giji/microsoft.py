"""Read today's Teams meetings through delegated Microsoft Graph access.

No client secret is needed for this public-client device-code flow. MSAL's
serialized cache is only passed to the application's encrypted secret store.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

SCOPES = ["Calendars.ReadBasic"]
CACHE_KEY = "microsoft_token_cache"
GRAPH_URL = "https://graph.microsoft.com/v1.0/me/calendarView"
TEAMS_HOSTS = {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}


class CalendarError(RuntimeError):
    """A safe, user-readable calendar/authentication error."""


def valid_teams_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and url.hostname in TEAMS_HOSTS
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and bool(url.path.strip("/"))
            and not any(ord(char) < 32 for char in value)
        )
    except ValueError:
        return False


def today_range(now: datetime | None = None) -> tuple[str, str]:
    """Return UTC bounds, resolving both local midnights separately for DST."""
    if now is None:
        today = datetime.now().date()
        start = datetime.combine(today, time.min).astimezone()
        end = datetime.combine(today + timedelta(days=1), time.min).astimezone()
    else:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
        end = datetime.combine(
            now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo
        )
    return (
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
    )


def _utc_datetime(value: dict[str, Any]) -> str:
    parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # We explicitly request UTC and must not reinterpret a Windows time
        # zone as the computer's local zone if Graph returns an invalid reply.
        if value.get("timeZone", "UTC") not in {"UTC", "Etc/UTC", "GMT"}:
            raise ValueError("unexpected calendar timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_meeting(event: dict[str, Any]) -> dict[str, Any] | None:
    online_meeting = event.get("onlineMeeting") or {}
    join_url = online_meeting.get("joinUrl") or event.get("onlineMeetingUrl")
    if not valid_teams_url(join_url):
        return None
    start, end = _utc_datetime(event["start"]), _utc_datetime(event["end"])
    if end <= start or not isinstance(event.get("id"), str):
        raise ValueError("invalid calendar event")
    organizer = (event.get("organizer") or {}).get("emailAddress") or {}
    attendees = []
    for attendee in event.get("attendees") or []:
        address = attendee.get("emailAddress") or {}
        name = address.get("name") or address.get("address")
        if isinstance(name, str) and name not in attendees:
            attendees.append(name)
    return {
        "id": event["id"],
        "title": event.get("subject") or "タイトルのない会議",
        "start": start,
        "end": end,
        "join_url": join_url,
        "organizer": organizer.get("name") or organizer.get("address") or "",
        "attendees": attendees,
        "cancelled": bool(event.get("isCancelled")),
    }


def _safe_graph_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and url.hostname == "graph.microsoft.com"
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and url.path.startswith("/v1.0/")
        )
    except ValueError:
        return False


class MicrosoftCalendar:
    def __init__(self, secrets: Any) -> None:
        self.secrets = secrets
        self._lock = threading.RLock()
        self._app: Any = None
        self._cache: Any = None
        self._config: tuple[str, str] | None = None
        self._flow: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self._generation = 0

    @staticmethod
    def _configuration(settings: dict[str, Any]) -> tuple[str, str]:
        client_id = str(settings.get("client_id", "")).strip()
        tenant = str(settings.get("tenant_id") or "organizations").strip()
        try:
            UUID(client_id)
        except (ValueError, AttributeError):
            raise CalendarError(
                "設定にMicrosoft Entraのアプリケーション（クライアント）IDを入力してください。"
            ) from None
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", tenant)
            or ".." in tenant
        ):
            raise CalendarError("テナントIDまたはテナントのドメイン名を確認してください。")
        return client_id, tenant

    def _get_app(self, settings: dict[str, Any]) -> Any:
        config = self._configuration(settings)
        if self._app is not None and self._config == config:
            return self._app
        try:
            import msal

            cache = msal.SerializableTokenCache()
            raw = self.secrets.get(CACHE_KEY)
            if raw:
                saved = json.loads(raw)
                if (saved.get("client_id"), saved.get("tenant_id")) == config:
                    cache.deserialize(saved["cache"])
            app = msal.PublicClientApplication(
                config[0],
                authority=f"https://login.microsoftonline.com/{config[1]}",
                token_cache=cache,
                timeout=15,
            )
        except ImportError:
            raise CalendarError("Microsoft連携の依存パッケージ（msal）がありません。") from None
        except Exception:
            raise CalendarError(
                "Microsoft認証を初期化できません。接続とクライアントID・テナントIDを確認してください。"
            ) from None
        self._app, self._cache, self._config = app, cache, config
        return app

    def _persist_cache(self) -> None:
        if self._cache is None or self._config is None:
            return
        try:
            self.secrets.set(
                CACHE_KEY,
                json.dumps(
                    {
                        "client_id": self._config[0],
                        "tenant_id": self._config[1],
                        "cache": self._cache.serialize(),
                    }
                ),
            )
        except Exception:
            raise CalendarError(
                "Microsoft認証情報を暗号化保存できません。Windowsの資格情報保管機能を確認してください。"
            ) from None

    def begin_login(self, settings: dict[str, Any]) -> dict[str, str]:
        self.cancel_login()
        with self._lock:
            app = self._get_app(settings)
            try:
                flow = app.initiate_device_flow(scopes=SCOPES)
            except Exception:
                raise CalendarError("Microsoftのサインイン開始に失敗しました。接続を確認してください。") from None
            if not all(flow.get(key) for key in ("user_code", "device_code", "verification_uri")):
                raise CalendarError(
                    "サインインを開始できません。Entraアプリのパブリッククライアントフローと組織のポリシーを確認してください。"
                )
            self._cancel = threading.Event()
            self._flow = flow
            return {
                "user_code": flow["user_code"],
                # Use the trusted Microsoft location, not arbitrary server text.
                "verification_uri": "https://microsoft.com/devicelogin",
                "message": "Microsoftのページを開き、このコードを入力して予定表の読み取りを許可してください。",
            }

    def complete_login(self) -> bool:
        with self._lock:
            app, flow, cancel = self._app, self._flow, self._cancel
            generation = self._generation
        if app is None or flow is None:
            raise CalendarError("先にサインインを開始してください。")
        try:
            result = app.acquire_token_by_device_flow(flow)
        except Exception:
            if cancel.is_set():
                return False
            raise CalendarError("Microsoftサインインに失敗しました。もう一度お試しください。") from None
        with self._lock:
            if cancel.is_set() or generation != self._generation:
                return False
            self._flow = None
            if "access_token" not in result:
                raise CalendarError(
                    "サインインが完了しませんでした。コードの有効期限、組織のアクセス制限を確認して再試行してください。"
                )
            self._persist_cache()
            return True

    def cancel_login(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel.set()
            if self._flow is not None:
                # MSAL documents mutating expires_at to stop the polling loop.
                self._flow["expires_at"] = 0
            self._flow = None

    def logout(self) -> None:
        self.cancel_login()
        with self._lock:
            self._app = self._cache = self._config = None
            try:
                self.secrets.delete(CACHE_KEY)
            except Exception:
                raise CalendarError("保存済み認証情報を削除できませんでした。") from None

    def is_connected(self) -> bool:
        with self._lock:
            try:
                if self._app is not None:
                    return bool(self._app.get_accounts())
                raw = self.secrets.get(CACHE_KEY)
                if not raw:
                    return False
                saved = json.loads(raw)
                cache = json.loads(saved["cache"])
                return bool(cache.get("Account"))
            except Exception:
                return False

    def _access_token(self, settings: dict[str, Any]) -> str:
        with self._lock:
            app = self._get_app(settings)
            try:
                accounts = app.get_accounts()
                if not accounts:
                    raise CalendarError("Microsoftアカウントにサインインしてください。")
                result = app.acquire_token_silent(SCOPES, account=accounts[0])
            except CalendarError:
                raise
            except Exception:
                raise CalendarError("Microsoft認証を更新できません。接続を確認してください。") from None
            if not result or "access_token" not in result:
                raise CalendarError("Microsoftへの再サインインが必要です。")
            self._persist_cache()
            return result["access_token"]

    def fetch_today(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        token = self._access_token(settings)
        start, end = today_range()
        params: dict[str, str] | None = {
            "startDateTime": start,
            "endDateTime": end,
            "$select": "id,subject,start,end,onlineMeeting,onlineMeetingUrl,onlineMeetingProvider,isCancelled,organizer,attendees",
            "$top": "100",
            "$orderby": "start/dateTime",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": 'outlook.timezone="UTC", IdType="ImmutableId"',
        }
        url: str | None = GRAPH_URL
        visited: set[str] = set()
        meetings: dict[str, dict[str, Any]] = {}
        try:
            with httpx.Client(timeout=25, follow_redirects=False) as client:
                while url:
                    if not _safe_graph_url(url) or url in visited or len(visited) >= 100:
                        raise CalendarError("Microsoft Graphのページ情報が不正です。同期を中止しました。")
                    visited.add(url)
                    response = client.get(url, params=params, headers=headers)
                    params = None
                    if response.status_code == 401:
                        raise CalendarError("Microsoftへの再サインインが必要です。")
                    if response.status_code == 403:
                        raise CalendarError(
                            "予定表へのアクセスが許可されていません。Calendars.ReadBasic権限と組織のポリシーを確認してください。"
                        )
                    if response.status_code == 429:
                        raise CalendarError("Microsoftのアクセス制限に達しました。少し待ってから同期してください。")
                    if response.status_code != 200:
                        raise CalendarError(
                            f"Microsoft Graphから予定を取得できませんでした（HTTP {response.status_code}）。"
                        )
                    payload = response.json()
                    if not isinstance(payload.get("value"), list):
                        raise CalendarError("Microsoft Graphの予定データを読み取れませんでした。")
                    for event in payload["value"]:
                        meeting = normalize_meeting(event)
                        if meeting is not None:
                            meetings[meeting["id"]] = meeting
                    url = payload.get("@odata.nextLink")
        except CalendarError:
            raise
        except httpx.RequestError:
            raise CalendarError("予定表に接続できません。インターネット接続を確認してください。") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CalendarError("Microsoft Graphの予定データを読み取れませんでした。") from None
        return sorted(meetings.values(), key=lambda meeting: (meeting["start"], meeting["id"]))
