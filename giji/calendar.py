"""Explicit calendar providers; desktop sources never acquire Graph credentials."""

from __future__ import annotations

from typing import Callable

from .microsoft import CalendarError, MicrosoftCalendar


class CalendarRouter:
    def __init__(self, secrets, settings_provider: Callable[[], dict], *, desktop=None,
                 graph=None, teams=None, teams_uia=None, teams_connection=None):
        from .desktop_calendar import DesktopOutlookCalendar
        from .teams_calendar import TeamsDesktopCalendar
        from .teams_calendar_uia import TeamsUIACalendar

        self.settings_provider = settings_provider
        self.desktop = desktop or DesktopOutlookCalendar()
        self.graph = graph or MicrosoftCalendar(secrets)
        self.teams = teams or TeamsDesktopCalendar(teams_connection)
        self.teams_uia = teams_uia or TeamsUIACalendar()

    def _teams_reader(self, settings: dict):
        return self.teams if settings.get("teams_calendar_mode", "uia") == "cdp" else self.teams_uia

    def _source(self) -> str:
        return self.settings_provider().get("calendar_source", "teams_desktop")

    def is_connected(self) -> bool:
        if self._source() == "graph":
            return self.graph.is_connected()
        if self._source() == "teams_desktop":
            return bool(self._teams_reader(self.settings_provider()).status().get("connected"))
        return self.desktop.status(self.settings_provider()).get("state") == "ready"

    def status(self) -> dict:
        if self._source() == "teams_desktop":
            return self._teams_reader(self.settings_provider()).status()
        return {"coverage": "complete", "warnings": []}

    def fetch_today(self, settings: dict) -> list[dict]:
        if settings.get("calendar_source", "teams_desktop") == "graph":
            return self.graph.fetch_today(settings)
        from .desktop_calendar import DesktopCalendarError
        from .teams_calendar import TeamsCalendarError
        from .teams_calendar_uia import TeamsUIACalendarError

        try:
            if settings.get("calendar_source", "teams_desktop") == "teams_desktop":
                return self._teams_reader(settings).fetch_today(settings)
            return self.desktop.fetch_today(settings)
        except (DesktopCalendarError, TeamsCalendarError, TeamsUIACalendarError) as exc:
            raise CalendarError(str(exc)) from None

    def begin_login(self, settings: dict) -> dict:
        if settings.get("calendar_source", "teams_desktop") != "graph":
            raise ValueError("デスクトップ連携ではMicrosoftへの追加サインインは不要です。カレンダーを開いて予定を同期してください。")
        return self.graph.begin_login(settings)

    def complete_login(self) -> bool:
        return self.graph.complete_login()

    def cancel_login(self) -> None:
        self.graph.cancel_login()

    def logout(self) -> None:
        self.graph.logout()

    def close(self) -> None:
        self.graph.cancel_login()
        self.desktop.close()
        self.teams.close()
        self.teams_uia.close()
