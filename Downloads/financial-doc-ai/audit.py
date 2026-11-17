"""Tamper-evident audit ledger.

Each entry stores the SHA-256 of the canonical JSON of the validated payload and is chained to the
previous entry (entry_hash = SHA-256 over the previous entry's hash + this entry's fields). Editing,
deleting or reordering any past entry breaks the chain, which `verify_ledger` detects.

This is tamper-*evident*, not tamper-*proof*: someone with write access to the file can still rewrite
the whole chain. For stronger guarantees, anchor the latest entry_hash somewhere you don't control
(a separate system, a signed email, a public timestamping service).
"""
import hashlib
import json
from datetime import datetime, timezone

LEDGER_PATH = "audit_ledger.jsonl"
GENESIS = "0" * 64


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def payload_hash(data: dict) -> str:
    """Deterministic SHA-256 of the payload: the same data always gives the same hash."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _entry_hash(prev_hash: str, timestamp: str, p_hash: str, record_count: int) -> str:
    return hashlib.sha256(f"{prev_hash}|{timestamp}|{p_hash}|{record_count}".encode("utf-8")).hexdigest()


def _read_entries(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def generate_audit_hash(data: dict, ledger_path: str = LEDGER_PATH) -> str:
    """Append the payload to the hash-chained ledger and return the payload hash.

    Re-submitting the identical payload as the most recent entry does not add a duplicate line.
    """
    p_hash = payload_hash(data)
    entries = _read_entries(ledger_path)
    if entries and entries[-1].get("payload_hash") == p_hash:
        return p_hash

    prev = entries[-1]["entry_hash"] if entries else GENESIS
    timestamp = datetime.now(timezone.utc).isoformat()
    record_count = len(data.get("valid_records", [])) + len(data.get("anomalies", []))
    entry = {
        "timestamp": timestamp,
        "payload_hash": p_hash,
        "record_count": record_count,
        "prev_hash": prev,
        "entry_hash": _entry_hash(prev, timestamp, p_hash, record_count),
    }
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return p_hash


def verify_ledger(ledger_path: str = LEDGER_PATH) -> tuple[bool, str]:
    """Check the whole chain. Returns (ok, message)."""
    prev = GENESIS
    for n, e in enumerate(_read_entries(ledger_path), start=1):
        if e.get("prev_hash") != prev:
            return False, f"Entry {n}: chain broken (previous entry was altered, removed or reordered)."
        if e.get("entry_hash") != _entry_hash(prev, e.get("timestamp", ""), e.get("payload_hash", ""), e.get("record_count", 0)):
            return False, f"Entry {n}: contents were modified."
        prev = e["entry_hash"]
    return True, "Ledger chain is intact."


def verify_payload(data: dict, ledger_path: str = LEDGER_PATH) -> bool:
    """True if this exact payload was recorded in the ledger (and the chain is intact)."""
    ok, _ = verify_ledger(ledger_path)
    return ok and any(e.get("payload_hash") == payload_hash(data) for e in _read_entries(ledger_path))
