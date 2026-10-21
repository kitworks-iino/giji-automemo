from unittest.mock import Mock

import pytest

from giji.calendar import CalendarRouter


def test_desktop_source_never_uses_graph_credentials():
    graph = Mock()
    desktop = Mock()
    settings = {"calendar_source": "outlook_desktop"}
    router = CalendarRouter(Mock(), lambda: settings, desktop=desktop, graph=graph)
    desktop.fetch_today.return_value = [{"id": "local"}]
    assert router.fetch_today(settings) == [{"id": "local"}]
    assert not graph.mock_calls
    with pytest.raises(ValueError, match="サインイン"):
        router.begin_login(settings)
    assert not graph.mock_calls


def test_desktop_failure_never_falls_back_to_graph():
    from giji.desktop_calendar import DesktopCalendarError
    from giji.microsoft import CalendarError

    graph, desktop = Mock(), Mock()
    settings = {"calendar_source": "outlook_desktop"}
    desktop.fetch_today.side_effect = DesktopCalendarError("Outlookを起動してください。")
    router = CalendarRouter(Mock(), lambda: settings, desktop=desktop, graph=graph)
    with pytest.raises(CalendarError, match="Outlook"):
        router.fetch_today(settings)
    assert not graph.mock_calls


def test_explicit_graph_selection_remains_available():
    settings = {"calendar_source": "graph"}
    graph, desktop = Mock(), Mock()
    graph.fetch_today.return_value = [{"id": "remote"}]
    router = CalendarRouter(Mock(), lambda: settings, desktop=desktop, graph=graph)
    assert router.fetch_today(settings) == [{"id": "remote"}]
    assert not desktop.mock_calls


def test_teams_default_never_touches_outlook_or_graph():
    settings = {}
    graph, desktop, teams = Mock(), Mock(), Mock()
    teams.fetch_today.return_value = [{"id": "screen-event"}]
    router = CalendarRouter(Mock(), lambda: settings, desktop=desktop, graph=graph, teams_uia=teams)
    assert router.fetch_today(settings) == [{"id": "screen-event"}]
    assert not graph.mock_calls
    assert not desktop.mock_calls


def test_teams_read_failure_is_public_and_never_falls_back():
    from giji.microsoft import CalendarError
    from giji.teams_calendar_uia import TeamsUIACalendarError

    graph, desktop, teams = Mock(), Mock(), Mock()
    teams.fetch_today.side_effect = TeamsUIACalendarError("Teamsのカレンダーを開いてください。")
    router = CalendarRouter(Mock(), lambda: {}, desktop=desktop, graph=graph, teams_uia=teams)
    with pytest.raises(CalendarError, match="Teams"):
        router.fetch_today({})
    assert not graph.mock_calls
    assert not desktop.mock_calls


def test_teams_cdp_is_only_used_after_explicit_mode_selection():
    uia, cdp = Mock(), Mock()
    settings = {"calendar_source": "teams_desktop", "teams_calendar_mode": "uia"}
    uia.fetch_today.return_value = [{"id": "visible-uia"}]
    cdp.fetch_today.return_value = [{"id": "visible-cdp"}]
    router = CalendarRouter(Mock(), lambda: settings, teams=cdp, teams_uia=uia)
    assert router.fetch_today(settings) == [{"id": "visible-uia"}]
    assert not cdp.mock_calls
    settings["teams_calendar_mode"] = "cdp"
    assert router.fetch_today(settings) == [{"id": "visible-cdp"}]
    assert uia.fetch_today.call_count == 1
