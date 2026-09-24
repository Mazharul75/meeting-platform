import hashlib

from sqlalchemy import select

from app.models import AuditLog, Meeting, Recording, RecordingPart


def H(c):
    return {"X-CSRF-Token": c.csrf}


async def start(c, m, consent=True, mime="video/webm;codecs=vp9,opus"):
    return await c.post(f"/api/meetings/{m.id}/recordings", headers=H(c), json={"mime_type": mime, "consent": consent})


async def upload_part(c, storage, rid, n, data: bytes, dur=1000):
    r = await c.post(f"/api/recordings/{rid}/parts/{n}/upload-url", headers=H(c))
    assert r.status_code == 200, r.text
    from app.models import RecordingPart  # noqa: F401

    return r


async def put_and_complete(c, session, storage, rid, n, data: bytes, dur=1000, claimed=None):
    up = await upload_part(c, storage, rid, n, data)
    part = (await session.execute(select(RecordingPart).where(RecordingPart.part_number == n))).scalars().all()[-1]
    storage.put(part.storage_path, data)
    return await c.post(f"/api/recordings/{rid}/parts/{n}/complete", headers=H(c),
                        json={"size_bytes": claimed or len(data), "duration_ms": dur, "sha256": hashlib.sha256(data).hexdigest()}), up


async def test_consent_and_host_only_start(login_as, make_user, make_meeting, session):
    host, inv, other = await make_user("h@x.com"), await make_user("i@x.com"), await make_user("o@x.com")
    m = await make_meeting(host, [inv])
    hc, ic, oc = await login_as(host), await login_as(inv), await login_as(other)
    assert (await start(hc, m, consent=False)).json() == {"error": "consent_required"}  # server-side gate
    assert (await start(ic, m)).status_code == 403  # REC-02: invited staff
    assert (await start(oc, m)).status_code == 404
    assert (await start(hc, m, mime="application/pdf")).status_code == 400
    r = await start(hc, m)
    assert r.status_code == 201 and r.json()["mime_type"] == "video/webm" and r.json()["part_seconds"] == 240
    await session.refresh(m)
    assert m.status == "live"
    actions = [a for (a,) in (await session.execute(select(AuditLog.action))).all()]
    assert "recording.consent" in actions and "recording.start" in actions
    audio = await start(hc, m, mime="audio/mp4")
    assert audio.json()["mime_type"] == "video/mp4"  # bucket only accepts video/* types


async def test_admin_who_is_not_host_cannot_record(login_as, make_user, make_meeting):
    admin, host = await make_user("a@x.com", "admin"), await make_user("h@x.com")
    m = await make_meeting(host)
    assert (await start(await login_as(admin), m)).status_code == 403


async def test_full_upload_flow_and_path_chosen_by_server(login_as, make_user, make_meeting, session, storage):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    r1, up1 = await put_and_complete(c, session, storage, rid, 1, b"a" * 1000, 4000)
    r2, _ = await put_and_complete(c, session, storage, rid, 2, b"b" * 500, 2000)
    assert r1.json() == {"status": "verified"} and r2.status_code == 200
    paths = [p for p, _ in storage.upload_requests]
    assert paths[0] == f"recordings/{m.id}/{rid}/part-0001.webm" and paths[1].endswith("part-0002.webm")
    assert up1.json()["body_kind"] == "raw" and storage.upload_requests[0][1] is True
    # finish is idempotent (REC-05)
    for _ in range(2):
        f = await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 2})
        assert f.json() == {"status": "ready", "missing": []}
    rec = (await session.execute(select(Recording))).scalar_one()
    await session.refresh(rec)
    assert rec.status == "ready" and rec.total_bytes == 1500 and rec.total_duration_ms == 6000
    # a verified part cannot be re-issued an upload link (part 2 cannot overwrite part 1, nor itself)
    again = await c.post(f"/api/recordings/{rid}/parts/1/upload-url", headers=H(c))
    assert again.status_code == 409 and again.json() == {"error": "already_verified"}
    # retrying complete is safe
    part = (await session.execute(select(RecordingPart).where(RecordingPart.part_number == 1))).scalar_one()
    retry = await c.post(f"/api/recordings/{rid}/parts/1/complete", headers=H(c),
                         json={"size_bytes": 1000, "duration_ms": 4000, "sha256": "a" * 64})
    assert retry.status_code == 200 and part.status == "verified"


