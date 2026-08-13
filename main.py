"""Synchronise incomplete Alexa shopping-list items with Home Assistant.

The module keeps the one-shot ``main`` entry point used by earlier releases,
while the command-line entry point runs it periodically.  Network failures are
treated as transient; configuration and cookie-format errors are fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
from collections.abc import Mapping
from http import cookies as http_cookies
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5_1 like Mac OS X)"
        " AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        " PitanguiBridge/2.2.345247.0-[HARDWARE=iPhone10_4][SOFTWARE=13.5.1]"
    ),
    "Accept": "*/*",
    "Accept-Language": "*",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}
SAFE_RETRY_METHODS = frozenset({"GET", "PUT"})
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

logger = logging.getLogger(__name__)

# Python versions before 3.13 do not know the cookie flag emitted by Amazon.
if sys.version_info[:2] < (3, 13):
    http_cookies.Morsel._reserved["partitioned"] = "Partitioned"  # type: ignore[attr-defined]
    http_cookies.Morsel._flags.add("partitioned")  # type: ignore[attr-defined]


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


def configure_logging(level: str | None = None) -> None:
    """Configure logging and reject misspelled log levels early."""

    selected = (level if level is not None else os.getenv("LOG_LEVEL") or "INFO").upper()
    numeric_level = getattr(logging, selected, None)
    if not isinstance(numeric_level, int):
        raise ConfigurationError(f"Invalid LOG_LEVEL: {selected}")
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=numeric_level,
    )
    logging.getLogger().setLevel(numeric_level)


def _log_fatal_startup_error(error: BaseException) -> None:
    """Log a startup failure without re-validating the user's configuration."""

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.CRITICAL,
    )
    logging.getLogger().setLevel(logging.CRITICAL)
    logger.critical("Fatal startup error: %s", error)


def _positive_number(name: str, value: str, integer: bool = False) -> int | float:
    try:
        parsed: int | float = int(value) if integer else float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return parsed


def _validate_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    return value.rstrip("/")


