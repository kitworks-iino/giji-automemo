"""Read the signed-in Teams desktop calendar through its local accessibility UI.

This is an explicitly enabled, version-dependent preview, not a Microsoft API.
Only rendered DOM/accessibility observations are read. No cookies, tokens,
application state, network interception or private calendar endpoints are used.
The supported CDP protocol is documented at chromedevtools.github.io/devtools-protocol/.
Teams' calendar may be an out-of-process iframe; a top-page-only reader is not
sufficient. Every calendar frame must be a visible descendant of a Teams page.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .teams_calendar_parser import CalendarParseError, parse_calendar_snapshot
from .teams_connection import TeamsConnectionError, validate_cdp_url


class TeamsCalendarError(RuntimeError):
    """A public error that never contains raw browser/CDP output."""


_TEAMS_HOSTS = {"teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com"}
_OUTLOOK_HOSTS = {"outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"}
_MAX_MESSAGE = 4 * 1024 * 1024
_TIME_RANGE = re.compile(r"\d{1,2}(?::\d{2}|時).*?(?:to|until|から|[〜～–—-]).*?\d{1,2}(?::\d{2}|時)", re.I)


def _bounded_socket_class(deadline: float | None = None):
    """Bound frames before allocation and fragmented messages before joining.

    websocket-client 1.x exposes no max-message option. These two small buffer
    subclasses are tested against the constrained dependency's wire framing.
    """
    import websocket
    from websocket._abnf import continuous_frame, frame_buffer

    class BoundedBuffer(frame_buffer):
        def recv_length(self):
            super().recv_length()
            if self.length is None or self.length > _MAX_MESSAGE:
                raise TeamsCalendarError("Teams の画面情報が大きすぎるため、読み取りを中止しました。")

    class BoundedContinuation(continuous_frame):
        def add(self, frame):
            previous = len(self.cont_data[1]) if self.cont_data else 0
            if previous + len(frame.data) > _MAX_MESSAGE:
                raise TeamsCalendarError("Teams の画面情報が大きすぎるため、読み取りを中止しました。")
            super().add(frame)

    class BoundedSocket(websocket.WebSocket):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.frame_buffer = BoundedBuffer(self._recv, False)
            self.cont_frame = BoundedContinuation(False, False)

        def _recv(self, size):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TeamsCalendarError("Teams 予定表の読み取りが時間切れになりました。")
                if self.sock is not None:
                    self.sock.settimeout(min(remaining, 5.0))
            return super()._recv(size)

    return BoundedSocket


def _allowed_origin(url: str, *, calendar: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None):
            return False
        if parsed.hostname in _TEAMS_HOSTS:
            return True
        return bool(calendar and parsed.hostname in _OUTLOOK_HOSTS
                    and re.match(r"^/(?:hosted/)?calendar(?:/|$)", parsed.path, re.I))
    except (TypeError, ValueError):
        return False


# These are semantic observations, not undocumented Teams-specific selectors.
# Attribute values are used only if they actually exist. A Today button alone
# NEVER proves which date the calendar displays. No event card is clicked.
_SNAPSHOT_JS = r"""(() => {
  const visible = e => {
    if (!e || !e.isConnected) return false;
    for (let p=e; p; p=p.parentElement) {
      const s=getComputedStyle(p);
      if (p.getAttribute('aria-hidden')==='true' || s.display==='none' || s.visibility==='hidden') return false;
    }
    const r=e.getBoundingClientRect();
    return r.width>0 && r.height>0 && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
  };
  const text=e => ((typeof e.innerText==='string' ? e.innerText : e.textContent) || '').trim().slice(0, 8000);
  const name=e => (e.getAttribute('aria-label') || e.getAttribute('title') || text(e)).trim();
  const norm=s => s.replace(/\s+/g,' ').trim();
  const controls=[...document.querySelectorAll('button,[role="button"],[role="tab"],[role="menuitemradio"],[role="option"]')].filter(visible);
  const today=controls.some(e => /^(Today|今日|本日)$/i.test(norm(name(e))));
  const calendarURL=/^https:\/\/outlook\.(?:office\.com|office365\.com|cloud\.microsoft)\/(?:hosted\/)?calendar(?:\/|$)/i.test(location.href);
  const grid=[...document.querySelectorAll('[role="grid"],[role="list"],[role="region"],[role="main"],main')].some(e => visible(e) && /calendar|予定表|カレンダー/i.test(name(e).slice(0,200)));
  const calendarPresent=calendarURL || (today && grid);
  const outsidePicker=e => !e.closest('[role="dialog"],[role="navigation"],nav,aside');
  const headings=[...document.querySelectorAll('h1,h2,h3,[role="heading"],[role="columnheader"]')].filter(e=>visible(e)&&outsidePicker(e));
  const dateTexts=headings.flatMap(e => [name(e), e.getAttribute('data-date') || '', e.getAttribute('datetime') || '']).filter(Boolean).slice(0,200);
  const dayColumns=headings.filter(e=>e.getAttribute('role')==='columnheader').map(name);
  const dayView=controls.some(e => /^(Day|日|日表示)$/i.test(norm(name(e))) && (e.getAttribute('aria-pressed')==='true' || e.getAttribute('aria-selected')==='true' || e.getAttribute('aria-checked')==='true'));
  const range=/\d{1,2}(?::\d{2}|時).*?(?:to|until|から|[〜～–—-]).*?\d{1,2}(?::\d{2}|時)/i;
  const candidates=calendarPresent ? [...document.querySelectorAll('[data-event-id],[data-item-id],[data-appointment-id],[data-start],[data-start-time],[aria-label],[role="listitem"],[role="gridcell"],button,a')].slice(0,10000) : [];
  const events=[]; const seen=new Set();
  const eventRoot='[data-event-id],[data-item-id],[data-appointment-id],[data-start],[data-start-time]';
  for (const e of candidates) {
    if (!visible(e)) continue;
    const root=e.closest(eventRoot);
    if (root && root!==e) continue;
    if (e.querySelector(eventRoot)) continue;
    const label=name(e), body=text(e);
    const key=e.getAttribute('data-event-id') || e.getAttribute('data-item-id') || e.getAttribute('data-appointment-id');
    const startTime=e.querySelector('time[itemprop="startDate"][datetime]');
    const endTime=e.querySelector('time[itemprop="endDate"][datetime]');
    const start=e.getAttribute('data-start') || e.getAttribute('data-start-time') || (startTime && startTime.getAttribute('datetime'));
    const end=e.getAttribute('data-end') || e.getAttribute('data-end-time') || (endTime && endTime.getAttribute('datetime'));
    if (!(key || start || end || range.test(label))) continue;
    if (label.length>1500 && !key) continue;
    const signature=[key,start,end,label].join('\0'); if (seen.has(signature)) continue; seen.add(signature);
    const links=e.matches('a[href]') ? [e] : [...e.querySelectorAll('a[href]')].filter(visible);
    const link=links.find(a=>/^https:\/\/(teams\.microsoft\.com|teams\.cloud\.microsoft|teams\.live\.com)\//i.test(a.href));
    events.push({key,title:e.getAttribute('data-subject'),label,text:body,start,end,date:e.getAttribute('data-date'),join_url:link ? link.href : null});
    if (events.length>=500) break;
  }
  const emptyPattern=/^(?:No (?:events|meetings)(?: scheduled| today| for today)?|There are no (?:events|meetings)(?: scheduled| today| for today)?|Nothing scheduled(?: today| for today)?|(?:今日|本日)の(?:予定|イベント|会議)はありません|(?:今日|本日)は(?:予定|イベント|会議)がありません|(?:予定|イベント|会議)(?:は|が)ありません)[.!。]?$/i;
  const empty=calendarPresent ? [...document.querySelectorAll('[role="status"],p,span,div')].slice(0,15000).find(e=>visible(e) && outsidePicker(e) && e.closest('[role="grid"],[role="main"],main') && e.children.length===0 && emptyPattern.test(text(e))) : null;
  return {url:location.href,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,calendar_present:calendarPresent,date_texts:dateTexts,day_column_texts:dayColumns,day_view:dayView,empty_text:empty ? text(empty) : '',events,truncated:candidates.length>=10000 || events.length>=500};
})()"""

_VISIBLE_FRAME_JS = """function() {
  for (let p=this;p;p=p.parentElement) {
    const s=getComputedStyle(p);
    if(p.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden') return false;
  }
  const r=this.getBoundingClientRect();
  return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;
}"""


class CDPClient:
    """Small bounded CDP client. Browser socket supports flattened OOPIF sessions."""

    def __init__(self, endpoint: str, deadline: float, *, socket_factory=None, http_client=None):
        self.deadline = deadline
        self._serial = 0
        self._events: deque[dict] = deque(maxlen=2000)
        self._socket = None
        url = endpoint + "/json/version"
        if not validate_cdp_url(url, endpoint):
            raise TeamsCalendarError("Teams のローカル接続先を確認できません。")
        own_http = http_client is None
        client = http_client or httpx.Client(trust_env=False, follow_redirects=False)
        try:
            with client.stream("GET", url, timeout=self.remaining()) as response:
                if response.status_code != 200:
                    raise TeamsCalendarError("Teams の接続準備が完了していません。")
                data = bytearray()
                for chunk in response.iter_bytes():
                    self.remaining()
                    data.extend(chunk)
                    if len(data) > _MAX_MESSAGE:
                        raise TeamsCalendarError("Teams の接続情報が大きすぎます。")
                metadata = json.loads(data)
            websocket_url = metadata.get("webSocketDebuggerUrl", "")
            if not validate_cdp_url(websocket_url, endpoint, websocket=True):
                raise TeamsCalendarError("Teams の接続情報がローカル接続先と一致しません。")
            if socket_factory is None:
                import websocket

                def socket_factory(url, **kwargs):
                    return websocket.create_connection(url, class_=_bounded_socket_class(self.deadline), **kwargs)
            self._socket = socket_factory(
                websocket_url, timeout=self.remaining(), suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost"], enable_multithread=True,
            )
        finally:
            if own_http:
                client.close()

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TeamsCalendarError("Teams 予定表の読み取りが時間切れになりました。保存済みの予定は保持されます。")
        return min(remaining, 5.0)

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._serial += 1
        request = {"id": self._serial, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self._socket.settimeout(self.remaining())
        self._socket.send(json.dumps(request))
        while True:
            self._socket.settimeout(self.remaining())
            raw = self._socket.recv()
            if not isinstance(raw, (str, bytes)) or len(raw) > _MAX_MESSAGE:
                raise TeamsCalendarError("Teams から読み取った画面情報を処理できません。")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise TeamsCalendarError("Teams の接続応答を確認できません。")
            if message.get("id") == self._serial:
                if "error" in message:
                    raise TeamsCalendarError("Teams の画面が変わったため読み取れませんでした。予定表を開いて再試行してください。")
                return message.get("result", {})
            self._remember_event(message)

    def _remember_event(self, message: dict) -> None:
        # Runtime.enable also emits console/exception data. Never retain it:
        # only the small identifiers needed to locate visible frames belong in
        # this queue, not arbitrary application logs or page values.
        method = message.get("method")
        session = message.get("sessionId")
        params = message.get("params", {})
        if not isinstance(session, str) or len(session) > 160 or not isinstance(params, dict):
            return
        retained = None
        if method == "Runtime.executionContextCreated":
            context = params.get("context", {})
            aux = context.get("auxData", {})
            frame_id = aux.get("frameId")
            if isinstance(frame_id, str) and len(frame_id) <= 160 and type(context.get("id")) is int:
                retained = {"context": {"id": context["id"], "auxData": {
                    "frameId": frame_id, "isDefault": aux.get("isDefault") is True,
                }}}
        elif method == "Runtime.executionContextDestroyed":
            if type(params.get("executionContextId")) is int:
                retained = {"executionContextId": params["executionContextId"]}
        elif method == "Runtime.executionContextsCleared":
            retained = {}
        elif method == "Target.attachedToTarget":
            target, child_session = params.get("targetInfo", {}), params.get("sessionId")
            if (target.get("type") == "iframe" and isinstance(child_session, str)
                    and len(child_session) <= 160 and isinstance(target.get("targetId"), str)
                    and len(target["targetId"]) <= 160 and isinstance(target.get("url"), str)
                    and len(target["url"]) <= 4096):
                retained = {"sessionId": child_session, "targetInfo": {
                    "targetId": target["targetId"], "type": "iframe", "url": target["url"],
                }}
        elif method == "Target.detachedFromTarget":
            if isinstance(params.get("sessionId"), str) and len(params["sessionId"]) <= 160:
                retained = {"sessionId": params["sessionId"]}
        if retained is not None:
            self._events.append({"method": method, "sessionId": session, "params": retained})

    def contexts(self, session: str) -> dict[str, int]:
        result = {}
        for event in self._events:
            if event.get("sessionId") != session:
                continue
            if event.get("method") == "Runtime.executionContextsCleared":
                result.clear()
            if event.get("method") == "Runtime.executionContextCreated":
                context = event.get("params", {}).get("context", {})
                aux = context.get("auxData", {})
                if aux.get("isDefault") and aux.get("frameId"):
                    result[aux["frameId"]] = context["id"]
            if event.get("method") == "Runtime.executionContextDestroyed":
                dead = event.get("params", {}).get("executionContextId")
                result = {frame: context for frame, context in result.items() if context != dead}
        return result

    def children(self, session: str) -> list[dict]:
        """Only targets whose parent session reported an automatic attachment."""
        children = {}
        for event in self._events:
            if event.get("sessionId") != session:
                continue
            params = event.get("params", {})
            if event.get("method") == "Target.attachedToTarget":
                child = params.get("targetInfo", {})
                if child.get("type") == "iframe" and params.get("sessionId"):
                    children[params["sessionId"]] = {**child, "session": params["sessionId"]}
            elif event.get("method") == "Target.detachedFromTarget":
                children.pop(params.get("sessionId"), None)
        return list(children.values())

    def evaluate(self, expression: str, session: str, context: int) -> object:
        response = self.call("Runtime.evaluate", {
            "expression": expression, "contextId": context, "returnByValue": True,
            "awaitPromise": True, "timeout": int(self.remaining() * 1000),
            "allowUnsafeEvalBlockedByCSP": False,
        }, session)
        if response.get("exceptionDetails"):
            raise TeamsCalendarError("Teams の画面情報を安全に読み取れません。予定表を開いて再試行してください。")
        return response.get("result", {}).get("value")

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket:
            try:
                socket.abort()
                socket.close(timeout=0)
            except Exception:
                pass


class _CalendarReader:
    def __init__(self, client: CDPClient):
        self.client = client
        self.sessions: dict[str, str] = {}
        self.warnings: list[str] = []

    def attach(self, target: str, existing_session: str | None = None) -> str:
        if target not in self.sessions:
            session = existing_session or self.client.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.sessions[target] = session
            self.client.call("Runtime.enable", session=session)
            self.client.call("Page.enable", session=session)
            # Page.getFrameTree omits OOPIF children in current Chromium. The
            # attachment event proves parentage without trusting unrelated tabs.
            self.client.call("Target.setAutoAttach", {
                "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
                "filter": [{"type": "iframe"}, {"exclude": True}],
            }, session)
        return self.sessions[target]

    def _frame_visible(self, frame_id: str, parent_session: str) -> bool:
        obj = None
        try:
            node = self.client.call("DOM.getFrameOwner", {"frameId": frame_id}, parent_session)
            obj = self.client.call("DOM.resolveNode", {"backendNodeId": node["backendNodeId"]}, parent_session)["object"]["objectId"]
            result = self.client.call("Runtime.callFunctionOn", {
                "objectId": obj, "functionDeclaration": _VISIBLE_FRAME_JS, "returnByValue": True,
            }, parent_session)
            return result.get("result", {}).get("value") is True
        except (TeamsCalendarError, KeyError):
            return False
        finally:
            if obj:
                try:
                    self.client.call("Runtime.releaseObject", {"objectId": obj}, parent_session)
                except TeamsCalendarError:
                    pass

    def frames(self) -> list[dict]:
        targets = self.client.call("Target.getTargets").get("targetInfos", [])
        target_ids = {target.get("targetId") for target in targets if target.get("type") == "iframe"}
        roots = [target for target in targets if target.get("type") == "page" and _allowed_origin(target.get("url", ""))]
        frames = []
        seen = set()
        for root in roots[:4]:
            # Meeting popout pages are never navigation targets or calendars.
            if re.search(r"/(?:v2/)?(?:meeting|meetup-join|calling)(?:/|$)", urlsplit(root["url"]).path, re.I):
                continue
            session = self.attach(root["targetId"])
            tree = self.client.call("Page.getFrameTree", session=session).get("frameTree", {})

            def visit(node: dict, parent_session: str, depth: int = 0) -> None:
                if depth > 8 or len(frames) >= 30:
                    return
                frame = node.get("frame", {})
                frame_id, url = frame.get("id"), frame.get("url", "")
                if not frame_id or frame_id in seen or not _allowed_origin(url, calendar=depth > 0):
                    return
                if depth and not self._frame_visible(frame_id, parent_session):
                    return
                seen.add(frame_id)
                frame_session = self.attach(frame_id, node.get("_session")) if frame_id in target_ids or node.get("_session") else parent_session
                context = self.client.contexts(frame_session).get(frame_id)
                if context is not None:
                    frames.append({"session": frame_session, "context": context, "frame_id": frame_id, "url": url})
                # OOPIF's Page tree may contain descendants omitted by the root.
                branch = node
                if frame_session != parent_session:
                    branch = self.client.call("Page.getFrameTree", session=frame_session).get("frameTree", node)
                for child in branch.get("childFrames", []):
                    visit(child, frame_session, depth + 1)
                if depth == 0 or frame_session != parent_session:
                    for child in self.client.children(frame_session):
                        visit({"frame": {"id": child["targetId"], "url": child.get("url", "")},
                               "_session": child["session"]}, frame_session, depth + 1)

            visit(tree, session)
        return frames

    def snapshot(self, frame: dict) -> dict:
        observed = self.client.evaluate(_SNAPSHOT_JS, frame["session"], frame["context"])
        if not isinstance(observed, dict) or not _allowed_origin(observed.get("url", ""), calendar=True):
            raise TeamsCalendarError("Teams 予定表の表示元を確認できません。")
        if not observed.get("calendar_present"):
            return observed
        if observed.get("truncated"):
            self.warnings.append("画面の予定候補が多いため、読み取れた範囲だけを表示します。")
        if not observed.get("events"):
            try:
                nodes = self.client.call("Accessibility.getFullAXTree", {"frameId": frame["frame_id"]}, frame["session"]).get("nodes", [])
                node_map = {node.get("nodeId"): node for node in nodes}
                for node in nodes[:15000]:
                    if node.get("ignored"):
                        continue
                    ancestor, parent_roles = node, []
                    for _ in range(50):
                        ancestor = node_map.get(ancestor.get("parentId"))
                        if ancestor is None:
                            break
                        parent_roles.append(ancestor.get("role", {}).get("value", "").lower())
                    if set(parent_roles) & {"dialog", "navigation", "complementary"}:
                        continue
                    name = node.get("name", {}).get("value", "")
                    role = node.get("role", {}).get("value", "").lower()
                    if not isinstance(name, str) or len(name) > 1500:
                        continue
                    if role in {"heading", "columnheader"}:
                        observed.setdefault("date_texts", []).append(name)
                    if role == "columnheader":
                        observed.setdefault("day_column_texts", []).append(name)
                    if role in {"button", "gridcell", "listitem", "link"} and _TIME_RANGE.search(name):
                        observed.setdefault("events", []).append({"label": name})
                    if "toolbar" in parent_roles and role in {"tab", "button", "menuitemradio", "option"} and name in {"Day", "日", "日表示"}:
                        if any(prop.get("name") in {"selected", "pressed", "checked"} and prop.get("value", {}).get("value") in {True, "true"} for prop in node.get("properties", [])):
                            observed["day_view"] = True
            except TeamsCalendarError:
                self.warnings.append("予定表のアクセシビリティ情報を取得できないため、画面の文字情報だけを使用しました。")
        return observed

    def read(self) -> list[dict]:
        frames = self.frames()
        observations = [(frame, self.snapshot(frame)) for frame in frames]
        calendars = [(frame, value) for frame, value in observations if value.get("calendar_present")]
        if not calendars:
            raise TeamsCalendarError("Teams のカレンダーを開いて、今日の予定を日表示にしてください。giji は Teams の画面を自動操作しません。")
        return [value for _, value in calendars]


class TeamsDesktopCalendar:
    def __init__(self, connection, *, timeout_seconds: float = 20, client_factory: Callable = CDPClient):
        self.connection = connection
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory
        self._lock = threading.Lock()
        self._active_client = None
        self._closed = False
        self._public = {"provider": "teams_desktop", "state": "not_synced", "connected": False,
                        "detail": "Teams の予定表はまだ読み取っていません。", "coverage": "unknown", "warnings": []}

    def status(self, settings: dict | None = None) -> dict:
        with self._lock:
            return {**self._public, "warnings": list(self._public["warnings"])}

    def fetch_today(self, settings: dict) -> list[dict]:
        client = None
        try:
            with self._lock:
                if self._closed:
                    raise TeamsCalendarError("Teams 予定表の接続は終了しています。")
            endpoint = self.connection.connect_endpoint()
            client = self.client_factory(endpoint, time.monotonic() + self.timeout_seconds)
            with self._lock:
                if self._closed:
                    raise TeamsCalendarError("Teams 予定表の接続は終了しています。")
                self._active_client = client
            reader = _CalendarReader(client)
            snapshots = reader.read()
            parsed = []
            for snapshot in snapshots:
                try:
                    parsed.append(parse_calendar_snapshot(snapshot))
                except CalendarParseError:
                    reader.warnings.append("読み取れない予定表の表示がありました。保存済みの予定は保持されます。")
            if not parsed:
                raise TeamsCalendarError("今日の会議日時を確認できませんでした。Teams で今日の予定表を日表示にしてください。保存済みの予定は保持されます。")
            meetings = {meeting["id"]: meeting for result in parsed for meeting in result["meetings"]}
            coverage = "complete" if len(parsed) == len(snapshots) == 1 and parsed[0]["coverage"] == "complete" and not reader.warnings else "visible_only"
            warnings = list(dict.fromkeys(reader.warnings + [warning for result in parsed for warning in result["warnings"]]))
            if coverage == "visible_only" and not warnings:
                warnings.append("画面で確認できた予定のみです。すべての予定を取得した保証はありません。")
            with self._lock:
                self._public.update(state="connected", connected=True, coverage=coverage, warnings=warnings,
                                    detail="Teams の画面に表示された今日の予定を取得しました。" if meetings else "今日の予定がないことを画面で確認しました。")
            return sorted(meetings.values(), key=lambda item: (item["start"], item["id"]))
        except Exception as exc:
            safe = str(exc) if isinstance(exc, (TeamsCalendarError, TeamsConnectionError)) else "Teams 予定表を読み取れませんでした。Teams のカレンダーを開いて再試行してください。"
            with self._lock:
                self._public.update(state="error", connected=False, detail=safe, coverage="unknown", warnings=[])
            raise TeamsCalendarError(safe) from None
        finally:
            if client:
                client.close()
            with self._lock:
                self._active_client = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            client = self._active_client
        if client:
            client.close()
