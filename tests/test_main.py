import json
import logging
import signal
import sqlite3
from pathlib import Path
from typing import Self

import pytest
import requests

import main


def write_cookies(path: Path, value: str = "token") -> Path:
    path.write_text(
        json.dumps({
            "format": "alexapy.cookies",
            "cookies": [{"name": "session", "value": value}],
        }),
        encoding="utf-8",
    )
    return path


def test_load_json_alexapy_cookies_and_reject_pickle(tmp_path: Path) -> None:
    cookie_path = write_cookies(tmp_path / "cookies.json")
    assert main.load_cookies_from_file(cookie_path) == {"session": "token"}

    pickle_path = tmp_path / "cookies.pickle"
    pickle_path.write_bytes(b"\x80\x04fake-pickle")
    assert main.load_cookies_from_file(pickle_path) is None


def test_config_validation_defaults_and_missing(tmp_path: Path) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")
    config = main.initialize_environment_variables({
        "HA_WEBHOOK_URL": "http://ha.local/hook",
        "AMAZON_URL": "https://amazon.example",
        "COOKIE_PATH": str(cookies),
    })
    assert config["poll_interval"] == 60
    assert config["connect_timeout"] == 5
    assert config["read_timeout"] == 30

    with pytest.raises(main.ConfigurationError, match="Missing required"):
        main.initialize_environment_variables({})
    with pytest.raises(main.ConfigurationError, match="positive"):
        main.initialize_environment_variables({
            "HA_WEBHOOK_URL": "http://ha.local/hook",
            "AMAZON_URL": "https://amazon.example",
            "COOKIE_PATH": str(cookies),
            "POLL_INTERVAL_SECONDS": "0",
        })


def test_config_rejects_invalid_log_level_urls_and_cookie_paths(tmp_path: Path) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")
    base = {
        "HA_WEBHOOK_URL": "http://ha.local/hook",
        "AMAZON_URL": "https://amazon.example",
        "COOKIE_PATH": str(cookies),
    }

    with pytest.raises(main.ConfigurationError, match="LOG_LEVEL"):
        main.initialize_environment_variables({**base, "LOG_LEVEL": "NOT_A_LEVEL"})
    with pytest.raises(main.ConfigurationError, match="HA_WEBHOOK_URL"):
        main.initialize_environment_variables({**base, "HA_WEBHOOK_URL": "not-a-url"})
    with pytest.raises(main.ConfigurationError, match="HA_WEBHOOK_URL"):
        main.initialize_environment_variables({**base, "HA_WEBHOOK_URL": "ftp://ha.local/hook"})
    with pytest.raises(main.ConfigurationError, match="AMAZON_URL"):
        main.initialize_environment_variables({**base, "AMAZON_URL": "not-a-url"})
    with pytest.raises(main.ConfigurationError, match="AMAZON_URL"):
        main.initialize_environment_variables({**base, "AMAZON_URL": "ftp://amazon.example"})
    with pytest.raises(main.ConfigurationError, match="COOKIE_PATH"):
        main.initialize_environment_variables({**base, "COOKIE_PATH": str(tmp_path / "missing.json")})
    with pytest.raises(main.ConfigurationError, match="COOKIE_PATH"):
        main.initialize_environment_variables({**base, "COOKIE_PATH": str(tmp_path)})


def test_fatal_startup_logging_does_not_revalidate_configuration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit, match="1"):
        try:
            raise main.ConfigurationError("Invalid LOG_LEVEL: NOT_A_LEVEL")
        except main.ConfigurationError as error:
            main._log_fatal_startup_error(error)
            raise SystemExit(1)
    assert "Fatal startup error" in caplog.text


def test_extract_and_filter_items() -> None:
    response = {"shoppingList": {"listItems": [
        {"id": "1", "value": "milk", "completed": False},
        {"id": "2", "value": "bread", "completed": True},
    ]}}
    items = main.extract_list_items(response)
    assert items is not None
    assert [item["value"] for item in main.filter_incomplete_items(items)] == ["milk"]
    assert main.extract_list_items({"shoppingList": {"listItems": "bad"}}) is None


class FakeResponse:
    def __init__(self, status: int = 200, data: object | None = None, json_error: bool = False):
        self.status_code = status
        self.data = data
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]

    def json(self) -> object:
        if self.json_error:
            raise ValueError("invalid JSON")
        return self.data


class FakeSession:
    def __init__(self, amazon_data: object):
        self.cookies: dict[str, str] = {}
        self.amazon_data = amazon_data
        self.posts: list[dict[str, object]] = []
        self.puts: list[dict[str, object]] = []
        self.fail_put = False

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        if method == "GET":
            return FakeResponse(data=self.amazon_data)
        self.puts.append(kwargs["json"])  # type: ignore[arg-type]
        return FakeResponse(500 if self.fail_put else 200)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.posts.append(kwargs["json"])  # type: ignore[arg-type]
        return FakeResponse(200)


