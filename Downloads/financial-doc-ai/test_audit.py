import json

from audit import generate_audit_hash, payload_hash, verify_ledger, verify_payload

PAYLOAD = {"valid_records": [{"description": "A", "total": 1.0}], "anomalies": []}


def test_hash_is_deterministic():
    assert payload_hash(PAYLOAD) == payload_hash(json.loads(json.dumps(PAYLOAD)))
    assert payload_hash(PAYLOAD) != payload_hash({**PAYLOAD, "anomalies": [{"x": 1}]})


def test_ledger_chain_verifies_and_dedupes(tmp_path):
    ledger = str(tmp_path / "ledger.jsonl")
    h1 = generate_audit_hash(PAYLOAD, ledger)
    assert generate_audit_hash(PAYLOAD, ledger) == h1                    # same payload: no duplicate line
    generate_audit_hash({**PAYLOAD, "anomalies": [{"x": 1}]}, ledger)
    assert len(open(ledger).read().splitlines()) == 2
    assert verify_ledger(ledger)[0] is True
    assert verify_payload(PAYLOAD, ledger) is True
    assert verify_payload({"valid_records": [], "anomalies": []}, ledger) is False


def test_tampering_is_detected(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    for i in range(3):
        generate_audit_hash({"valid_records": [{"n": i}], "anomalies": []}, str(ledger))

    lines = ledger.read_text().splitlines()
    edited = json.loads(lines[1]); edited["record_count"] = 99
    tampered = [lines[0], json.dumps(edited), lines[2]]
    ledger.write_text("\n".join(tampered) + "\n")
    assert verify_ledger(str(ledger))[0] is False

    ledger.write_text("\n".join([lines[0], lines[2]]) + "\n")           # delete the middle entry
    assert verify_ledger(str(ledger))[0] is False