def initialize_environment_variables(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read and validate runtime configuration.

    ``environ`` is injectable to make validation deterministic in tests.
    """

    values = environ if environ is not None else os.environ
    required = {name: values.get(name, "").strip() for name in (
        "HA_WEBHOOK_URL", "COOKIE_PATH", "AMAZON_URL"
    )}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigurationError(f"Missing required environment variables: {', '.join(missing)}")

    cookie_path = Path(required["COOKIE_PATH"]).expanduser()
    if not cookie_path.is_file():
        raise ConfigurationError(f"COOKIE_PATH does not point to a file: {cookie_path}")

    log_level = values.get("LOG_LEVEL", "INFO").upper().strip()
    if not isinstance(getattr(logging, log_level, None), int):
        raise ConfigurationError(f"Invalid LOG_LEVEL: {log_level}")

    return {
        "webhook_url": _validate_url("HA_WEBHOOK_URL", required["HA_WEBHOOK_URL"]),
        "cookie_path": str(cookie_path),
        "amazon_api_url": _validate_url("AMAZON_URL", required["AMAZON_URL"]),
        "poll_interval": _positive_number(
            "POLL_INTERVAL_SECONDS", values.get("POLL_INTERVAL_SECONDS", "60"), integer=True
        ),
        "connect_timeout": _positive_number(
            "HTTP_CONNECT_TIMEOUT_SECONDS", values.get("HTTP_CONNECT_TIMEOUT_SECONDS", "5")
        ),
        "read_timeout": _positive_number(
            "HTTP_READ_TIMEOUT_SECONDS", values.get("HTTP_READ_TIMEOUT_SECONDS", "30")
        ),
        "state_path": values.get("STATE_PATH", "state.sqlite3").strip() or "state.sqlite3",
        "heartbeat_path": values.get(
            "HEARTBEAT_PATH", "/tmp/alexa2ha-heartbeat"
        ).strip() or "/tmp/alexa2ha-heartbeat",
        "log_level": log_level,
    }


def load_cookies_from_file(cookie_file_path: str | os.PathLike[str]) -> dict[str, str] | None:
    """Load the JSON ``alexapy.cookies`` format; never deserialize pickle data."""

    try:
        with Path(cookie_file_path).open("r", encoding="utf-8") as cookie_file:
            loaded = json.load(cookie_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        logger.error("Failed to load JSON cookies from %s: %s", cookie_file_path, err)
        return None

    if not isinstance(loaded, dict) or loaded.get("format") != "alexapy.cookies":
        logger.error("Unsupported cookie format in %s", cookie_file_path)
        return None
    cookie_entries = loaded.get("cookies")
    if not isinstance(cookie_entries, list):
        logger.error("Invalid alexapy cookie entries in %s", cookie_file_path)
        return None

    result: dict[str, str] = {}
    for entry in cookie_entries:
        if not isinstance(entry, dict):
            continue
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and name and value is not None:
            result[name] = str(value)
    if not result:
        logger.error("No usable cookies found in %s", cookie_file_path)
        return None
    return result


def create_session() -> requests.Session:
    """Create one session with bounded retries for idempotent Amazon calls.

    The adapter will not retry the Home Assistant POST because retrying that
    operation could create a duplicate shopping-list item.
    """

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=SAFE_RETRY_METHODS,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _timeout(config: Mapping[str, Any] | None = None) -> tuple[float, float]:
    if config is None:
        return (5.0, 30.0)
    return (float(config.get("connect_timeout", 5)), float(config.get("read_timeout", 30)))


def add_item_to_shopping_list(
    webhook_url: str,
    item_name: str,
    session: requests.Session | None = None,
    timeout: tuple[float, float] | None = None,
) -> bool:
    if not item_name or not item_name.strip():
        logger.warning("Skipping shopping-list item with an empty name")
        return False
    client = session or create_session()
    try:
        response = client.post(
            webhook_url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"name": item_name},
            timeout=timeout or _timeout(),
        )
        response.raise_for_status()
    except requests.RequestException as err:
        error_response = getattr(err, "response", None)
        if error_response is not None and error_response.status_code in (401, 403):
            logger.error("Home Assistant rejected webhook authentication for item %r", item_name)
        else:
            logger.error("Error adding item %r: %s", item_name, err)
        return False
    logger.info("Successfully added item: %s", item_name)
    return True


def make_authenticated_request(
    url: str,
    cookie_file_path: str | os.PathLike[str],
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
    timeout: tuple[float, float] | None = None,
) -> requests.Response | None:
    method = method.upper()
    if method not in SAFE_RETRY_METHODS:
        raise ValueError(f"Invalid method {method}; expected GET or PUT")
    client = session or create_session()
    loaded_cookies = load_cookies_from_file(cookie_file_path)
    if not loaded_cookies:
        logger.error("No cookies loaded")
        return None
    client.cookies.update(loaded_cookies)
    try:
        response = client.request(
            method,
            url,
            json=payload if method == "PUT" else None,
            timeout=timeout or _timeout(),
        )
        response.raise_for_status()
        return response
    except requests.RequestException as err:
        error_response = getattr(err, "response", None)
        if error_response is not None and error_response.status_code in (401, 403):
            logger.error("Amazon authentication rejected (%s); refresh the JSON cookies", error_response.status_code)
        elif error_response is not None and error_response.status_code == 429:
            logger.warning("Amazon rate-limited the request; will retry on the next cycle")
        elif error_response is not None and error_response.status_code >= 500:
            logger.warning("Amazon server error %s; will retry on the next cycle", error_response.status_code)
        else:
            logger.error("HTTP request failed (%s %s): %s", method, url, err)
        return None


def extract_list_items(response_data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(response_data, dict):
        return None
    for value in response_data.values():
        if isinstance(value, dict) and "listItems" in value:
            items = value["listItems"]
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return None
    return None


def filter_incomplete_items(list_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not list_items:
        return []
    return [item for item in list_items if not item.get("completed", False)]


def _item_key(list_item: Mapping[str, Any]) -> str | None:
    for field in ("id", "itemId", "listItemId", "item_id"):
        value = list_item.get(field)
        if value is not None and str(value).strip():
            return f"{field}:{value}"
    value = list_item.get("value")
    if isinstance(value, str) and value.strip():
        digest = hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()
        return f"value:{digest}"
    return None


class StateStore:
    """Small durable registry preventing HA duplicates across restarts."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS delivered_items (
                item_key TEXT PRIMARY KEY,
                item_name TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.connection.commit()

    def is_ha_delivered(self, item_key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM delivered_items WHERE item_key = ?", (item_key,)
        ).fetchone()
        return row is not None

    def mark_ha_delivered(self, item_key: str, item_name: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO delivered_items(item_key, item_name) VALUES (?, ?)",
            (item_key, item_name),
        )
        self.connection.commit()

    def remove(self, item_key: str) -> None:
        self.connection.execute("DELETE FROM delivered_items WHERE item_key = ?", (item_key,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def mark_item_as_completed(
    amazon_api_url: str,
    cookie_file_path: str | os.PathLike[str],
    list_item: Mapping[str, Any],
    session: requests.Session | None = None,
    timeout: tuple[float, float] | None = None,
) -> bool:
    url = f"{amazon_api_url.rstrip('/')}/alexashoppinglists/api/updatelistitem"
    payload = dict(list_item)
    payload["completed"] = True
    response = make_authenticated_request(
        url, cookie_file_path, method="PUT", payload=payload, session=session, timeout=timeout
    )
    if response is not None:
        logger.info("Item marked as completed: %s", list_item.get("value", "unknown"))
        return True
    logger.error("Failed to update item: %s", list_item.get("value", "unknown"))
    return False


def run_cycle(config: Mapping[str, Any], state: StateStore, session: requests.Session) -> bool:
    """Run one poll/forward/complete cycle and report overall success."""

    timeout = _timeout(config)
    list_items_url = f"{str(config['amazon_api_url']).rstrip('/')}/alexashoppinglists/api/getlistitems"
    response = make_authenticated_request(
        list_items_url, str(config["cookie_path"]), session=session, timeout=timeout
    )
    if response is None:
        return False
    try:
        response_data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as err:
        logger.error("Amazon returned invalid JSON: %s", err)
        return False

    list_items = extract_list_items(response_data)
    if list_items is None:
        logger.warning("Amazon response did not contain a valid listItems array")
        return False

    cycle_ok = True
    for item in filter_incomplete_items(list_items):
        name = item.get("value")
        key = _item_key(item)
        if not isinstance(name, str) or not name.strip() or key is None:
            logger.warning("Skipping malformed shopping-list item")
            continue
        if not state.is_ha_delivered(key):
            if not add_item_to_shopping_list(
                str(config["webhook_url"]), name, session=session, timeout=timeout
            ):
                cycle_ok = False
                continue
            state.mark_ha_delivered(key, name)
        if mark_item_as_completed(
            str(config["amazon_api_url"]), str(config["cookie_path"]), item,
            session=session, timeout=timeout
        ):
            state.remove(key)
        else:
            cycle_ok = False
    return cycle_ok


def touch_heartbeat(path: str | os.PathLike[str]) -> None:
    heartbeat = Path(path).expanduser()
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def main(
    config: Mapping[str, Any] | None = None,
    state: StateStore | None = None,
    session: requests.Session | None = None,
) -> bool:
    """Run one cycle.  Returns false for transient upstream failures."""

    runtime = dict(config or initialize_environment_variables())
    configure_logging(str(runtime.get("log_level", "INFO")))
    own_state = state is None
    own_session = session is None
    store = state or StateStore(str(runtime["state_path"]))
    client = session or create_session()
    try:
        succeeded = run_cycle(runtime, store, client)
        if succeeded:
            touch_heartbeat(str(runtime["heartbeat_path"]))
        return succeeded
    finally:
        if own_state:
            store.close()
        if own_session:
            client.close()


def run_forever(interval: float | None = None) -> None:
    config = initialize_environment_variables()
    configure_logging(config["log_level"])
    period = interval if interval is not None else config["poll_interval"]
    if float(period) <= 0:
        raise ConfigurationError("interval must be positive")

    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        logger.info("Shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with StateStore(str(config["state_path"])) as state, create_session() as session:
        while not stop_event.is_set():
            try:
                main(config, state=state, session=session)
            except Exception:
                logger.exception("Unhandled exception during synchronization cycle")
            stop_event.wait(float(period))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize Alexa and Home Assistant shopping lists")
    parser.add_argument(
        "--interval", type=float, default=None,
        help="Interval in seconds (default: POLL_INTERVAL_SECONDS or 60)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    try:
        run_forever(arguments.interval)
    except (ConfigurationError, OSError, sqlite3.Error) as err:
        _log_fatal_startup_error(err)
        raise SystemExit(1) from err