def test_state_store_prevents_ha_duplicate_after_amazon_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")
    config = {
        "webhook_url": "http://ha.local/hook",
        "amazon_api_url": "https://amazon.example",
        "cookie_path": str(cookies),
        "state_path": str(tmp_path / "state.sqlite3"),
        "heartbeat_path": str(tmp_path / "heartbeat"),
        "connect_timeout": 1,
        "read_timeout": 1,
        "log_level": "INFO",
    }
    data = {"shoppingList": {"listItems": [{"id": "abc", "value": "milk"}]}}
    session = FakeSession(data)
    session.fail_put = True
    with main.StateStore(str(config["state_path"])) as state:
        assert not main.run_cycle(config, state, session)  # type: ignore[arg-type]
        assert len(session.posts) == 1
        assert state.is_ha_delivered("id:abc")
        session.fail_put = False
        assert main.run_cycle(config, state, session)  # type: ignore[arg-type]
        assert len(session.posts) == 1
        assert len(session.puts) == 2
        assert not state.is_ha_delivered("id:abc")


def test_webhook_failure_does_not_complete_amazon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")
    config = {
        "webhook_url": "http://ha.local/hook",
        "amazon_api_url": "https://amazon.example",
        "cookie_path": str(cookies),
        "connect_timeout": 1,
        "read_timeout": 1,
    }
    state = main.StateStore(":memory:")
    session = FakeSession({"shoppingList": {"listItems": [{"id": "a", "value": "milk"}]}})
    monkeypatch.setattr(main, "add_item_to_shopping_list", lambda *args, **kwargs: False)
    assert not main.run_cycle(config, state, session)  # type: ignore[arg-type]
    assert not session.puts
    state.close()


def test_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "heartbeat"
    main.touch_heartbeat(path)
    assert path.exists()


def test_http_failures_are_bounded_and_use_timeout(tmp_path: Path) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")

    class FailingSession:
        def __init__(self, status: int):
            self.status = status
            self.cookies: dict[str, str] = {}
            self.timeout: object = None

        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            self.timeout = kwargs["timeout"]
            return FakeResponse(self.status)

    for status in (401, 403, 429, 500, 502, 503, 504):
        failing_session = FailingSession(status)
        assert main.make_authenticated_request(
            "https://amazon.example/list", cookies, session=failing_session, timeout=(1, 2)  # type: ignore[arg-type]
        ) is None
        assert failing_session.timeout == (1, 2)

    client = main.create_session()
    retries = client.get_adapter("https://").max_retries  # type: ignore[attr-defined]
    assert retries.total == 3
    assert retries.allowed_methods == main.SAFE_RETRY_METHODS
    client.close()


def test_webhook_timeout_is_not_retried(tmp_path: Path) -> None:
    class TimeoutSession:
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            raise requests.Timeout("webhook timed out")

    assert not main.add_item_to_shopping_list(
        "http://ha.local/hook", "milk", session=TimeoutSession(), timeout=(1, 2)  # type: ignore[arg-type]
    )


def test_add_item_ignores_empty_and_whitespace_names() -> None:
    class NoPostSession:
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError("post should not be called for an empty item")

    session = NoPostSession()
    for name in ("", "   ", "\t\n"):
        assert not main.add_item_to_shopping_list(
            "http://ha.local/hook", name, session=session, timeout=(1, 2)  # type: ignore[arg-type]
        )


def test_run_cycle_handles_invalid_json_empty_and_malformed_lists(tmp_path: Path) -> None:
    cookies = write_cookies(tmp_path / "cookies.json")
    config = {
        "webhook_url": "http://ha.local/hook",
        "amazon_api_url": "https://amazon.example",
        "cookie_path": str(cookies),
        "connect_timeout": 1,
        "read_timeout": 1,
    }

    class JsonSession(FakeSession):
        def __init__(self, response: FakeResponse):
            super().__init__({})
            self.response = response

        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            return self.response

    state = main.StateStore(":memory:")
    assert not main.run_cycle(config, state, JsonSession(FakeResponse(json_error=True)))  # type: ignore[arg-type]
    assert main.run_cycle(
        config, state, JsonSession(FakeResponse(data={"shoppingList": {"listItems": []}}))  # type: ignore[arg-type]
    )
    assert not main.run_cycle(
        config, state, JsonSession(FakeResponse(data={"shoppingList": {"listItems": "bad"}}))  # type: ignore[arg-type]
    )
    state.close()


def test_state_store_survives_restart_and_rejects_unavailable_path(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    state = main.StateStore(state_path)
    state.mark_ha_delivered("id:abc", "milk")
    state.close()
    reopened = main.StateStore(state_path)
    assert reopened.is_ha_delivered("id:abc")
    reopened.close()

    with pytest.raises(sqlite3.Error):
        main.StateStore(tmp_path)  # directory is not a SQLite database file


def test_sigterm_stops_polling_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "state_path": ":memory:",
        "log_level": "INFO",
        "poll_interval": 60,
    }
    handlers: dict[int, object] = {}

    class FakeEvent:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, period: float) -> bool:
            assert period == 60
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
            return self.stopped

    class FakeState:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(main, "initialize_environment_variables", lambda: config)
    monkeypatch.setattr(main.threading, "Event", FakeEvent)
    monkeypatch.setattr(main.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(main, "StateStore", lambda path: FakeState())
    monkeypatch.setattr(main, "create_session", lambda: FakeSession())
    cycles: list[object] = []
    monkeypatch.setattr(main, "main", lambda *args, **kwargs: cycles.append(True))

    main.run_forever()
    assert len(cycles) == 1
    assert signal.SIGTERM in handlers
