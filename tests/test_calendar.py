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
