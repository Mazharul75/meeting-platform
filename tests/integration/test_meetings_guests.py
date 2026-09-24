import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import clock
from app.models import AuditLog, GuestInvite, Meeting
from tests.conftest import csrf_of


def _local(days=2):
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT10:00")


async def test_schedule_meeting_ac02_and_dashboard(login_as, make_user, session):
    host = await make_user("h@x.com")
    inv = await make_user("i@x.com", name="Ivy")
    c = await login_as(host)
    page = await c.get("/meetings/new")
    assert page.status_code == 200 and "Ivy" in page.text
    r = await c.post("/meetings/new", data={"csrf_token": c.csrf, "title": "Kickoff", "description": "d", "mode": "online",
                                            "start": _local(), "duration": "45", "timezone": "Asia/Dhaka", "user_ids": [str(inv.id)]})
    assert r.status_code == 303
    m = (await session.execute(select(Meeting))).scalar_one()
    assert m.status == "scheduled" and m.host_id == host.id and len(m.members) == 2
    assert m.scheduled_start.hour == 4  # 10:00 Dhaka = 04:00 UTC
    for who in (host, inv):
        cc = await login_as(who)
        assert "Kickoff" in (await cc.get("/")).text
    detail = await c.get(r.headers["location"])
    assert detail.status_code == 200 and "Meeting was scheduled" not in detail.text and "Kickoff" in detail.text
    assert "meeting.create" in [a for (a,) in (await session.execute(select(AuditLog.action))).all()]


async def test_schedule_validation_errors(login_as, make_user):
    c = await login_as(await make_user("h@x.com"))
    base = {"csrf_token": c.csrf, "title": "T", "mode": "online", "start": _local(), "duration": "30", "timezone": "Asia/Dhaka"}
    for over in ({"title": ""}, {"title": "x" * 300}, {"timezone": "Nowhere/Land"}, {"start": "2001-01-01T10:00"},
                 {"mode": "in_person"}, {"user_ids": ["00000000-0000-0000-0000-000000000009"]}):
        r = await c.post("/meetings/new", data={**base, **over})
        assert r.status_code == 422, over


async def test_other_staff_get_404_everywhere_mtg04(login_as, make_user, make_meeting):
    host, other = await make_user("h@x.com"), await make_user("o@x.com")
    m = await make_meeting(host)
    c = await login_as(other)
    for path in (f"/meetings/{m.id}", f"/meetings/{m.id}/calendar.ics", f"/meetings/{m.id}/lobby"):
        assert (await c.get(path)).status_code == 404, path
    for path in (f"/meetings/{m.id}/cancel", f"/meetings/{m.id}/guest-links", f"/meetings/{m.id}/invite", f"/meetings/{m.id}/end",
                 f"/api/meetings/{m.id}/end", f"/api/meetings/{m.id}/recordings"):
        kw = {"json": {"mime_type": "video/webm", "consent": True}} if path.endswith("recordings") else {"data": {"csrf_token": c.csrf}}
        r = await c.post(path, headers={"X-CSRF-Token": c.csrf}, **kw)
        assert r.status_code == 404, path
    assert "Weekly sync" not in (await c.get("/")).text


async def test_invited_can_view_but_not_manage(login_as, make_user, make_meeting):
    host, inv = await make_user("h@x.com"), await make_user("i@x.com")
    m = await make_meeting(host, [inv])
    c = await login_as(inv)
    assert (await c.get(f"/meetings/{m.id}")).status_code == 200
    assert (await c.get(f"/meetings/{m.id}/lobby")).status_code == 200
    assert (await c.post(f"/meetings/{m.id}/cancel", data={"csrf_token": c.csrf})).status_code == 403
    assert (await c.post(f"/meetings/{m.id}/guest-links", data={"csrf_token": c.csrf})).status_code == 403


