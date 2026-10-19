"""Per-invocation IPC between gateway ``/update`` and its detached ``hermes update --gateway``.

Each invocation owns ``logs/update_receipts/gateway/<correlation>/`` (beside the native update
receipts) for its pending marker, output, exit code, prompt/response and delivery receipt. The
child learns its directory from the launcher correlation id the native receipt already records
(``HERMES_UPDATE_CORRELATION_ID``); without one, every path stays in the home root as before.
Stdlib-only at import: the completion and takeover processes call it before app imports.
"""

import os
import re
from pathlib import Path

_RUN_ID = re.compile(r"[0-9a-f]{32}")
_KEEP_DELIVERED = 20  # same bound as the native receipt store


def gateway_runs_dir(home: Path) -> Path:
    return Path(home) / "logs" / "update_receipts" / "gateway"


def is_run_id(value: object) -> bool:
    return isinstance(value, str) and bool(_RUN_ID.fullmatch(value))


def gateway_update_home(home: Path, run_id: str | None = None) -> Path:
    """IPC directory for one invocation; the home root for legacy and non-gateway runs.

    With no explicit id, the launcher correlation id selects a run directory only when the
    gateway created it: a Desktop-launched update carries a correlation id too, and must keep
    writing where its own launcher reads.
    """
    if run_id is not None:
        if not is_run_id(run_id):
            raise ValueError("invalid gateway update run id")
        return gateway_runs_dir(home) / run_id
    value = os.environ.get("HERMES_UPDATE_CORRELATION_ID", "").strip()
    run_home = gateway_runs_dir(home) / value
    return run_home if is_run_id(value) and run_home.is_dir() else Path(home)


def pending_run_homes(home: Path) -> list[Path]:
    directory = gateway_runs_dir(home)
    if not directory.is_dir():
        return []
    return [path for path in sorted(directory.iterdir()) if is_run_id(path.name) and path.is_dir()]


def write_update_response(answer: str, run_id: str = "") -> None:
    """Hand a prompt answer to the update process that asked; raises OSError.

    A correlated answer lands in the gateway home that owns that invocation (where the watcher
    and child exchange files); an uncorrelated one keeps the legacy profile-home path.
    """
    if run_id:
        from gateway.run import _hermes_home
        home = gateway_update_home(_hermes_home, run_id)
    else:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    response_path = home / ".update_response"
    tmp = response_path.with_suffix(".tmp")
    tmp.write_text(answer, encoding="utf-8")
    tmp.replace(response_path)


def native_receipt_for(correlation_id: str):
    """``(path, record)`` of the native receipt this launcher id names; never the latest pointer."""
    import json
    from hermes_cli.update_receipt import _receipt_dirs
    if not is_run_id(correlation_id):
        return None
    for directory in _receipt_dirs():
        for path in sorted(directory.glob("update_*.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("correlation_id") == correlation_id:
                return path, record
    return None


def record_gateway_delivery(run_home: Path, pending: dict, target: dict | None) -> None:
    """Persist one delivery outcome for a correlated invocation; raises OSError.

    The run directory keeps every attempt's latest outcome. A confirmed delivery is also stamped
    on the native receipt once it is terminal (its writer is done; a running record would be
    overwritten by the updater's next persist).
    """
    import json
    from datetime import UTC, datetime
    from pm.filesystem import durable_write_bytes
    run_id = pending.get("run_id", "")
    if not is_run_id(run_id):
        return
    native = native_receipt_for(run_id)
    report = {"correlation_id": run_id, "delivered": target is not None, "target": target,
              "update_id": native[1].get("update_id") if native else None,
              "recorded_at": datetime.now(UTC).isoformat()}
    durable_write_bytes(run_home / "delivery.json", (json.dumps(report, indent=2) + "\n").encode("utf-8"))
    if target is not None and native and native[1].get("finished_at"):
        record = {**native[1], "gateway_report": report}
        durable_write_bytes(native[0], (json.dumps(record, indent=2, default=str) + "\n").encode("utf-8"))


def prune_delivered_runs(home: Path) -> None:
    """Keep the newest delivered invocation directories; undelivered ones are never pruned."""
    import shutil
    delivered = [path for path in pending_run_homes(home)
                 if not (path / ".update_pending.json").exists()
                 and not (path / ".update_pending.claimed.json").exists()]
    delivered.sort(key=lambda path: path.stat().st_mtime)
    for path in delivered[:-_KEEP_DELIVERED]:
        shutil.rmtree(path, ignore_errors=True)
