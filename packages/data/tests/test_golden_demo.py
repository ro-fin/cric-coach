"""Golden snapshot: the demo session is byte-stable across releases (REG).

If this digest changes, a code change altered synthetic generation. That must be
intentional: review the diff, update the digest in the same commit, and note it
in the commit message (Definition of Done: golden fixtures updated intentionally).
"""

import hashlib
import json

import pytest
from cricai_data.demo import build_demo_session

pytestmark = pytest.mark.golden

GOLDEN_SHA256 = "66a672fa75e12877f80e7424e1a4e9230c52b8eda6cdd2f3202f1646db810c5c"


def test_demo_session_digest_is_pinned() -> None:
    payload = json.dumps(build_demo_session().to_dict(), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == GOLDEN_SHA256
