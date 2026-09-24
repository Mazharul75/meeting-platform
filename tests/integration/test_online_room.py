import re
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog
from tests.conftest import csrf_of


def H(c):
    return {"X-CSRF-Token": c.csrf}


def _configure_livekit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_url", "wss://fake.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "test-secret-" + "x" * 20)
    return settings


async def test_no_video_until_livekit_is_configured(login_as, make_user, make_meeting):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    assert (await c.get(f"/meetings/{m.id}/room")).status_code == 503
    r = await c.post(f"/api/meetings/{m.id}/livekit-token", headers=H(c), json={})
    assert r.status_code == 503


async def test_token_issued_to_host_invited_admin_not_other(login_as, make_user, make_meeting, monkeypatch, session):
    _configure_livekit(monkeypatch)
    host, inv, other, admin = (await make_user("h@x.com"), await make_user("i@x.com"), await make_user("o@x.com"),
                               await make_user("a@x.com", "admin"))
    m = await make_meeting(host, [inv])
    hc, ic, ac, oc = await login_as(host), await login_as(inv), await login_as(admin), await login_as(other)

    assert (await oc.post(f"/api/meetings/{m.id}/livekit-token", headers=H(oc), json={})).status_code == 404

    for c in (hc, ic, ac):
        r = await c.post(f"/api/meetings/{m.id}/livekit-token", headers=H(c), json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == "wss://fake.livekit.cloud"
        assert len(body["identity"]) == 32 and body["identity"] not in (str(host.id), str(inv.id))
        claims = pyjwt.decode(body["token"], get_settings().livekit_api_secret, algorithms=["HS256"],
                              options={"verify_aud": False})
        assert claims["video"]["room"] == str(m.room_name)
        assert claims["exp"] - claims["nbf"] == 600
        assert len(body["e2ee_key"]) == 64

    actions = [a for (a,) in (await session.execute(select(AuditLog.action))).all()]
    assert "livekit.token" in actions and "meeting.live" in actions


async def test_first_token_moves_scheduled_meeting_to_live(login_as, make_user, make_meeting, monkeypatch, session):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    assert (await c.post(f"/api/meetings/{m.id}/livekit-token", headers=H(c), json={})).status_code == 200
    await session.refresh(m)
    assert m.status == "live"


async def test_token_refused_for_closed_meeting(login_as, make_user, make_meeting, monkeypatch):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m = await make_meeting(host, status="ended")
    c = await login_as(host)
    r = await c.post(f"/api/meetings/{m.id}/livekit-token", headers=H(c), json={})
    assert r.status_code == 409


async def test_same_e2ee_key_every_time_for_one_meeting_different_for_another(
    login_as, make_user, make_meeting, monkeypatch
):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m1 = await make_meeting(host, title="one")
    m2 = await make_meeting(host, title="two")
    c = await login_as(host)
    k1a = (await c.post(f"/api/meetings/{m1.id}/livekit-token", headers=H(c), json={})).json()["e2ee_key"]
    k1b = (await c.post(f"/api/meetings/{m1.id}/livekit-token", headers=H(c), json={})).json()["e2ee_key"]
    k2 = (await c.post(f"/api/meetings/{m2.id}/livekit-token", headers=H(c), json={})).json()["e2ee_key"]
    assert k1a == k1b and k1a != k2


async def test_participants_list_only_visible_to_allowed_roles(login_as, make_user, make_meeting, monkeypatch):
    _configure_livekit(monkeypatch)
    host, other = await make_user("h@x.com"), await make_user("o@x.com")
    m = await make_meeting(host)
    hc, oc = await login_as(host), await login_as(other)
    await hc.post(f"/api/meetings/{m.id}/livekit-token", headers=H(hc), json={})
    r = await hc.get(f"/api/meetings/{m.id}/participants")
    assert r.status_code == 200 and host.display_name in r.json()["participants"].values()
    assert (await oc.get(f"/api/meetings/{m.id}/participants")).status_code == 404


async def test_only_host_can_end_and_it_closes_the_room(login_as, make_user, make_meeting, monkeypatch, session):
    settings = _configure_livekit(monkeypatch)
    calls = []

    async def fake_close(s, room_name):
        calls.append(room_name)

    from app.routers import meetings as meetings_router

    monkeypatch.setattr(meetings_router.livekit_tokens, "close_room", fake_close)
    host, inv = await make_user("h@x.com"), await make_user("i@x.com")
    m = await make_meeting(host, [inv], status="live")
    hc, ic = await login_as(host), await login_as(inv)
    assert (await ic.post(f"/api/meetings/{m.id}/end", headers=H(ic))).status_code == 403
    r = await hc.post(f"/api/meetings/{m.id}/end", headers=H(hc))
    assert r.status_code == 200 and calls == [str(m.room_name)]
    await session.refresh(m)
    assert m.status == "ended"
    assert settings.livekit_url  # sanity: fixture actually configured


async def test_guest_gets_token_and_own_identity_each_time(login_as, make_user, make_meeting, make_client, monkeypatch):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m = await make_meeting(host, start=datetime.now(UTC) + timedelta(minutes=5))
    c = await login_as(host)
    r = await c.post(f"/meetings/{m.id}/guest-links", data={"csrf_token": c.csrf, "label": "Client"})
    token = re.search(r"/join/([\w-]+)", r.text).group(1)

    g = make_client()
    room_page = await g.get(f"/join/{token}/room?name=Cara")
    assert room_page.status_code == 200 and "room-config" in room_page.text
    gh = {"X-CSRF-Token": await csrf_of(g, f"/join/{token}/room?name=Cara")}

    r1 = await g.post(f"/join/{token}/livekit-token?name=Cara", headers=gh)
    r2 = await g.post(f"/join/{token}/livekit-token?name=Cara", headers=gh)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["identity"] != r2.json()["identity"]
    assert r1.json()["display_name"] == "Cara"
    await g.aclose()


async def test_guest_cannot_join_room_of_another_meeting(login_as, make_user, make_meeting, make_client, monkeypatch):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m1 = await make_meeting(host, start=datetime.now(UTC) + timedelta(minutes=5), title="one")
    await make_meeting(host, start=datetime.now(UTC) + timedelta(minutes=5), title="two")
    c = await login_as(host)
    r = await c.post(f"/meetings/{m1.id}/guest-links", data={"csrf_token": c.csrf})
    token = re.search(r"/join/([\w-]+)", r.text).group(1)
    g = make_client()
    gh = {"X-CSRF-Token": await csrf_of(g, f"/join/{token}")}
    ok = await g.post(f"/join/{token}/livekit-token", headers=gh)
    assert ok.status_code == 200
    claims = pyjwt.decode(ok.json()["token"], get_settings().livekit_api_secret, algorithms=["HS256"],
                          options={"verify_aud": False})
    assert claims["video"]["room"] == str(m1.room_name)
    assert (await g.post("/join/not-a-real-token/livekit-token", headers=gh)).status_code == 404
    await g.aclose()


async def test_guest_participants_endpoint_scoped_to_own_meeting(login_as, make_user, make_meeting, make_client, monkeypatch):
    _configure_livekit(monkeypatch)
    host = await make_user("h@x.com")
    m = await make_meeting(host, start=datetime.now(UTC) + timedelta(minutes=5))
    c = await login_as(host)
    r = await c.post(f"/meetings/{m.id}/guest-links", data={"csrf_token": c.csrf})
    token = re.search(r"/join/([\w-]+)", r.text).group(1)
    g = make_client()
    gh = {"X-CSRF-Token": await csrf_of(g, f"/join/{token}")}
    await g.post(f"/join/{token}/livekit-token?name=Cara", headers=gh)
    names = await g.get(f"/join/{token}/participants")
    assert names.status_code == 200 and "Cara" in names.json()["participants"].values()
    await g.aclose()
