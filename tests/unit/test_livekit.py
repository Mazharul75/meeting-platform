import uuid

import jwt as pyjwt
import pytest

from app.config import Settings
from app.services import participant_names
from app.services.e2ee_keys import meeting_e2ee_key
from app.services.livekit_tokens import create_join_token, new_identity


@pytest.fixture
def settings() -> Settings:
    return Settings(
        livekit_url="wss://x.livekit.cloud", livekit_api_key="key1", livekit_api_secret="s3cret" * 6,
        _env_file=None,
    )


def _claims(token: str, secret: str) -> dict:
    return pyjwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})


def test_identity_is_random_and_unlinked_to_real_names():
    a, b = new_identity(), new_identity()
    assert a != b and len(a) == 32  # uuid4 hex, not a name or user id


def test_token_grants_exactly_one_room_and_expires_in_10_minutes(settings):
    identity = new_identity()
    token = create_join_token(settings, "room-abc", identity)
    claims = _claims(token, settings.livekit_api_secret)
    assert claims["sub"] == identity
    assert claims["video"] == {
        "roomJoin": True, "room": "room-abc", "canPublish": True, "canSubscribe": True, "canPublishData": True,
    }
    assert claims["exp"] - claims["nbf"] == 600


def test_token_is_scoped_to_its_own_room_only(settings):
    token = create_join_token(settings, "room-a", new_identity())
    claims = _claims(token, settings.livekit_api_secret)
    assert claims["video"]["room"] == "room-a"
    assert claims["video"]["room"] != "room-b"


def test_wrong_secret_cannot_read_the_token(settings):
    token = create_join_token(settings, "room-a", new_identity())
    with pytest.raises(pyjwt.InvalidSignatureError):
        _claims(token, "a-completely-different-secret")


def test_e2ee_key_is_deterministic_per_meeting_and_differs_across_meetings():
    m1, m2 = uuid.uuid4(), uuid.uuid4()
    k1a = meeting_e2ee_key("master-key-value", m1)
    k1b = meeting_e2ee_key("master-key-value", m1)
    k2 = meeting_e2ee_key("master-key-value", m2)
    assert k1a == k1b and k1a != k2
    assert len(k1a) == 64  # 32-byte key, hex-encoded


def test_e2ee_key_changes_with_a_different_master_key():
    m = uuid.uuid4()
    assert meeting_e2ee_key("key-one", m) != meeting_e2ee_key("key-two", m)


def test_participant_names_registry_scopes_by_room_and_expires():
    participant_names.reset_all()
    participant_names.register("room-a", "id1", "Alice")
    participant_names.register("room-b", "id2", "Bob")
    assert participant_names.names_for_room("room-a") == {"id1": "Alice"}
    assert participant_names.names_for_room("room-b") == {"id2": "Bob"}
    assert participant_names.names_for_room("room-c") == {}


def test_participant_name_registry_truncates_long_names():
    participant_names.reset_all()
    participant_names.register("room-a", "id1", "x" * 500)
    assert len(participant_names.names_for_room("room-a")["id1"]) == 60
