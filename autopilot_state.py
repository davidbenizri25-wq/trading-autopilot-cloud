"""Durable, local personal state for Trading Autopilot.

The store is intentionally small and dependency-free.  It is designed for a
single Streamlit process, where several reruns or sessions may touch the same
file from different threads.  It does not hold provider configuration and
rejects values that look like credentials before anything is written.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 2_000_000
MAX_STRING_LENGTH = 65_536
MAX_TREE_DEPTH = 12
MAX_CONTAINER_ITEMS = 5_000
MAX_WATCHLIST = 250
MAX_RECENT_SEARCHES = 50
MAX_SAVED_PLANS = 250
MAX_POSITIONS = 250
MAX_ALERT_EVENTS = 500
MAX_JOURNAL_ENTRIES = 2_000
MAX_CALIBRATION_RESULTS = 500
MAX_LAST_ANALYSES = 100
MAX_STATE_CHANGES = 1_000

SETUP_STATES = frozenset({"BLOCKED", "FORMING", "ARMED", "ENTER", "EXTENDED", "INVALIDATED"})
MONITORED_SETUP_STATES = frozenset({"FORMING", "ARMED", "ENTER", "EXTENDED", "INVALIDATED"})

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?$")
_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|apikey|secret|password|passwd|token|access_?token|accesstoken|"
    r"refresh_?token|refreshtoken|auth_?token|authtoken|authorization|credential|credentials|"
    r"private_?key|privatekey|client_?(?:secret|key)|clientsecret|clientkey|"
    r"provider_?key|providerkey|polygon_?key|polygonkey|openai_?key|openaikey|"
    r"account_?number|accountnumber)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk|ghp|github_pat|xox[baprs])-[_A-Za-z0-9-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:API[_ -]?KEY|ACCESS[_ -]?TOKEN|CLIENT[_ -]?SECRET|PASSWORD)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


class StateError(ValueError):
    """Base class for state validation errors."""


class StatePathError(StateError):
    """Raised when a state path is not safe to use."""


class InvalidTickerError(StateError):
    """Raised when a ticker does not match the supported conservative syntax."""


class StateSizeError(StateError):
    """Raised when state exceeds a configured safety bound."""


class SecretDataError(StateError):
    """Raised when a payload contains a credential-like key or value."""


class UnsupportedSchemaError(StateError):
    """Raised instead of overwriting state created by a newer schema."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_state(*, timestamp: str | None = None) -> dict[str, Any]:
    """Return a fresh state document using the current versioned schema."""

    now = timestamp or _utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "profile": {
            "directions": ["bullish", "bearish"],
            "focus": ["swing_trades", "long_options"],
            "entry_style": "pre_confirmation_with_guardrails",
        },
        "chart_preferences": {},
        "preferred_timeframes": ["1W", "1D", "4H", "15m", "5m"],
        "default_trade_horizon": "swing",
        "watchlist": [],
        "recent_searches": [],
        "saved_plans": {},
        "positions": {},
        "alerts": {"enabled": {}, "events": []},
        "journal": [],
        "calibration": {"results": [], "updated_at": None},
        "ui_preferences": {"mode": "beginner"},
        "last_analyses": {},
        "monitoring": {},
        "state_changes": [],
        "activity_log": [],
    }


create_default_state = default_state


def normalize_ticker(value: Any) -> str:
    """Normalize and validate a US-style ticker symbol."""

    if not isinstance(value, str):
        raise InvalidTickerError("Ticker must be text.")
    ticker = value.strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidTickerError("Ticker contains unsupported characters or has an invalid length.")
    return ticker


def validate_ticker(value: Any) -> str:
    """Validate a ticker and return its normalized representation."""

    return normalize_ticker(value)


def is_valid_ticker(value: Any) -> bool:
    try:
        normalize_ticker(value)
    except InvalidTickerError:
        return False
    return True


