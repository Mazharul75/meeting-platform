from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import clock
from app.config import Settings
from app.logging_setup import redact
from app.models import GuestInvite, Meeting
from app.security import permissions as perm
from app.security.headers import build_csp, security_headers
from app.security.passwords import hash_password, validate_new_password, verify_password
from app.sentry_scrub import scrub_event
from app.services import meetings as svc
from app.services.storage import LocalDevStorage, StorageError, SupabaseStorage, part_path


def test_prod_refuses_missing_secret():
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(app_env="prod", secret_key="", master_key="", storage_backend="", _env_file=None)


def test_prod_needs_https_and_supabase():
    with pytest.raises(ValueError, match="APP_BASE_URL"):
        Settings(
            app_env="prod", secret_key="s" * 40, master_key="m" * 40, database_url="postgresql://x", storage_backend="",
            supabase_url="https://a.supabase.co", supabase_service_key="k", app_base_url="http://x", _env_file=None,
        )


def test_prod_ok_and_headers():
    s = Settings(
        app_env="prod", secret_key="s" * 40, master_key="m" * 40, database_url="postgresql://x", storage_backend="",
        supabase_url="https://a.supabase.co", supabase_service_key="k", app_base_url="https://x.io",
        livekit_url="wss://lk.example.com", _env_file=None,
    )
    csp = build_csp(s)
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "https://a.supabase.co" in csp and "wss://lk.example.com" in csp
    h = security_headers(s)
    assert "Strict-Transport-Security" in h and h["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in csp


def test_passwords():
    assert validate_new_password("short") is not None
    assert validate_new_password("a" * 12) is None
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")


# Expected matrix written out from plan section 6.3 (independent of the implementation).
EXPECTED = {
    "edit_meeting": dict(admin=1, host=1, invited=0, other=0, guest=0),
    "view_meeting": dict(admin=1, host=1, invited=1, other=0, guest=0),
    "join_room": dict(admin=1, host=1, invited=1, other=0, guest=1),
    "manage_guest_links": dict(admin=1, host=1, invited=0, other=0, guest=0),
    "record": dict(admin=0, host=1, invited=0, other=0, guest=0),
    "play_recording": dict(admin=1, host=1, invited=1, other=0, guest=0),
    "download_recording": dict(admin=1, host=1, invited=0, other=0, guest=0),
    "delete_recording": dict(admin=1, host=1, invited=0, other=0, guest=0),
}


@pytest.mark.parametrize("action", list(EXPECTED))
@pytest.mark.parametrize("role", ["admin", "host", "invited", "other", "guest"])
def test_permission_matrix(role, action):
    assert perm.is_allowed(role, action) == bool(EXPECTED[action][role])


def test_matrix_covers_every_action():
    assert set(EXPECTED) == set(perm.ALL_ACTIONS)


def _meeting(status="scheduled", start=None):
    start = start or datetime.now(UTC) + timedelta(days=1)
    return Meeting(title="t", scheduled_start=start, scheduled_end=start + timedelta(hours=1), status=status)


@pytest.mark.parametrize(
    "old,new,ok",
    [("scheduled", "live", 1), ("scheduled", "cancelled", 1), ("live", "ended", 1), ("ended", "live", 0),
     ("cancelled", "live", 0), ("live", "scheduled", 0), ("scheduled", "ended", 0), ("ended", "cancelled", 0)],
)
def test_status_machine(old, new, ok):
    m = _meeting(old)
    if ok:
        svc.transition(m, new)
        assert m.status == new
    else:
        with pytest.raises(svc.IllegalTransition):
            svc.transition(m, new)


def _form(**over):
    base = dict(title="Weekly", description="", mode="online", start_local="2099-01-01T10:00",
                duration_minutes=60, timezone="Asia/Dhaka")
    base.update(over)
    return svc.parse_meeting_form(**base)


def test_form_valid_converts_to_utc():
    parsed, errors = _form()
    assert not errors and parsed.start_utc == datetime(2099, 1, 1, 4, 0, tzinfo=UTC)
    assert parsed.end_utc - parsed.start_utc == timedelta(minutes=60)


@pytest.mark.parametrize(
    "over,field",
    [(dict(title=""), "title"), (dict(title="x" * 300), "title"), (dict(timezone="Mars/Base"), "timezone"),
     (dict(start_local="2001-01-01T10:00"), "start"), (dict(start_local="nonsense"), "start"),
     (dict(mode="in_person"), "mode"), (dict(duration_minutes=1), "duration")],
)
def test_form_rejects(over, field):
    parsed, errors = _form(**over)
    assert parsed is None and field in errors


def test_guest_link_states():
    m = _meeting(start=datetime.now(UTC) + timedelta(hours=5))
    m.id = __import__("uuid").uuid4()
    token, inv = svc.new_guest_invite(m, "client")
    assert len(token) >= 43 and inv.token_hash == svc.hash_guest_token(token) and token not in inv.token_hash
    now = datetime.now(UTC)
    assert svc.guest_invite_state(inv, m, now) == "not_yet"
    assert svc.guest_invite_state(inv, m, m.scheduled_start) == "ok"
    assert svc.guest_invite_state(inv, m, m.scheduled_end + timedelta(hours=5)) == "expired"
    inv.revoked_at = now
    assert svc.guest_invite_state(inv, m, m.scheduled_start) == "revoked"
    inv.revoked_at = None
    m.status = "cancelled"
    assert svc.guest_invite_state(inv, m, m.scheduled_start) == "meeting_closed"
    assert isinstance(inv, GuestInvite)


def test_redaction_and_sentry_scrub():
    text = "GET /x?token=abc123&sig=zzz user bob@example.com Bearer eyJhbGciOi /join/" + "A" * 30
    out = redact(text)
    for leak in ("abc123", "zzz", "bob@example.com", "eyJhbGciOi", "A" * 30):
        assert leak not in out
    ev = scrub_event({"request": {"cookies": {"s": "1"}, "headers": {"a": "b"}, "url": "https://x/y?token=1"},
                      "user": {"email": "a@b.co"}, "exception": {"values": [{"value": "bad a@b.co"}]}})
    assert "cookies" not in ev["request"] and "user" not in ev and "a@b.co" not in str(ev)


def test_part_path_is_server_format():
    assert part_path("M", "R", 3, "video/webm") == "recordings/M/R/part-0003.webm"


async def test_local_storage_signatures(tmp_path):
    st = LocalDevStorage(tmp_path, "secret")
    url = await st.create_download_url("recordings/a/b/part-0001.mp4", 600, "n.mp4")
    assert url.startswith("/_local_storage/download?") and "sig=" in url
    exp = 2**31
    sig = st.sign("get", "p", exp)
    assert st.verify("get", "p", exp, sig) and not st.verify("get", "q", exp, sig)
    assert not st.verify("put", "p", exp, sig) and not st.verify("get", "p", 1, st.sign("get", "p", 1))
    with pytest.raises(StorageError):
        st.file_path("../../etc/passwd")


async def test_supabase_storage_calls():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("x-upsert"), req.headers["authorization"]))
        p = req.url.path
        if p.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(200, json={"url": "/object/upload/sign/recordings/x?token=T"})
        if p.startswith("/storage/v1/object/sign/"):
            return httpx.Response(200, json={"signedURL": "/object/sign/recordings/x?token=D"})
        if p.startswith("/storage/v1/object/authenticated/"):
            if "missing" in p:
                return httpx.Response(400, json={"statusCode": "404"})
            return httpx.Response(200, headers={"content-length": "1234"})
        if req.method == "DELETE":
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    st = SupabaseStorage("https://p.supabase.co", "KEY", "recordings", httpx.MockTransport(handler))
    up = await st.create_upload("recordings/m/r/part-0001.mp4", "video/mp4", True)
    assert up.url == "https://p.supabase.co/storage/v1/object/upload/sign/recordings/x?token=T"
    assert up.headers["x-upsert"] == "true" and up.body_kind == "multipart"
    assert await st.stat("recordings/m/r/part-0001.mp4") == 1234
    assert await st.stat("recordings/missing.mp4") is None
    d = await st.create_download_url("p", 600, "a b.mp4")
    assert d.startswith("https://p.supabase.co/storage/v1/object/sign/") and "download=a+b.mp4" in d
    await st.delete(["p1"])
    assert all(s[3] == "Bearer KEY" for s in seen)
    assert clock.utcnow().tzinfo is not None