async def test_upload_verification_failures(login_as, make_user, make_meeting, session, storage):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    await upload_part(c, storage, rid, 1, b"")
    ok = {"size_bytes": 10, "duration_ms": 1, "sha256": "a" * 64}
    # file never uploaded (UPL-02/03)
    assert (await c.post(f"/api/recordings/{rid}/parts/1/complete", headers=H(c), json=ok)).json() == {"error": "not_in_storage"}
    # wrong size
    r, _ = await put_and_complete(c, session, storage, rid, 2, b"x" * 20, claimed=25)
    assert r.status_code == 409 and r.json() == {"error": "size_mismatch"}
    part2 = (await session.execute(select(RecordingPart).where(RecordingPart.part_number == 2))).scalar_one()
    await session.refresh(part2)
    assert part2.status == "uploaded"  # not verified
    # complete for a part that never asked for a link
    assert (await c.post(f"/api/recordings/{rid}/parts/9/complete", headers=H(c), json=ok)).status_code == 404
    # bad inputs
    assert (await c.post(f"/api/recordings/{rid}/parts/0/upload-url", headers=H(c))).status_code == 404
    assert (await c.post(f"/api/recordings/{rid}/parts/501/upload-url", headers=H(c))).status_code == 404
    assert (await c.post(f"/api/recordings/{rid}/parts/1/complete", headers=H(c), json={**ok, "size_bytes": 0})).status_code == 422
    assert (await c.post(f"/api/recordings/{rid}/parts/1/complete", headers=H(c), json={**ok, "sha256": "z" * 64})).status_code == 422
    # missing part 1 and 2 -> partial with the missing numbers; failed when nothing recorded
    f = await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 3})
    assert f.json() == {"status": "partial", "missing": [1, 2, 3]}
    assert (await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 0})).json()["status"] == "failed"


async def test_partial_recovers_to_ready_after_late_upload(login_as, make_user, make_meeting, session, storage):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    await put_and_complete(c, session, storage, rid, 1, b"a" * 10)
    assert (await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 2})).json()["status"] == "partial"
    await put_and_complete(c, session, storage, rid, 2, b"b" * 10)
    assert (await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 2})).json()["status"] == "ready"


async def test_recording_api_hidden_from_others(login_as, make_user, make_meeting, session, storage):
    host, inv, other = await make_user("h@x.com"), await make_user("i@x.com"), await make_user("o@x.com")
    m = await make_meeting(host, [inv])
    hc = await login_as(host)
    rid = (await start(hc, m)).json()["recording_id"]
    body = {"size_bytes": 1, "duration_ms": 1, "sha256": "a" * 64}
    oc, ic = await login_as(other), await login_as(inv)
    for c, code in ((oc, 404), (ic, 403)):
        assert (await c.post(f"/api/recordings/{rid}/parts/1/upload-url", headers=H(c))).status_code == code
        assert (await c.post(f"/api/recordings/{rid}/parts/1/complete", headers=H(c), json=body)).status_code == code
        assert (await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 1})).status_code == code
        assert (await c.post(f"/api/recordings/{rid}/note", headers=H(c), json={"code": "tab_hidden"})).status_code == code
    assert storage.upload_requests == []


