"""Pure state transitions. Calendar end times deliberately never stop recording."""
from __future__ import annotations

from datetime import datetime, timedelta


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def match_meeting(meetings: list[dict], now: datetime, armed_id: str | None = None) -> dict | None:
    candidates = [
        m for m in meetings
        if not m.get("cancelled") and not m["id"].startswith("demo-")
        and parse_time(m["start"]) - timedelta(minutes=15) <= now
        <= parse_time(m["end"]) + timedelta(minutes=30)
    ]
    if armed_id:
        return next((m for m in candidates if m["id"] == armed_id), None)
    return candidates[0] if len(candidates) == 1 else None


class MeetingLifecycle:
    def __init__(self):
        self.idle_since: float | None = None
        self.seen_in_meeting = False
        self.suppressed = False

    def on_started(self, in_meeting: bool) -> None:
        self.seen_in_meeting = in_meeting
        self.idle_since = None

    def on_stopped(self) -> None:
        self.suppressed = True
        self.seen_in_meeting = False
        self.idle_since = None

    def step(self, state: str, active: bool, auto: bool, now: float, grace: float) -> str | None:
        if state in ("unknown", "unavailable"):
            self.idle_since = None
            return None
        if state == "in_meeting":
            self.idle_since = None
            if active:
                self.seen_in_meeting = True
            elif auto and not self.suppressed:
                return "start"
        elif state == "idle":
            if self.idle_since is None:
                self.idle_since = now
            if now - self.idle_since >= grace:
                self.suppressed = False
                if active and self.seen_in_meeting:
                    return "stop"
        return None