def _normalise_setup_state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    state = value.strip().upper().replace(" ", "_")
    aliases = {
        "WAIT": "FORMING",
        "WAIT_FOR_CONFIRMATION": "ARMED",
        "PASSED": "INVALIDATED",
        "PASS": "INVALIDATED",
    }
    state = aliases.get(state, state)
    return state if state in SETUP_STATES else None


def _snapshot(value: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {"state": value}
    if not isinstance(value, Mapping):
        raise StateError("Monitoring snapshots must be mappings or setup-state strings.")
    return copy.deepcopy(dict(value))


def _first_number(source: Mapping[str, Any], names: Iterable[str]) -> float | None:
    for name in names:
        value = source.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _direction(source: Mapping[str, Any]) -> str:
    value = str(source.get("direction") or source.get("bias") or "bullish").strip().lower()
    return "bearish" if value.startswith(("bear", "short", "down")) else "bullish"


def _targets(source: Mapping[str, Any]) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    raw_targets = source.get("targets")
    if isinstance(raw_targets, Mapping):
        candidates = list(raw_targets.items())
    elif isinstance(raw_targets, (list, tuple)):
        candidates = [(f"target_{index + 1}", value) for index, value in enumerate(raw_targets)]
    elif raw_targets is not None:
        candidates = [("target", raw_targets)]
    else:
        candidates = []
    candidates.extend(
        (name, source[name])
        for name in ("target", "target_1", "target_2", "target_3")
        if name in source
    )
    seen: set[tuple[str, float]] = set()
    for label, value in candidates:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        key = (str(label), number)
        if math.isfinite(number) and key not in seen:
            found.append(key)
            seen.add(key)
    return found


def _explicit_hits(source: Mapping[str, Any]) -> set[str]:
    raw = source.get("targets_hit", source.get("hit_targets", []))
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raw = [raw]
    return {str(value).strip().lower() for value in raw if str(value).strip()}


def detect_state_changes(
    previous: str | Mapping[str, Any] | None,
    current: str | Mapping[str, Any],
    *,
    ticker: str | None = None,
) -> list[dict[str, Any]]:
    """Return only meaningful monitoring events between two snapshots.

    Ordinary price movement is ignored.  Events are emitted for transitions
    among FORMING, ARMED, ENTER, EXTENDED, and INVALIDATED, and when a target or
    invalidation is newly crossed.  BLOCKED is accepted as context, but is not
    itself treated as a monitored destination.
    """

    old = _snapshot(previous)
    new = _snapshot(current)
    symbol_source = ticker or new.get("ticker") or old.get("ticker")
    symbol = normalize_ticker(symbol_source) if symbol_source else None
    old_state = _normalise_setup_state(old.get("state") or old.get("setup_state"))
    new_state = _normalise_setup_state(new.get("state") or new.get("setup_state"))
    events: list[dict[str, Any]] = []

    if old_state and new_state and old_state != new_state and new_state in MONITORED_SETUP_STATES:
        event: dict[str, Any] = {
            "event_type": "state_change",
            "previous_state": old_state,
            "current_state": new_state,
            "transition": f"{old_state}_TO_{new_state}",
        }
        if symbol:
            event["ticker"] = symbol
        events.append(event)

    old_price = _first_number(old, ("current_price", "price", "close", "last"))
    new_price = _first_number(new, ("current_price", "price", "close", "last"))
    direction = _direction(new or old)

    targets = _targets(new) or _targets(old)
    old_hits = _explicit_hits(old)
    new_hits = _explicit_hits(new)
    emitted_target_labels: set[str] = set()
    for label, target in targets:
        label_key = label.lower()
        numeric_key = f"{target:g}".lower()
        explicitly_new = (label_key in new_hits or numeric_key in new_hits) and not (
            label_key in old_hits or numeric_key in old_hits
        )
        crossed = False
        if old_price is not None and new_price is not None:
            if direction == "bearish":
                crossed = old_price > target >= new_price
            else:
                crossed = old_price < target <= new_price
        if (explicitly_new or crossed) and label_key not in emitted_target_labels:
            event = {"event_type": "target_reached", "target": target, "target_label": label}
            if symbol:
                event["ticker"] = symbol
            if new_price is not None:
                event["price"] = new_price
            events.append(event)
            emitted_target_labels.add(label_key)

    invalidation = _first_number(new, ("invalidation", "invalidation_level", "invalid"))
    if invalidation is None:
        invalidation = _first_number(old, ("invalidation", "invalidation_level", "invalid"))
    explicitly_invalidated = bool(new.get("invalidation_hit") or new.get("invalidated"))
    was_explicitly_invalidated = bool(old.get("invalidation_hit") or old.get("invalidated"))
    state_invalidated = new_state == "INVALIDATED" and old_state != "INVALIDATED"
    invalidation_crossed = False
    if invalidation is not None and old_price is not None and new_price is not None:
        if direction == "bearish":
            invalidation_crossed = old_price < invalidation <= new_price
        else:
            invalidation_crossed = old_price > invalidation >= new_price
    if state_invalidated or (explicitly_invalidated and not was_explicitly_invalidated) or invalidation_crossed:
        event = {"event_type": "invalidation_reached"}
        if symbol:
            event["ticker"] = symbol
        if invalidation is not None:
            event["invalidation"] = invalidation
        if new_price is not None:
            event["price"] = new_price
        events.append(event)

    return events


detect_monitoring_events = detect_state_changes


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _safe_path(path: str | os.PathLike[str], allowed_root: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(path).expanduser()
    if any(part == ".." for part in candidate.parts):
        raise StatePathError("Parent-directory traversal is not allowed in state paths.")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.is_symlink():
        raise StatePathError("Symbolic-link state files are not allowed.")
    resolved = candidate.resolve(strict=False)
    if not resolved.name or resolved.suffix.lower() != ".json":
        raise StatePathError("State files must use a .json filename.")
    if allowed_root is not None:
        root = Path(allowed_root).expanduser().resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise StatePathError("State path is outside its allowed root.") from exc
    if resolved.exists() and not resolved.is_file():
        raise StatePathError("State path must refer to a regular file.")
    return resolved


def _secret_check(value: Any, *, path: str = "state", depth: int = 0) -> None:
    if depth > MAX_TREE_DEPTH:
        raise StateSizeError("State nesting is too deep.")
    if isinstance(value, Mapping):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise StateSizeError("A state object contains too many items.")
        for key, child in value.items():
            if not isinstance(key, str):
                raise StateError("State object keys must be text.")
            split_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
            normalised_key = re.sub(r"[^a-z0-9]+", "_", split_key.lower()).strip("_")
            if _SECRET_KEY_RE.search(normalised_key):
                raise SecretDataError("Credential-like fields cannot be persisted.")
            _secret_check(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise StateSizeError("A state list contains too many items.")
        for child in value:
            _secret_check(child, path=path, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise StateSizeError("A state string is too large.")
        if any(pattern.search(value) for pattern in _SECRET_VALUE_RES):
            raise SecretDataError("Credential-like values cannot be persisted.")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StateError("State numbers must be finite.")
        return
    raise StateError("State values must be JSON-compatible.")


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StateError(f"{name} must be an object.")
    return copy.deepcopy(dict(value))


def _require_list(value: Any, name: str, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise StateError(f"{name} must be a list.")
    if len(value) > limit:
        raise StateSizeError(f"{name} exceeds its item limit.")
    return copy.deepcopy(value)


def _trim_mapping(mapping: dict[str, Any], limit: int) -> None:
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))


def _normalise_document(document: Mapping[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(dict(document))
    version = source.get("schema_version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise StateError("schema_version must be a non-negative integer.")
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaError("This state file uses a newer schema version.")

    base = default_state(timestamp=str(source.get("created_at") or _utc_now()))
    base.update(source)
    base["schema_version"] = SCHEMA_VERSION
    revision = base.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise StateError("revision must be a non-negative integer.")

    for key in ("profile", "chart_preferences", "ui_preferences", "saved_plans", "positions", "last_analyses", "monitoring"):
        base[key] = _require_mapping(base.get(key), key)
    base["watchlist"] = _require_list(base.get("watchlist"), "watchlist", MAX_WATCHLIST)
    base["recent_searches"] = _require_list(
        base.get("recent_searches"), "recent_searches", MAX_RECENT_SEARCHES
    )
    base["journal"] = _require_list(base.get("journal"), "journal", MAX_JOURNAL_ENTRIES)
    base["state_changes"] = _require_list(
        base.get("state_changes"), "state_changes", MAX_STATE_CHANGES
    )
    base["activity_log"] = _require_list(
        base.get("activity_log"), "activity_log", MAX_STATE_CHANGES
    )

    timeframes = _require_list(base.get("preferred_timeframes"), "preferred_timeframes", 20)
    if not all(isinstance(item, str) and 0 < len(item.strip()) <= 16 for item in timeframes):
        raise StateError("preferred_timeframes contains an invalid value.")
    base["preferred_timeframes"] = list(dict.fromkeys(item.strip() for item in timeframes))
    horizon = base.get("default_trade_horizon")
    if not isinstance(horizon, str) or not horizon.strip() or len(horizon) > 64:
        raise StateError("default_trade_horizon must be short text.")
    base["default_trade_horizon"] = horizon.strip()

    watchlist: list[str] = []
    for value in base["watchlist"]:
        ticker = normalize_ticker(value)
        if ticker not in watchlist:
            watchlist.append(ticker)
    base["watchlist"] = watchlist

    for section, limit in (
        ("saved_plans", MAX_SAVED_PLANS),
        ("positions", MAX_POSITIONS),
        ("last_analyses", MAX_LAST_ANALYSES),
        ("monitoring", MAX_WATCHLIST),
    ):
        if len(base[section]) > limit:
            raise StateSizeError(f"{section} exceeds its item limit.")
    for item in base["recent_searches"]:
        if not isinstance(item, Mapping):
            raise StateError("recent_searches entries must be objects.")
    for section in ("saved_plans", "positions", "last_analyses", "monitoring"):
        if not all(isinstance(value, Mapping) for value in base[section].values()):
            raise StateError(f"{section} entries must be objects.")
    for section in ("positions", "last_analyses", "monitoring"):
        normalised: dict[str, Any] = {}
        for ticker, value in base[section].items():
            normalised[normalize_ticker(ticker)] = value
        base[section] = normalised

    alerts = _require_mapping(base.get("alerts"), "alerts")
    alerts["enabled"] = _require_mapping(alerts.get("enabled", {}), "alerts.enabled")
    alerts["events"] = _require_list(alerts.get("events", []), "alerts.events", MAX_ALERT_EVENTS)
    base["alerts"] = alerts

    calibration = _require_mapping(base.get("calibration"), "calibration")
    calibration["results"] = _require_list(
        calibration.get("results", []), "calibration.results", MAX_CALIBRATION_RESULTS
    )
    base["calibration"] = calibration

    _secret_check(base)
    try:
        encoded = json.dumps(base, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StateError("State is not valid JSON data.") from exc
    if len(encoded) > MAX_STATE_BYTES:
        raise StateSizeError("State exceeds the maximum file size.")
    return base


def _atomic_write(path: Path, text: str, *, max_bytes: int = MAX_STATE_BYTES) -> None:
    payload = text.encode("utf-8")
    if len(payload) > max_bytes:
        raise StateSizeError("Output exceeds the maximum file size.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise StatePathError("Symbolic-link state files are not allowed.")
    if path.exists() and not path.is_file():
        raise StatePathError("State path must refer to a regular file.")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


class AutopilotStateStore:
    """Atomic JSON state store with an in-process, per-path reentrant lock."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        allowed_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.allowed_root = (
            Path(allowed_root).expanduser().resolve(strict=False) if allowed_root is not None else None
        )
        self.path = _safe_path(path, self.allowed_root)
        self._lock = _path_lock(self.path)
        self.last_recovery_reason: str | None = None

    def _write_unlocked(self, state: Mapping[str, Any]) -> dict[str, Any]:
        normalised = _normalise_document(state)
        text = json.dumps(normalised, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.path, text)
        return normalised

    def _recover_unlocked(self, reason: str) -> dict[str, Any]:
        self.last_recovery_reason = reason
        recovered = default_state()
        recovered["recovery"] = {"occurred": True, "reason": reason, "recovered_at": _utc_now()}
        return self._write_unlocked(recovered)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._write_unlocked(default_state())
        if self.path.is_symlink() or not self.path.is_file():
            raise StatePathError("State path is not a safe regular file.")
        try:
            size = self.path.stat().st_size
        except OSError:
            raise
        if size > MAX_STATE_BYTES:
            return self._recover_unlocked("oversized_state")
        try:
            raw = self.path.read_text(encoding="utf-8")
            loaded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._recover_unlocked("invalid_json")
        if not isinstance(loaded, Mapping):
            return self._recover_unlocked("invalid_state")
        try:
            normalised = _normalise_document(loaded)
        except UnsupportedSchemaError:
            raise
        except (StateError, TypeError, ValueError):
            return self._recover_unlocked("invalid_state")
        if normalised != loaded:
            normalised = self._write_unlocked(normalised)
        self.last_recovery_reason = None
        return normalised

    def load(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_unlocked())

    snapshot = load

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Replace state intentionally, incrementing the on-disk revision."""

        if not isinstance(state, Mapping):
            raise StateError("State must be an object.")
        _secret_check(state)
        with self._lock:
            current = self._load_unlocked()
            proposed = copy.deepcopy(dict(state))
            proposed["revision"] = max(int(proposed.get("revision", 0)), int(current["revision"])) + 1
            proposed["created_at"] = current.get("created_at") or proposed.get("created_at") or _utc_now()
            proposed["updated_at"] = _utc_now()
            return copy.deepcopy(self._write_unlocked(proposed))

    def update(self, updater: Callable[[dict[str, Any]], Mapping[str, Any] | None]) -> dict[str, Any]:
        """Run one locked read-modify-write transaction.

        The callback may mutate its argument or return a replacement mapping.
        Locks are shared by all store instances for the same path, preventing
        lost updates inside one Streamlit process.
        """

        if not callable(updater):
            raise TypeError("updater must be callable")
        with self._lock:
            current = self._load_unlocked()
            working = copy.deepcopy(current)
            replacement = updater(working)
            if replacement is not None:
                if not isinstance(replacement, Mapping):
                    raise StateError("State updater must return an object or None.")
                working = copy.deepcopy(dict(replacement))
            _secret_check(working)
            working["revision"] = int(current["revision"]) + 1
            working["created_at"] = current["created_at"]
            working["updated_at"] = _utc_now()
            return copy.deepcopy(self._write_unlocked(working))

    transaction = update

    def add_watchlist(self, ticker: str) -> list[str]:
        symbol = normalize_ticker(ticker)

        def mutate(state: dict[str, Any]) -> None:
            items = state["watchlist"]
            if symbol not in items:
                if len(items) >= MAX_WATCHLIST:
                    raise StateSizeError("Watchlist is full.")
                items.append(symbol)

        return self.update(mutate)["watchlist"]

    add_to_watchlist = add_watchlist

    def remove_watchlist(self, ticker: str) -> list[str]:
        symbol = normalize_ticker(ticker)

        def mutate(state: dict[str, Any]) -> None:
            state["watchlist"] = [item for item in state["watchlist"] if item != symbol]

        return self.update(mutate)["watchlist"]

    remove_from_watchlist = remove_watchlist

    def remember_search(self, query: str, *, ticker: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str):
            raise StateError("Search query must be text.")
        cleaned = " ".join(query.strip().split())
        if not cleaned or len(cleaned) > 128 or not _QUERY_RE.fullmatch(cleaned):
            raise StateError("Search query contains unsupported characters or has an invalid length.")
        symbol: str | None = normalize_ticker(ticker) if ticker is not None else None
        if symbol is None and " " not in cleaned and is_valid_ticker(cleaned):
            symbol = normalize_ticker(cleaned)
            cleaned = symbol
        record = {"query": cleaned, "ticker": symbol, "searched_at": _utc_now()}

        def mutate(state: dict[str, Any]) -> None:
            key = symbol or cleaned.casefold()
            state["recent_searches"] = [
                item
                for item in state["recent_searches"]
                if (item.get("ticker") or str(item.get("query", "")).casefold()) != key
            ]
            state["recent_searches"].insert(0, copy.deepcopy(record))
            del state["recent_searches"][MAX_RECENT_SEARCHES:]

        self.update(mutate)
        return copy.deepcopy(record)

    def save_analysis(self, ticker: str, analysis: Mapping[str, Any]) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        payload = _require_mapping(analysis, "analysis")
        _secret_check(payload)
        record = payload
        record["ticker"] = symbol
        record["saved_at"] = _utc_now()

        def mutate(state: dict[str, Any]) -> None:
            state["last_analyses"].pop(symbol, None)
            state["last_analyses"][symbol] = copy.deepcopy(record)
            _trim_mapping(state["last_analyses"], MAX_LAST_ANALYSES)

        self.update(mutate)
        return copy.deepcopy(record)

    def save_plan(
        self,
        ticker: str | Mapping[str, Any],
        plan: Mapping[str, Any] | None = None,
        *,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(ticker, Mapping):
            if plan is not None:
                raise StateError("Pass either a plan object or ticker plus plan, not both forms.")
            payload = _require_mapping(ticker, "plan")
            symbol = normalize_ticker(payload.get("ticker"))
        else:
            symbol = normalize_ticker(ticker)
            payload = _require_mapping(plan or {}, "plan")
        _secret_check(payload)
        identifier = plan_id or payload.get("plan_id") or payload.get("id") or uuid.uuid4().hex
        if not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(identifier):
            raise StateError("plan_id contains unsupported characters or has an invalid length.")
        record = payload
        record.pop("id", None)
        record["plan_id"] = identifier
        record["ticker"] = symbol
        record["saved_at"] = _utc_now()

        def mutate(state: dict[str, Any]) -> None:
            state["saved_plans"].pop(identifier, None)
            state["saved_plans"][identifier] = copy.deepcopy(record)
            _trim_mapping(state["saved_plans"], MAX_SAVED_PLANS)

        self.update(mutate)
        return copy.deepcopy(record)

    @staticmethod
    def _tracking_plan(state: dict[str, Any], symbol: str) -> tuple[str, dict[str, Any]]:
        for identifier, plan in reversed(list(state["saved_plans"].items())):
            if isinstance(plan, Mapping) and plan.get("ticker") == symbol:
                return identifier, dict(plan)
        identifier = f"{symbol}:tracking"
        return identifier, {"plan_id": identifier, "ticker": symbol, "saved_at": _utc_now()}

    def _mark(self, ticker: str, action: str, details: Mapping[str, Any] | None) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        payload = _require_mapping(details or {}, "details")
        _secret_check(payload)
        changed_at = _utc_now()
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            identifier, plan = self._tracking_plan(state, symbol)
            plan.update(copy.deepcopy(payload))
            plan.update({"plan_id": identifier, "ticker": symbol, "tracking_status": action, "status_changed_at": changed_at})
            state["saved_plans"][identifier] = plan
            if action == "WATCHING" and symbol not in state["watchlist"]:
                if len(state["watchlist"]) >= MAX_WATCHLIST:
                    raise StateSizeError("Watchlist is full.")
                state["watchlist"].append(symbol)
            entry = {"action": action, "ticker": symbol, "recorded_at": changed_at}
            if payload:
                entry["details"] = copy.deepcopy(payload)
            state["activity_log"].append(entry)
            del state["activity_log"][:-MAX_STATE_CHANGES]
            if action == "PASSED":
                state["journal"].append(copy.deepcopy(entry))
                del state["journal"][:-MAX_JOURNAL_ENTRIES]
            result.update(plan)

        self.update(mutate)
        return copy.deepcopy(result)

    def mark_watching(self, ticker: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._mark(ticker, "WATCHING", details)

    def mark_passed(self, ticker: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._mark(ticker, "PASSED", details)

    def mark_entered(
        self,
        ticker: str,
        entry_price: float | int | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        payload = _require_mapping(details or {}, "details")
        _secret_check(payload)
        price = self._optional_price(entry_price, "entry_price")
        entered_at = _utc_now()
        position = payload
        position.update({"ticker": symbol, "status": "OPEN", "entered_at": entered_at})
        if price is not None:
            position["entry_price"] = price

        def mutate(state: dict[str, Any]) -> None:
            if symbol not in state["positions"] and len(state["positions"]) >= MAX_POSITIONS:
                raise StateSizeError("Positions list is full.")
            state["positions"][symbol] = copy.deepcopy(position)
            identifier, plan = self._tracking_plan(state, symbol)
            plan.update({"tracking_status": "ENTERED", "status_changed_at": entered_at})
            state["saved_plans"][identifier] = plan
            state["activity_log"].append({"action": "ENTERED", "ticker": symbol, "recorded_at": entered_at})
            del state["activity_log"][:-MAX_STATE_CHANGES]
            state["journal"].append({"action": "ENTERED", **copy.deepcopy(position)})
            del state["journal"][:-MAX_JOURNAL_ENTRIES]

        self.update(mutate)
        return copy.deepcopy(position)

    @staticmethod
    def _optional_price(value: float | int | None, name: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise StateError(f"{name} must be a non-negative finite number.")
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise StateError(f"{name} must be a non-negative finite number.") from exc
        if not math.isfinite(price) or price < 0:
            raise StateError(f"{name} must be a non-negative finite number.")
        return price

    def close_trade(
        self,
        ticker: str,
        exit_price: float | int | None = None,
        *,
        outcome: str | Mapping[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        price = self._optional_price(exit_price, "exit_price")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 4_000):
            raise StateError("notes must be short text.")
        if outcome is not None and not isinstance(outcome, (str, Mapping)):
            raise StateError("outcome must be text or an object.")
        _secret_check({"outcome": outcome, "notes": notes})
        closed: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            if symbol not in state["positions"]:
                raise KeyError(f"No open position is tracked for {symbol}.")
            position = dict(state["positions"].pop(symbol))
            entry = copy.deepcopy(position)
            entry.update({"action": "CLOSED", "status": "CLOSED", "ticker": symbol, "closed_at": _utc_now()})
            if price is not None:
                entry["exit_price"] = price
            if outcome is not None:
                entry["outcome"] = copy.deepcopy(outcome)
            if notes:
                entry["notes"] = notes.strip()
            state["journal"].append(entry)
            del state["journal"][:-MAX_JOURNAL_ENTRIES]
            identifier, plan = self._tracking_plan(state, symbol)
            plan.update({"tracking_status": "CLOSED", "status_changed_at": entry["closed_at"]})
            state["saved_plans"][identifier] = plan
            state["activity_log"].append({"action": "CLOSED", "ticker": symbol, "recorded_at": entry["closed_at"]})
            del state["activity_log"][:-MAX_STATE_CHANGES]
            closed.update(entry)

        self.update(mutate)
        return copy.deepcopy(closed)

    def set_alert_enabled(
        self,
        ticker: str,
        alert_type: str,
        enabled: bool = True,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        if not isinstance(alert_type, str) or not _IDENTIFIER_RE.fullmatch(alert_type):
            raise StateError("alert_type contains unsupported characters or has an invalid length.")
        payload = _require_mapping(details or {}, "details")
        _secret_check(payload)
        key = f"{symbol}:{alert_type}"
        record = {
            **payload,
            "ticker": symbol,
            "alert_type": alert_type,
            "enabled": bool(enabled),
            "updated_at": _utc_now(),
        }

        def mutate(state: dict[str, Any]) -> None:
            state["alerts"]["enabled"][key] = copy.deepcopy(record)

        self.update(mutate)
        return copy.deepcopy(record)

    def record_high_value_state_changes(
        self,
        ticker: str,
        previous: str | Mapping[str, Any] | None,
        current: str | Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        symbol = normalize_ticker(ticker)
        events = detect_state_changes(previous, current, ticker=symbol)
        if not events:
            return []
        recorded_at = _utc_now()
        recorded = [{**event, "recorded_at": recorded_at} for event in events]

        def mutate(state: dict[str, Any]) -> None:
            state["state_changes"].extend(copy.deepcopy(recorded))
            del state["state_changes"][:-MAX_STATE_CHANGES]
            state["alerts"]["events"].extend(copy.deepcopy(recorded))
            del state["alerts"]["events"][:-MAX_ALERT_EVENTS]

        self.update(mutate)
        return copy.deepcopy(recorded)

    record_state_changes = record_high_value_state_changes
    record_state_change = record_high_value_state_changes
    record_high_value_state_change = record_high_value_state_changes

    def record_monitoring_update(
        self,
        ticker: str,
        current: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Compare with the stored snapshot, record events, and save the new baseline."""

        symbol = normalize_ticker(ticker)
        payload = _require_mapping(current, "current monitoring snapshot")
        payload["ticker"] = symbol
        payload["observed_at"] = payload.get("observed_at") or _utc_now()
        _secret_check(payload)
        recorded: list[dict[str, Any]] = []

        def mutate(state: dict[str, Any]) -> None:
            previous = state["monitoring"].get(symbol)
            events = detect_state_changes(previous, payload, ticker=symbol) if previous else []
            timestamp = _utc_now()
            recorded.extend({**event, "recorded_at": timestamp} for event in events)
            if recorded:
                state["state_changes"].extend(copy.deepcopy(recorded))
                del state["state_changes"][:-MAX_STATE_CHANGES]
                state["alerts"]["events"].extend(copy.deepcopy(recorded))
                del state["alerts"]["events"][:-MAX_ALERT_EVENTS]
            state["monitoring"].pop(symbol, None)
            state["monitoring"][symbol] = copy.deepcopy(payload)
            _trim_mapping(state["monitoring"], MAX_WATCHLIST)

        self.update(mutate)
        return copy.deepcopy(recorded)

    process_monitoring_update = record_monitoring_update

    def export_state(self, destination: str | os.PathLike[str] | None = None) -> str:
        """Return a JSON export and optionally atomically write the same export."""

        state = self.load()
        _secret_check(state)
        text = json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
        if destination is not None:
            export_path = _safe_path(destination, self.allowed_root)
            if export_path == self.path:
                raise StatePathError("Export destination must differ from the live state file.")
            with _path_lock(export_path):
                _atomic_write(export_path, text)
        return text

    export = export_state


StateStore = AutopilotStateStore
PersonalStateStore = AutopilotStateStore


__all__ = [
    "AutopilotStateStore",
    "PersonalStateStore",
    "StateStore",
    "InvalidTickerError",
    "SecretDataError",
    "StateError",
    "StatePathError",
    "StateSizeError",
    "UnsupportedSchemaError",
    "SCHEMA_VERSION",
    "SETUP_STATES",
    "MONITORED_SETUP_STATES",
    "create_default_state",
    "default_state",
    "detect_monitoring_events",
    "detect_state_changes",
    "is_valid_ticker",
    "normalize_ticker",
    "validate_ticker",
]