async def test_ics_ac03(login_as, make_user, make_meeting):
    host = await make_user("h@x.com")
    m = await make_meeting(host, title="Board, Q3; review")
    r = await (await login_as(host)).get(f"/meetings/{m.id}/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert "BEGIN:VCALENDAR" in body and "BEGIN:VEVENT" in body and f"UID:{m.id}@meeting-platform" in body
    assert "DTSTART:" in body and body.count("Z") >= 2 and "END:VCALENDAR" in body


async def test_cancel_then_cannot_start_mtg05(login_as, make_user, make_meeting):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    assert (await c.post(f"/meetings/{m.id}/cancel", data={"csrf_token": c.csrf})).status_code == 303
    assert (await c.post(f"/meetings/{m.id}/cancel", data={"csrf_token": c.csrf})).status_code == 409
    r = await c.post(f"/api/meetings/{m.id}/recordings", headers={"X-CSRF-Token": c.csrf}, json={"mime_type": "video/webm", "consent": True})
    assert r.status_code == 409
    assert (await c.post(f"/api/meetings/{m.id}/end", headers={"X-CSRF-Token": c.csrf})).status_code == 409  # MTG-07


async def test_invite_more_staff(login_as, make_user, make_meeting, session):
    host, other = await make_user("h@x.com"), await make_user("o@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    r = await c.post(f"/meetings/{m.id}/invite", data={"csrf_token": c.csrf, "user_ids": [str(other.id)]})
    assert r.status_code == 303
    assert (await (await login_as(other)).get(f"/meetings/{m.id}")).status_code == 200


async def _make_link(c, m, label="Client"):
    r = await c.post(f"/meetings/{m.id}/guest-links", data={"csrf_token": c.csrf, "label": label})
    assert r.status_code == 200
    link = re.search(r"http://testserver/join/([\w-]+)", r.text)
    assert link, r.text[:500]
    return link.group(1), r


async def test_guest_link_lifecycle(login_as, make_user, make_meeting, make_client, session):
    host = await make_user("h@x.com")
    m = await make_meeting(host, start=datetime.now(UTC) + timedelta(minutes=10))
    c = await login_as(host)
    token, resp = await _make_link(c, m)
    assert "shown only once" in resp.text
    again = await c.get(f"/meetings/{m.id}")
    assert token not in again.text  # never shown again
    inv = (await session.execute(select(GuestInvite))).scalar_one()
    assert inv.token_hash != token and token not in inv.token_hash

    g = make_client()
    ok = await g.get(f"/join/{token}")
    assert ok.status_code == 200 and "Weekly sync" in ok.text and "recorded" in ok.text
    # consent + name required
    bad = await g.post(f"/join/{token}", data={"csrf_token": await csrf_of(g, f"/join/{token}"), "name": "", "consent": ""})
    assert bad.status_code == 422
    good = await g.post(f"/join/{token}", data={"csrf_token": await csrf_of(g, f"/join/{token}"), "name": "Cara", "consent": "yes"})
    assert good.status_code == 200 and "Cara" in good.text
    assert "guest.consent" in [a for (a,) in (await session.execute(select(AuditLog.action))).all()]

    # GST-04: a guest reaches no staff page and no recording
    for path in ("/", "/recordings", "/meetings/new", f"/meetings/{m.id}", f"/meetings/{m.id}/lobby", "/admin/users"):
        r = await g.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert (await g.get("/join/not-a-real-token")).status_code == 404  # GST-05: wrong token

    # revoke (GST-03)
    await session.refresh(inv)
    r = await c.delete(f"/meetings/{m.id}/guest-links/{inv.id}", headers={"X-CSRF-Token": c.csrf})
    assert r.status_code == 204
    assert (await g.get(f"/join/{token}")).status_code == 404
    await g.aclose()


async def test_guest_link_window_and_closed_meeting(login_as, make_user, make_meeting, make_client):
    host = await make_user("h@x.com")
    m = await make_meeting(host, start=datetime.now(UTC) + timedelta(days=3))
    c = await login_as(host)
    token, _ = await _make_link(c, m)
    g = make_client()
    assert (await g.get(f"/join/{token}")).status_code == 404  # too early: 30 min before start
    clock.set_clock(lambda: m.scheduled_start - timedelta(minutes=29))
    assert (await g.get(f"/join/{token}")).status_code == 200
    clock.set_clock(lambda: m.scheduled_end + timedelta(hours=4, minutes=1))
    assert (await g.get(f"/join/{token}")).status_code == 404  # GST-02 expired
    clock.set_clock(None)
    await c.post(f"/meetings/{m.id}/cancel", data={"csrf_token": c.csrf})
    clock.set_clock(lambda: m.scheduled_start)
    assert (await g.get(f"/join/{token}")).status_code == 404
    await g.aclose()


async def test_guest_link_only_for_host_and_open_meetings(login_as, make_user, make_meeting):
    host = await make_user("h@x.com")
    m = await make_meeting(host, status="ended")
    c = await login_as(host)
    r = await c.post(f"/meetings/{m.id}/guest-links", data={"csrf_token": c.csrf})
    assert r.status_code == 409


async def test_end_meeting_flow(login_as, make_user, make_meeting, session):
    host = await make_user("h@x.com")
    m = await make_meeting(host, status="live")
    c = await login_as(host)
    assert (await c.post(f"/meetings/{m.id}/end", data={"csrf_token": c.csrf})).status_code == 303
    await session.refresh(m)
    assert m.status == "ended" and m.ended_at is not None


async def test_error_pages_have_no_details(login_as, make_user):
    c = await login_as(await make_user("h@x.com"))
    r = await c.get("/meetings/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and "Traceback" not in r.text and "Page not found" in r.text
    assert (await c.get("/dev/nothing")).status_code == 404