async def test_library_playback_download_delete(login_as, make_user, make_meeting, session, storage):
    host, inv, other, admin = (await make_user("h@x.com"), await make_user("i@x.com"), await make_user("o@x.com"),
                               await make_user("a@x.com", "admin"))
    m = await make_meeting(host, [inv], title="Board review")
    hc = await login_as(host)
    rid = (await start(hc, m)).json()["recording_id"]
    await put_and_complete(hc, session, storage, rid, 1, b"a" * 100, 3000)
    await put_and_complete(hc, session, storage, rid, 2, b"b" * 100, 3000)
    await hc.post(f"/api/recordings/{rid}/finish", headers=H(hc), json={"parts_expected": 2})
    ic, oc, ac = await login_as(inv), await login_as(other), await login_as(admin)

    # library lists only what the user may see
    assert "Board review" in (await hc.get("/recordings")).text
    assert "Board review" in (await ic.get("/recordings")).text
    assert "Board review" in (await ac.get("/recordings")).text
    assert "Board review" not in (await oc.get("/recordings")).text
    assert "Board review" not in (await oc.get("/")).text
    partial = await hc.get("/recordings", headers={"HX-Request": "true"})
    assert "<html" not in partial.text and "Board review" in partial.text

    # playback: allowed roles get short-lived signed links; others 404 (AC-09)
    for c in (hc, ic, ac):
        page = await c.get(f"/recordings/{rid}")
        assert page.status_code == 200 and "player-config" in page.text and "part-0001.webm" in page.text
    assert (await oc.get(f"/recordings/{rid}")).status_code == 404
    assert (await oc.get(f"/api/recordings/{rid}/parts")).status_code == 404
    assert all(ttl == 600 for _, ttl in storage.download_requests)  # expire within 10 minutes
    links = (await hc.get(f"/api/recordings/{rid}/parts")).json()["parts"]
    assert [p["n"] for p in links] == [1, 2] and links[0]["duration_ms"] == 3000

    # download: host and admin yes, invited no, other 404 (DL-01)
    url = f"/recordings/{rid}/parts/1/download"
    for c, code in ((hc, 303), (ac, 303), (ic, 403), (oc, 404)):
        assert (await c.get(url)).status_code == code
    r = await hc.get(url)
    assert "storage.test/download" in r.headers["location"] and "Board-review" in r.headers["location"]
    assert (await hc.get(f"/recordings/{rid}/parts/7/download")).status_code == 404

    # delete: invited/other refused, host removes files and marks deleted
    assert (await ic.delete(f"/recordings/{rid}", headers=H(ic))).status_code == 403
    assert (await oc.delete(f"/recordings/{rid}", headers=H(oc))).status_code == 404
    assert len(storage.files) == 2
    assert (await hc.delete(f"/recordings/{rid}", headers=H(hc))).status_code == 204
    assert storage.files == {}
    rec = (await session.execute(select(Recording))).scalar_one()
    await session.refresh(rec)
    assert rec.status == "deleted"
    assert (await hc.get(f"/recordings/{rid}")).status_code == 404
    assert "Board review" not in (await hc.get("/recordings")).text
    actions = [a for (a,) in (await session.execute(select(AuditLog.action))).all()]
    for a in ("recording.view", "recording.download", "recording.delete", "recording.finish"):
        assert a in actions


async def test_storage_meter_counts_only_live_recordings(login_as, make_user, make_meeting, session, storage):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    await put_and_complete(c, session, storage, rid, 1, b"z" * 2048)
    await c.post(f"/api/recordings/{rid}/finish", headers=H(c), json={"parts_expected": 1})
    assert "2.0 KB" in (await c.get("/")).text
    await c.delete(f"/recordings/{rid}", headers=H(c))
    assert "0 B of" in (await c.get("/")).text


async def test_note_endpoint_whitelist(login_as, make_user, make_meeting, session):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    assert (await c.post(f"/api/recordings/{rid}/note", headers=H(c), json={"code": "tab_hidden"})).status_code == 200
    assert (await c.post(f"/api/recordings/{rid}/note", headers=H(c), json={"code": "<script>"})).status_code == 422
    notes = (await session.execute(select(AuditLog).where(AuditLog.action == "recording.note"))).scalars().all()
    assert len(notes) == 1 and notes[0].details == {"code": "tab_hidden"}
    assert isinstance(m, Meeting)


async def test_rate_limit_on_upload_urls(login_as, make_user, make_meeting):
    host = await make_user("h@x.com")
    m = await make_meeting(host)
    c = await login_as(host)
    rid = (await start(c, m)).json()["recording_id"]
    codes = {(await c.post(f"/api/recordings/{rid}/parts/1/upload-url", headers=H(c))).status_code for _ in range(245)}
    assert 429 in codes
