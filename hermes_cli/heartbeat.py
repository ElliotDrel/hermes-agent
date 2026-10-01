"""Session heartbeats — recurring re-entry prompts for the current session.

Session-scoped and in-process (CLI or gateway must be running); durable cross-process scheduling stays
``hermes cron``. Invariants (mirrors goals.py): injection is a plain user message — no system-prompt
mutation or toolset swap, so prompt caching stays intact — and a real user message always wins:
heartbeats only fire into an idle session with an empty input queue."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60  # floor: re-entering more often than once a minute is a busy-loop, not a heartbeat
POLL_SECONDS = 5.0  # how often drivers poll for due heartbeats; not user-facing

HEARTBEAT_PROMPT_TEMPLATE = (
    "[Heartbeat — recurring instruction, fires every {interval}]\n{prompt}\n\n"
    "If there is nothing meaningful to do or report for this instruction "
    "right now, reply briefly that nothing has changed and stop — do not invent work."
)

_INTERVAL_RE = re.compile(
    r"^\s*(?:every\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600),
    **dict.fromkeys(("d", "day", "days"), 86400),
}

# field -> (coercer, default used when the stored value is missing/falsy)
_STATE_FIELDS = {
    "prompt": (str, ""), "interval_seconds": (int, 0), "status": (str, "active"),
    "created_at": (float, 0.0), "last_fired_at": (float, 0.0), "fire_count": (int, 0),
}


def parse_interval(text: str) -> Optional[int]:
    """Parse ``10m`` / ``every 2h`` / ``every 90 minutes`` into seconds.

    None when not an interval; below ``MIN_INTERVAL_SECONDS`` returns -1 so callers can tell "too small" apart.
    """
    m = _INTERVAL_RE.match(text) if text else None
    if not m:
        return None
    seconds = int(float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    return -1 if seconds < MIN_INTERVAL_SECONDS else seconds


def format_interval(seconds: int) -> str:
    """Human-readable interval (``600`` → ``10m``)."""
    seconds = int(seconds)
    units = ((86400, "d"), (3600, "h"), (60, "m"))
    return next((f"{seconds // unit}{suffix}" for unit, suffix in units if seconds % unit == 0), f"{seconds}s")


_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def _window_minutes(window: str) -> tuple[int, int]:
    match = _WINDOW_RE.fullmatch(window)
    if not match:
        raise ValueError("windows must use HH:MM-HH:MM (00:00 through 23:59)")
    sh, sm, eh, em = map(int, match.groups())
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        raise ValueError("equal window endpoints are ambiguous")
    return start, end


def validate_heartbeat_options(windows, timezone, include_time) -> None:
    """Validate persisted options too; corrupt opt-in state must not fire unrestricted."""
    if not isinstance(windows, list) or any(not isinstance(w, str) for w in windows):
        raise ValueError("windows must be a list of daily ranges")
    if not isinstance(include_time, bool):
        raise ValueError("include_time must be boolean")
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone must be an IANA timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {timezone}") from exc
    if windows and timezone is None:
        raise ValueError("--windows requires --timezone with an IANA timezone")
    occupied = set()
    for window in windows:
        start, end = _window_minutes(window)
        minutes = set(range(start, end + 1)) if start < end else set(range(start, 1440)) | set(range(end + 1))
        if occupied & minutes:
            raise ValueError("overlapping daily windows are ambiguous (ending minute is inclusive)")
        occupied.update(minutes)


def parse_heartbeat_spec(text: str) -> tuple[int, str, dict]:
    """Parse only the option prefix; the remainder is opaque prompt text, not shell syntax."""
    tokens = list(re.finditer(r"\S+", text))
    index = 1 if tokens and tokens[0].group().lower() == "every" else 0
    if index >= len(tokens):
        raise ValueError("Usage: /heartbeat every <interval> [options] <prompt>")
    interval = parse_interval(tokens[index].group())
    if interval is None:
        raise ValueError("Usage: /heartbeat every <interval> [options] <prompt>")
    if interval < 0:
        raise ValueError(f"Interval too small — minimum is {MIN_INTERVAL_SECONDS}s.")
    index += 1
    options = {}
    while index < len(tokens) and tokens[index].group().startswith("--"):
        option = tokens[index].group()
        index += 1
        if option == "--":
            break  # explicit boundary for prompts beginning with a literal option
        name = {"--windows": "windows", "--timezone": "timezone", "--include-time": "include_time"}.get(option)
        if name is None or name in options:
            raise ValueError(f"unknown or duplicate heartbeat option: {option}")
        if name == "include_time":
            options[name] = True
        else:
            if index >= len(tokens) or tokens[index].group().startswith("--"):
                raise ValueError(f"missing value for {option}")
            value = tokens[index].group()
            options[name] = value.split(",") if name == "windows" else value
            index += 1
    if index >= len(tokens):
        raise ValueError("Usage: /heartbeat every <interval> <prompt> — the prompt is required.")
    validate_heartbeat_options(options.get("windows", []), options.get("timezone"), options.get("include_time", False))
    prompt = text[tokens[index].start():]
    return interval, prompt, options


@dataclass
class HeartbeatState:
    """Serializable per-session heartbeat."""

    prompt: str
    interval_seconds: int
    status: str = "active"          # active | paused | cleared
    created_at: float = 0.0
    last_fired_at: float = 0.0
    fire_count: int = 0
    windows: list[str] = field(default_factory=list)
    timezone: Optional[str] = None
    include_time: bool = False

    def __post_init__(self):
        validate_heartbeat_options(self.windows, self.timezone, self.include_time)

    def to_json(self) -> str:
        data = asdict(self)
        # Default records stay byte-compatible; opt-in fields travel with compaction.
        for name, default in (("windows", []), ("timezone", None), ("include_time", False)):
            if data[name] == default:
                del data[name]
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "HeartbeatState":
        data = json.loads(raw)
        fields = {name: coerce(data.get(name) or default) for name, (coerce, default) in _STATE_FIELDS.items()}
        fields.update({name: data[name] for name in ("windows", "timezone", "include_time") if name in data})
        return cls(**fields)

    def window_is_open(self, now: Optional[float] = None) -> bool:
        if not self.windows:
            return True
        local = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(self.timezone))
        minute = local.hour * 60 + local.minute
        # Inclusive ending MINUTE retains endpoint reminders, including :59 seconds.
        return any(start <= minute <= end if start < end else minute >= start or minute <= end
                   for start, end in map(_window_minutes, self.windows))

    def is_due(self, now: Optional[float] = None) -> bool:
        if self.status != "active" or not self.prompt or self.interval_seconds <= 0:
            return False
        now = time.time() if now is None else now
        return self.window_is_open(now) and now - (self.last_fired_at or self.created_at) >= self.interval_seconds

    def render_prompt(self, now: Optional[float] = None) -> str:
        prompt = HEARTBEAT_PROMPT_TEMPLATE.format(interval=format_interval(self.interval_seconds), prompt=self.prompt)
        if self.include_time:
            zone = self.timezone or "UTC"
            local = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(zone))
            prompt = f"[Heartbeat current time: {local.isoformat(timespec='seconds')} ({zone})]\n{prompt}"
        return prompt


def _get_session_db() -> Optional[Any]:
    """Persistence goes through the goals module's per-HERMES_HOME cached SessionDB (one shared connection)."""
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
        return _goals_db()
    except Exception as exc:  # pragma: no cover
        logger.debug("HeartbeatManager: SessionDB bootstrap failed (%s)", exc)
        return None


