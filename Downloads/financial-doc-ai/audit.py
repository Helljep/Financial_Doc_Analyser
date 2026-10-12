import os
import json
import hashlib
from datetime import datetime

def generate_audit_hash(data: dict) -> str:
    """
    Generates a secure, verbatim audit trail hash of the final JSON data.
    To simulate high-security enterprise environments, we seed the hash
    with physical randomness (os.urandom) rather than standard pseudorandom libs.
    """
    # 1. Serialize the final validated data
    serialized_data = json.dumps(data, sort_keys=True)
    
    # 2. Add physical randomness entropy (salt)
    salt = os.urandom(16)
    
    # 3. Create SHA-256 hash of the payload + salt
    hasher = hashlib.sha256()
    hasher.update(salt)
    hasher.update(serialized_data.encode('utf-8'))
    secure_hash = hasher.hexdigest()
    
    # 4. Save to immutable ledger
    ledger_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "hash": secure_hash,
        "salt_hex": salt.hex(),
        "record_count": len(data.get("valid_records", []))
    }
    
    with open("immutable_ledger.log", "a") as f:
        f.write(json.dumps(ledger_entry) + "\n")
        
    return secure_hash
