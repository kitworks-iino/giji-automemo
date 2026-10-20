"""Calendar source selection; desktop Outlook never acquires Graph credentials."""

from __future__ import annotations

from typing import Callable

from .microsoft import CalendarError, MicrosoftCalendar


class CalendarRouter:
    def __init__(self, secrets, settings_provider: Callable[[], dict], *, desktop=None, graph=None):
        from .desktop_calendar import DesktopOutlookCalendar

        self.settings_provider = settings_provider
        self.desktop = desktop or DesktopOutlookCalendar()
        self.graph = graph or MicrosoftCalendar(secrets)

    def _source(self) -> str:
        return self.settings_provider().get("calendar_source", "outlook_desktop")

    def is_connected(self) -> bool:
        if self._source() == "graph":
            return self.graph.is_connected()
        return self.desktop.status(self.settings_provider()).get("state") == "ready"

    def fetch_today(self, settings: dict) -> list[dict]:
        if settings.get("calendar_source", "outlook_desktop") == "graph":
            return self.graph.fetch_today(settings)
        from .desktop_calendar import DesktopCalendarError

        try:
            return self.desktop.fetch_today(settings)
        except DesktopCalendarError as exc:
            raise CalendarError(str(exc)) from None

    def begin_login(self, settings: dict) -> dict:
        if settings.get("calendar_source", "outlook_desktop") != "graph":
            raise ValueError("Outlookデスクトップ連携ではサインイン操作は不要です。予定を同期してください。")
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