_META_PREFIX = "heartbeat:"


def load_heartbeat(session_id: str) -> Optional[HeartbeatState]:
    db = _get_session_db() if session_id else None
    if db is None:
        return None
    try:
        raw = db.get_meta(_META_PREFIX + session_id)
    except Exception as exc:
        logger.debug("HeartbeatManager: get_meta failed: %s", exc)
        return None
    try:
        state = HeartbeatState.from_json(raw) if raw else None
    except Exception as exc:
        logger.warning("HeartbeatManager: could not parse stored heartbeat for %s: %s", session_id, exc)
        return None
    return None if state is None or state.status == "cleared" else state


def store_has_active_heartbeat(db: Any) -> bool:
    """True when *db* holds an ACTIVE ``heartbeat:*`` row — or one that cannot be parsed (unknown, so
    the caller keeps its full sweep). ``clear``/``pause`` keep their rows (status ``cleared``/``paused``),
    so key existence alone is not "active". Read errors propagate: "unavailable" is the caller's call."""
    for _key, raw in db.list_meta_prefix(_META_PREFIX):
        try:
            if HeartbeatState.from_json(raw).status == "active":
                return True
        except Exception:
            return True
    return False


def save_heartbeat(session_id: str, state: HeartbeatState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write
        _warn_dropped_write("HeartbeatManager", "heartbeat", session_id)
        return
    try:
        db.set_meta(_META_PREFIX + session_id, state.to_json())
    except Exception as exc:
        logger.debug("HeartbeatManager: set_meta failed: %s", exc)


class HeartbeatManager:
    """Per-session heartbeat state + due-tick decisions; the surface CLI + gateway talk to.

    Drivers (CLI thread / gateway task) call :meth:`due_prompt` on a poll cadence while the session is
    idle; a non-None return is the user-role message to inject.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state: Optional[HeartbeatState] = load_heartbeat(session_id)
        self._last_claim: Optional[tuple[float, int]] = None  # (last_fired_at, fire_count) before the last due_prompt

    @property
    def state(self) -> Optional[HeartbeatState]:
        return self._state

    def has_heartbeat(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def status_line(self) -> str:
        s = self._state
        if s is None:
            return "No heartbeat. Set one with /heartbeat every <interval> <prompt>."
        every = format_interval(s.interval_seconds)
        options = []
        if s.windows:
            options.append(f"windows {','.join(s.windows)} (inclusive ending minute)")
        if s.timezone:
            options.append(f"timezone {s.timezone}")
        if s.include_time:
            options.append(f"include-time ({s.timezone or 'UTC'})")
        if options:
            every += ", " + ", ".join(options)
        fired = f", fired {s.fire_count}×" if s.fire_count else ""
        if s.status == "active":
            next_in = max(0, int((s.last_fired_at or s.created_at) + s.interval_seconds - time.time()))
            timing = f"next in ~{next_in}s" if s.window_is_open() else "waiting for daily window"
            return f"♥ Heartbeat (every {every}, {timing}{fired}): {s.prompt}"
        icon = "⏸ " if s.status == "paused" else ""
        return f"{icon}Heartbeat ({s.status}, every {every}{fired}): {s.prompt}"

    def set(self, prompt: str, interval_seconds: int, *, windows=None, timezone=None, include_time=False) -> HeartbeatState:
        # Preserve opt-in instruction bytes; keep the old default whitespace behavior.
        prompt = (prompt or "") if windows or timezone or include_time else (prompt or "").strip()
        if not prompt.strip():
            raise ValueError("heartbeat prompt is empty")
        interval_seconds = int(interval_seconds)
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS}s")
        self._state = HeartbeatState(prompt=prompt, interval_seconds=interval_seconds, status="active",
                                     created_at=time.time(), windows=[] if windows is None else windows,
                                     timezone=timezone, include_time=include_time)
        save_heartbeat(self.session_id, self._state)
        return self._state

    def _set_status(self, status: str, *, reanchor: bool = False) -> Optional[HeartbeatState]:
        if not self._state:
            return None
        self._state.status = status
        if reanchor:
            self._state.last_fired_at = time.time()
        save_heartbeat(self.session_id, self._state)
        return self._state

    def pause(self) -> Optional[HeartbeatState]:
        return self._set_status("paused")

    def resume(self) -> Optional[HeartbeatState]:
        # Re-anchor so resuming doesn't instantly fire a stale tick.
        return self._set_status("active", reanchor=True)

    def clear(self) -> bool:
        cleared = self._set_status("cleared") is not None
        self._state = None
        return cleared

    def due_prompt(self, now: Optional[float] = None) -> Optional[str]:
        """Return the injection prompt if the heartbeat is due, else None.

        The fire is recorded immediately (before the turn runs) so overlapping polls or a long turn can never
        double-fire the same tick. Missed ticks coalesce: the anchor resets to NOW, not the theoretical
        schedule.
        """
        s = self._state
        if s is None or not s.is_due(now):
            return None
        self._last_claim = (s.last_fired_at, s.fire_count)
        s.last_fired_at = now if now is not None else time.time()
        s.fire_count += 1
        save_heartbeat(self.session_id, s)
        return s.render_prompt(now=s.last_fired_at)

    def abandon_fire(self) -> bool:
        """Rewind the fire recorded by the last :meth:`due_prompt` whose turn never started, so the tick stays
        due for the next poll instead of being silently consumed. Mirrors ``LoopManager.abandon_tick``. Skipped
        (False) when the persisted state moved on — a pause/resume/clear that landed in between wins."""
        claim, s = self._last_claim, self._state
        if claim is None or s is None:
            return False
        current = load_heartbeat(self.session_id)
        if current is None or current.status != "active" or (current.last_fired_at, current.fire_count) != (
                s.last_fired_at, s.fire_count):
            return False
        s.last_fired_at, s.fire_count = claim
        self._last_claim = None
        save_heartbeat(self.session_id, s)
        return True


@dataclass
class HeartbeatTick:
    """Opt-in CLI queue token: revalidate and stamp at dequeue, not watchdog admission."""

    manager: HeartbeatManager
    state: HeartbeatState = field(init=False)

    def __post_init__(self):
        # Freeze admission state: pause/resume and replacement must cancel an old queued tick.
        self.state = replace(self.manager.state, windows=list(self.manager.state.windows))

    def prepare(self, session_id: str, *, user_waiting: bool = False) -> Optional[str]:
        current = load_heartbeat(self.manager.session_id)
        now = time.time()
        if (session_id != self.manager.session_id or user_waiting or current != self.state
                or current is None or current.status != "active" or not current.window_is_open(now)):
            # Refund only the exact claim; never rewind a replacement or resumed instruction.
            if current == self.state and self.manager.state == self.state:
                self.manager.abandon_fire()
            return None
        return self.state.render_prompt(now)


def migrate_heartbeat_to_session(old_session_id: str, new_session_id: str) -> bool:
    """Carry a heartbeat across a compression session rotation (copy to child, archive parent, never raise).

    Same shape as ``goals.migrate_goal_to_session``.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_heartbeat(old_session_id)
        if state is None or load_heartbeat(new_session_id) is not None:
            return False
        save_heartbeat(new_session_id, state)
        state.status = "cleared"
        save_heartbeat(old_session_id, state)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("HeartbeatManager: migration failed: %s", exc)
        return False


__all__ = [
    "HeartbeatState", "HeartbeatManager", "HeartbeatTick", "parse_heartbeat_spec", "parse_interval", "format_interval", "load_heartbeat", "save_heartbeat",
    "migrate_heartbeat_to_session", "HEARTBEAT_PROMPT_TEMPLATE", "MIN_INTERVAL_SECONDS", "POLL_SECONDS",
]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Dict  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
