"""Opt-in, metadata-only hook timing. Never perform I/O on the dispatch thread."""
from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
from typing import Any
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

APPROVED_GUILD = "1517646536505557132"
_FIELDS = frozenset({"schema", "event", "process_id", "dispatch_id", "callback_index",
                     "callback_id", "hook", "mode", "session_id", "turn_id",
                     "parent_session_id", "parent_turn_id", "start_ns", "end_ns",
                     "duration_ms", "gap_ms", "admission_ms", "outcome", "observed_at"})
_current: contextvars.ContextVar[Any] = contextvars.ContextVar("plugin_hook_timing", default=None)
_writers = {}
_writer_lock = threading.Lock()


def identifier(value):
    # Hash even malformed identifiers: no arbitrary payload text can enter the queue.
    if type(value) is str and 0 < len(value) <= 256:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return None


class Writer:
    """One daemon and a fixed queue per manager; all routing and disk work is here."""
    def __init__(self, home, capacity=1024):
        self.home = Path(home)
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = self.failed = self.excluded = self.written = 0
        self.thread = threading.Thread(target=self._run, name="hermes-hook-timing", daemon=True)
        self.thread.start()

    def offer(self, record):
        try:
            clean = {k: v for k, v in record.items()
                     if k in _FIELDS and type(v) in (str, int, float, bool)}
            self.queue.put_nowait(clean)
        except Exception:
            self.dropped += 1

    def _scoped(self, record):
        sid = record.get("parent_session_id") if record.get("hook") == "subagent_stop" else None
        sid = sid or record.get("session_id")
        if not sid:
            return False
        # Read-only URI: do not create a DB, migrate it, import gateway, or take its write lock.
        uri = (self.home / "state.db").resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.05) as db:
            matches = []
            for (raw,) in db.execute("SELECT entry_json FROM gateway_routing"):
                entry = json.loads(raw)
                if isinstance(entry, dict) and identifier(entry.get("session_id")) == sid:
                    source = entry.get("origin") or {}
                    matches.append(source.get("platform") == "discord" and
                                   source.get("scope_id", source.get("guild_id")) == APPROVED_GUILD)
            # Conflicting routes fail closed; no trust in kwargs platform/guild claims.
            return bool(matches) and all(matches)

    def _status(self):
        directory = self.home / "runtime" / "hermes-timing"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"plugin-hook-status-{os.getpid()}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema": 1, "process_id": os.getpid(),
                                   "written": self.written, "dropped": self.dropped,
                                   "failed": self.failed, "excluded": self.excluded,
                                   "queued": self.queue.qsize(), "updated_at": time.time()}), encoding="utf-8")
        temp.replace(target)

    def _write(self, record):
        if not self._scoped(record):
            self.excluded += 1
            return
        directory = self.home / "runtime" / "hermes-timing"
        directory.mkdir(parents=True, exist_ok=True)
        record = dict(record, scope="verified_discord_guild", guild_id=APPROVED_GUILD)
        with (directory / "plugin-hook-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.written += 1

    def _run(self):
        while True:
            try:
                record = self.queue.get(timeout=1)
            except queue.Empty:
                record = None
            if record is not None:
                try:
                    self._write(record)
                except Exception:
                    self.failed += 1  # No exception text, logging, or recursive hook dispatch.
                finally:
                    self.queue.task_done()
            try:
                self._status()
            except Exception:
                self.failed += 1


class Dispatch:
    def __init__(self, writer, hook, mode, payload):
        from hermes_cli.plugins import VALID_HOOKS
        self.writer = writer
        self.base = {"schema": 1, "process_id": os.getpid(), "dispatch_id": uuid.uuid4().hex,
                     "hook": hook if hook in VALID_HOOKS else "other", "mode": mode}
        for field in ("session_id", "turn_id", "parent_session_id", "parent_turn_id"):
            value = identifier(payload.get(field))
            if value:
                self.base[field] = value
        self.start = self.previous = time.monotonic_ns()
        self.outcome = "ok"

    def emit(self, event, start, end, outcome, **extra):
        try:
            self.writer.offer(dict(self.base, event=event, start_ns=start, end_ns=end,
                                   duration_ms=(end - start) / 1e6, outcome=outcome,
                                   observed_at=time.time(), **extra))
        except Exception:
            pass

    def callback(self, index, cb):
        return Callback(self, index, cb)

    def finish(self, outcome=None):
        self.emit("dispatch", self.start, time.monotonic_ns(), outcome or self.outcome)


class Callback:
    def __init__(self, dispatch, index, cb):
        self.dispatch = dispatch
        self.start = time.monotonic_ns()
        self.gap = (self.start - dispatch.previous) / 1e6
        self.meta = {"callback_index": index, "callback_id": hex(id(cb))}
        self.outcome = "ok"

    def mark(self, outcome):
        self.outcome = outcome
        if outcome != "ok":
            self.dispatch.outcome = "partial"

    def execute(self, fn, *args):
        start = time.monotonic_ns()
        outcome = "ok"
        try:
            return fn(*args)
        except BaseException:
            outcome = "error"
            raise
        finally:
            self.dispatch.emit("callback_execution", start, time.monotonic_ns(), outcome,
                               admission_ms=(start - self.start) / 1e6, **self.meta)

    async def await_execution(self, awaitable):
        start = time.monotonic_ns()
        outcome = "ok"
        try:
            return await awaitable
        except BaseException as exc:
            outcome = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
            raise
        finally:
            self.dispatch.emit("callback_execution", start, time.monotonic_ns(), outcome,
                               admission_ms=(start - self.start) / 1e6, **self.meta)

    def finish(self):
        end = time.monotonic_ns()
        self.dispatch.previous = end
        self.dispatch.emit("callback_wait", self.start, end, self.outcome, gap_ms=self.gap, **self.meta)


def begin(manager, hook, mode, payload):
    try:
        from hermes_cli.plugins import load_config_readonly
        config = (load_config_readonly() or {}).get("plugins") or {}
        if config.get("hook_timing", {}).get("enabled") is not True:
            return None
        # Never retain unidentifiable dispatches, even in the metadata queue.
        if not any(identifier(payload.get(field)) for field in ("session_id", "parent_session_id")):
            return None
        writer = getattr(manager, "_hook_timing_writer", None)
        if writer is None:
            # Never wait behind another dispatch; bound daemon count across manager reloads.
            if not _writer_lock.acquire(blocking=False):
                return None
            try:
                key = str(manager.home_path)
                writer = _writers.get(key)
                if writer is None:
                    if len(_writers) >= 8:
                        return None
                    writer = _writers[key] = Writer(manager.home_path)
                manager._hook_timing_writer = writer
            finally:
                _writer_lock.release()
        return Dispatch(writer, hook, mode, payload)
    except Exception:
        return None


def timed_dispatch(fn):
    """Wrap dispatch, not payloads/callbacks; context is copied by the native worker."""
    def start(manager, hook, payload):
        return begin(manager, hook, "async" if inspect.iscoroutinefunction(fn) else "sync", payload)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(manager, hook_name, **kwargs):
            dispatch = start(manager, hook_name, kwargs)
            token = _current.set(dispatch)
            try:
                result = await fn(manager, hook_name, **kwargs)
            except BaseException:
                if dispatch:
                    dispatch.finish("aborted")
                raise
            else:
                if dispatch:
                    dispatch.finish()
                return result
            finally:
                _current.reset(token)
        return async_wrapper
    else:
        @functools.wraps(fn)
        def wrapper(manager, hook_name, **kwargs):
            dispatch = start(manager, hook_name, kwargs)
            token = _current.set(dispatch)
            try:
                result = fn(manager, hook_name, **kwargs)
            except BaseException:
                if dispatch:
                    dispatch.finish("aborted")
                raise
            else:
                if dispatch:
                    dispatch.finish()
                return result
            finally:
                _current.reset(token)
        return wrapper


def callback_span(index, cb):
    dispatch = _current.get()
    return dispatch.callback(index, cb) if dispatch else None
