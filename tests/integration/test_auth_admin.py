from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app import clock
from app.config import Settings
from app.models import AuditLog, Session, User
from app.security import sessions
from tests.conftest import PASSWORD, csrf_of, login


async def test_healthz_and_security_headers(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    for path in ("/healthz", "/login", "/nope", "/static/css/theme.css"):
        h = (await client.get(path)).headers
        assert "script-src 'self'" in h["content-security-policy"], path
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
        assert "camera=(self)" in h["permissions-policy"] and h["x-frame-options"] == "DENY"
    assert (await client.get("/login")).headers["cache-control"] == "no-store"


async def test_anonymous_redirected_and_guest_pages_blocked(client):
    for path in ("/", "/recordings", "/meetings/new", "/admin/users"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = await client.get("/api/recordings/00000000-0000-0000-0000-000000000000/parts")
    assert r.status_code == 401


async def test_login_success_and_cookie_flags(client, make_user):
    await make_user("a@x.com")
    r = await login(client, "a@x.com")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "session=" in cookie
    assert (await client.get("/")).status_code == 200


async def test_prod_cookie_is_host_prefixed_secure(monkeypatch):
    s = Settings(app_env="prod", secret_key="s" * 40, master_key="m" * 40, database_url="postgresql://x",
                 storage_backend="", supabase_url="https://a.supabase.co", supabase_service_key="k",
                 app_base_url="https://x.io", _env_file=None)
    monkeypatch.setattr(sessions, "get_settings", lambda: s)
    from fastapi import Response

    resp = Response()
    sessions.set_cookie(resp, sessions.cookie_name(), "v", 10)
    c = resp.headers["set-cookie"]
    assert c.startswith("__Host-session=") and "Secure" in c and "HttpOnly" in c and "SameSite=lax" in c


async def test_generic_error_does_not_leak_account(client, make_user):
    await make_user("a@x.com")
    r1 = await login(client, "a@x.com", "wrong password here")
    r2 = await login(client, "nobody@x.com", "wrong password here")
    assert r1.status_code == r2.status_code == 401
    assert r1.text.count("Invalid email or password") == r2.text.count("Invalid email or password") == 1


async def test_lockout_after_five_and_unlock_after_15_min(client, make_user):
    await make_user("a@x.com")
    t = datetime(2030, 1, 1, tzinfo=UTC)
    clock.set_clock(lambda: t)
    for _ in range(5):
        assert (await login(client, "a@x.com", "wrong password here")).status_code == 401
    # correct password is rejected while locked
    assert (await login(client, "a@x.com")).status_code == 401
    clock.set_clock(lambda: t + timedelta(minutes=14, seconds=50))
    assert (await login(client, "a@x.com")).status_code == 401
    clock.set_clock(lambda: t + timedelta(minutes=15, seconds=5))
    assert (await login(client, "a@x.com")).status_code == 303


async def test_login_rate_limit_per_ip(client):
    codes = [(await login(client, f"u{i}@x.com", "x" * 12)).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


async def test_csrf_required_everywhere(client, login_as, make_user):
    u = await make_user("a@x.com")
    r = await client.post("/login", data={"email": "a@x.com", "password": PASSWORD})
    assert r.status_code == 403
    c = await login_as(u)
    assert (await c.post("/logout")).status_code == 403
    assert (await c.post("/meetings/new", data={"title": "x"})).status_code == 403
    assert (await c.post("/api/meetings/x/end", headers={"X-CSRF-Token": "bad"})).status_code == 403
    assert (await c.post("/logout", data={"csrf_token": "0" * 64})).status_code == 403


async def test_tampered_and_expired_sessions(client, make_user, session):
    u = await make_user("a@x.com")
    await login(client, "a@x.com")
    real = client.cookies.get("session")
    client.cookies.set("session", real[:-2] + "xx")
    assert (await client.get("/")).status_code == 303
    client.cookies.set("session", real)
    assert (await client.get("/")).status_code == 200
    # idle timeout (8h)
    clock.set_clock(lambda: datetime.now(UTC) + timedelta(hours=9))
    assert (await client.get("/")).status_code == 303
    clock.set_clock(None)
    assert (await session.execute(select(func.count()).select_from(Session))).scalar_one() == 0
    # absolute timeout (7 days) even with activity
    client.cookies.clear()
    assert (await login(client, "a@x.com")).status_code == 303
    assert (await client.get("/")).status_code == 200
    t0 = datetime.now(UTC)
    for k in range(1, 28):  # active every 6 hours for 6.75 days
        clock.set_clock(lambda k=k: t0 + timedelta(hours=6 * k))
        assert (await client.get("/")).status_code == 200, k
    clock.set_clock(lambda: t0 + timedelta(days=7, minutes=1))  # idle < 8h, but past the 7 day limit
    assert (await client.get("/")).status_code == 303
    assert u


async def test_logout_deletes_server_session(client, make_user, session):
    await make_user("a@x.com")
    await login(client, "a@x.com")
    token = await csrf_of(client, "/")
    old = client.cookies.get("session")
    assert (await client.post("/logout", data={"csrf_token": token})).status_code == 303
    assert (await session.execute(select(func.count()).select_from(Session))).scalar_one() == 0
    client.cookies.set("session", old)
    assert (await client.get("/")).status_code == 303
    row = None
    assert row is None and sessions.hash_token(old)  # only a hash is ever stored


async def test_session_stores_only_hash(client, make_user, session):
    await make_user("a@x.com")
    await login(client, "a@x.com")
    row = (await session.execute(select(Session))).scalar_one()
    assert row.token_hash != client.cookies.get("session") and len(row.token_hash) == 64


async def test_admin_flow_ac01(login_as, make_user, make_client, session):
    admin = await make_user("admin@x.com", "admin")
    c = await login_as(admin)
    # weak password rejected (AUTH-07)
    r = await c.post("/admin/users", data={"csrf_token": c.csrf, "email": "s@x.com", "display_name": "Sam", "password": "12345678"})
    assert r.status_code == 422 and "at least 12" in r.text
    r = await c.post("/admin/users", data={"csrf_token": c.csrf, "email": "S@X.com", "display_name": "Sam",
                                           "password": "a-long-password-1", "role": "member"})
    assert r.status_code == 303
    dup = await c.post("/admin/users", data={"csrf_token": c.csrf, "email": "s@x.com", "display_name": "Sam",
                                             "password": "a-long-password-1"})
    assert dup.status_code == 422 and "already exists" in dup.text
    staff = make_client()
    assert (await login(staff, "s@x.com", "a-long-password-1")).status_code == 303
    assert (await staff.get("/")).status_code == 200
    assert (await staff.get("/admin/users")).status_code == 403
    actions = [a for (a,) in (await session.execute(select(AuditLog.action))).all()]
    assert "user.create" in actions and "login.ok" in actions
    await staff.aclose()


async def test_deactivate_logs_user_out_and_last_admin_protected(login_as, make_user, session):
    admin = await make_user("admin@x.com", "admin")
    staff = await make_user("s@x.com")
    ac, sc = await login_as(admin), await login_as(staff)
    assert (await sc.get("/")).status_code == 200
    r = await ac.post(f"/admin/users/{staff.id}/deactivate", data={"csrf_token": ac.csrf})
    assert r.status_code == 303
    assert (await sc.get("/")).status_code == 303  # AUTH-04
    assert (await login(sc, "s@x.com")).status_code == 401
    r = await ac.post(f"/admin/users/{admin.id}/deactivate", data={"csrf_token": ac.csrf})
    assert r.status_code == 409 and "last administrator" in r.text
    assert (await session.get(User, admin.id)).is_active


async def test_reset_password_unlocks_and_logs_out(login_as, make_user, make_client):
    admin = await make_user("admin@x.com", "admin")
    staff = await make_user("s@x.com")
    ac, sc = await login_as(admin), await login_as(staff)
    r = await ac.post(f"/admin/users/{staff.id}/reset-password", data={"csrf_token": ac.csrf, "password": "brand-new-password"})
    assert r.status_code == 303
    assert (await sc.get("/")).status_code == 303
    fresh = make_client()
    assert (await login(fresh, "s@x.com", "brand-new-password")).status_code == 303
    await fresh.aclose()
