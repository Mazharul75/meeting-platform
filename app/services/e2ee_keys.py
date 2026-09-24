"""End-to-end encryption key for one meeting's online room (plan Section 6.6).

key = HMAC-SHA256(MASTER_KEY, meeting_id). Nothing is stored, so nothing can leak from the
database; the key is only ever handed to a browser that already passed a permission check.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid


def meeting_e2ee_key(master_key: str, meeting_id: uuid.UUID) -> str:
    digest = hmac.new(master_key.encode(), str(meeting_id).encode(), hashlib.sha256).digest()
    return digest.hex()
