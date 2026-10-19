"""Completed compaction metadata, written on the native boundary transaction."""
import hashlib
from hermes_state_sessions import _parse_model_config

COUNT_KEY = "_completed_compactions"
RECORD_KEY = "_record_compaction"


def read_count(config):
    value = config.get(COUNT_KEY, 0)
    return value if type(value) is int and value >= 0 else None


def increment(db, conn, session_id, *, receipt=None):
    row = conn.execute("SELECT model_config FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    config = _parse_model_config(row[0])
    count = read_count(config)
    if count is None:
        return None
    if receipt:
        key = "compaction.receipt." + hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        inserted = conn.execute("INSERT OR IGNORE INTO state_meta(key,value) VALUES (?,?)", (key, session_id))
        if not inserted.rowcount:
            return count
    config[COUNT_KEY] = count + 1
    import json
    conn.execute("UPDATE sessions SET model_config=? WHERE id=?", (json.dumps(config),session_id))
    return count + 1


def provider_boundary(db, conn, session_id, boundary_id):
    if boundary_id is None:
        # Unknown is a JSON null (rather than deleting the key and claiming zero).
        from hermes_state_sessions import _parse_model_config
        import json
        row = conn.execute("SELECT model_config FROM sessions WHERE id=?",(session_id,)).fetchone()
        if row is not None:
            config = _parse_model_config(row[0]); config[COUNT_KEY] = None
            conn.execute("UPDATE sessions SET model_config=? WHERE id=?",(json.dumps(config),session_id))
        return None
    return increment(db, conn, session_id, receipt=boundary_id)
