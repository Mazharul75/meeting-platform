import re
import time
from datetime import datetime, timedelta

import pytest

from tests.e2e.conftest import ADMIN, HOST, STAFF, login

pytestmark = pytest.mark.e2e


def schedule(page, base, title, invite_staff=False, minutes_from_now=1440):
    page.goto(base + "/meetings/new")
    page.fill("#title", title)
    when = datetime.now() + timedelta(minutes=minutes_from_now)
    page.fill("#date", when.strftime("%Y-%m-%d"))
    page.fill("#time", when.strftime("%H:%M"))
    page.fill("#timezone", "Asia/Dhaka")
    if invite_staff:
        page.check("text=Sam Staff")
    page.click("button:has-text('Schedule meeting')")
    page.wait_for_url(re.compile(r".*/meetings/[0-9a-f-]{36}.*"))
    return re.search(r"/meetings/([0-9a-f-]{36})", page.url).group(1)


def test_admin_creates_staff_and_login_desktop_and_phone(server, new_page):  # AC-01
    admin = new_page()
    login(admin, server, ADMIN)
    admin.click("text=Admin")
    admin.fill("#display_name", "Nadia New")
    admin.fill("form[action='/admin/users'] #email", "nadia@e2e.test")
    admin.fill("form[action='/admin/users'] #password", "short")
    admin.click("button:has-text('Add staff')")
    assert "at least 12" in admin.content()
    admin.fill("form[action='/admin/users'] #display_name", "Nadia New")
    admin.fill("form[action='/admin/users'] #email", "nadia@e2e.test")
    admin.fill("form[action='/admin/users'] #password", "nadia-password-123")
    admin.click("button:has-text('Add staff')")
    assert "account was created" in admin.content()
    for viewport in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
        p = new_page(viewport=viewport)
        login(p, server, ("nadia@e2e.test", "nadia-password-123"))
        assert p.locator("h1").inner_text().strip() == "Dashboard"
        assert "Welcome back, Nadia" in p.content()


def test_schedule_calendar_and_permissions(server, new_page):  # AC-02, AC-03, AC-09 (UI part)
    host, staff = new_page(), new_page()
    login(host, server, HOST)
    login(staff, server, STAFF)
    mid = schedule(host, server, "Design review", invite_staff=True)
    host.goto(server + "/")
    assert "Design review" in host.content()
    staff.goto(server + "/")
    assert "Design review" in staff.content()
    host.goto(server + f"/meetings/{mid}")
    with host.expect_download() as dl:
        host.click("text=Add to calendar")
    ics = open(dl.value.path(), encoding="utf-8").read()
    assert "BEGIN:VEVENT" in ics and "Design review" in ics
    # a person who is not logged in / not invited gets nothing by changing the address
    other = new_page()
    assert other.goto(server + f"/meetings/{mid}").url.endswith("/login")
    nadia = new_page()
    login(nadia, server, ("nadia@e2e.test", "nadia-password-123"))
    assert nadia.goto(server + f"/meetings/{mid}").status == 404
    assert "Design review" not in nadia.goto(server + "/").text()


def test_guest_link_flow_in_browser(server, new_page):  # AC-10, AC-11 (consent), GST-*
    host, guest = new_page(), new_page()
    login(host, server, HOST)
    mid = schedule(host, server, "Client call", minutes_from_now=10)
    host.fill("#label", "Acme")
    host.click("button:has-text('Create link')")
    link = host.input_value("input[aria-label='Guest link']")
    assert "/join/" in link
    guest.goto(link)
    assert "Client call" in guest.content()
    guest.click("button:has-text('Continue')")
    assert "Please enter your name" in guest.content()
    guest.fill("#name", "Cara Client")
    guest.check("#consent")
    guest.click("button:has-text('Continue')")
    guest.wait_for_selector("#start-test")
    guest.click("#start-test")  # fake camera + microphone
    guest.wait_for_function("document.getElementById('preview').srcObject !== null")
    assert guest.locator("#lobby-error.d-none").count() == 1
    for path in ("/", "/recordings", f"/meetings/{mid}"):
        guest.goto(server + path)
        assert guest.url.endswith("/login")
    host.goto(server + f"/meetings/{mid}")
    # revoke through the confirm dialog that htmx shows
    host.on("dialog", lambda d: d.accept())
    host.click("button:has-text('Revoke')")
    host.wait_for_load_state("networkidle")
    guest.goto(link)
    assert "cannot be used" in guest.content()


def test_no_horizontal_scroll_on_phone_and_no_outside_requests(server, new_page):  # UI-02
    page = new_page(viewport={"width": 360, "height": 740}, bypass_csp=False)
    requests, console = [], []
    page.on("request", lambda r: requests.append(r.url))
    page.on("console", lambda m: console.append(m.text))
    login(page, server, HOST)
    for path in ("/", "/meetings/new", "/recordings"):
        page.goto(server + path)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, (path, overflow)
    page.goto(server + "/login")
    page.goto(server + "/dev/recorder-test")
    assert not [m for m in console if "Content Security Policy" in m], console  # strict CSP, no violations
    assert all(u.startswith(server) or u.startswith("data:") for u in requests), [u for u in requests if not u.startswith(server)]


def _start_meeting_page(server, new_page, title, url_params="part=6&slice=1000"):
    host = new_page()
    login(host, server, HOST)
    mid = schedule(host, server, title)
    host.goto(server + f"/dev/recorder-test?meeting={mid}&{url_params}")
    host.wait_for_selector("#btn-start")
    return host, mid


def _record(host, seconds):
    host.check("#consent")
    host.click("#btn-start")
    host.wait_for_function("document.getElementById('st-line').textContent.startsWith('Recording')")
    time.sleep(seconds)
    host.click("#btn-stop")
    host.wait_for_function("document.getElementById('st-line').textContent.includes('done')", timeout=60000)


def test_record_upload_playback_download_ac04_ac08(server, new_page):
    host, mid = _start_meeting_page(server, new_page, "Recorded sync")
    _record(host, 15)  # 6-second parts -> at least 3 parts
    host.goto(server + "/recordings")
    host.wait_for_selector("text=Recorded sync")
    assert host.locator("text=ready").count() >= 1
    host.click("text=Recorded sync")
    host.wait_for_selector("#part-list li")
    parts = host.locator("#part-list li").count()
    assert parts >= 3, parts
    video = host.locator("#player")
    host.wait_for_function("document.getElementById('player').readyState >= 1")
    duration = host.evaluate("document.getElementById('player').duration")
    assert duration > 1
    # play: time advances and, when the part ends, the next part starts automatically
    host.evaluate("document.getElementById('player').muted = true; document.getElementById('player').play()")
    host.wait_for_function("document.getElementById('player').currentTime > 0.5")
    # seek inside the part works
    host.evaluate("document.getElementById('player').currentTime = 2")
    host.wait_for_function("Math.abs(document.getElementById('player').currentTime - 2) < 1.5")
    host.wait_for_function("document.getElementById('player-status').textContent.includes('part 2')", timeout=30000)
    assert video.count() == 1
    # download is offered to the host and works
    with host.expect_download() as dl:
        host.click("#part-list li >> text=Download")
    assert dl.value.path() and dl.value.suggested_filename.endswith((".mp4", ".webm"))
    # the dashboard storage meter counts it
    host.goto(server + "/")
    assert "0 B of" not in host.content()
    assert mid


def test_invited_staff_can_play_but_not_download_or_delete(server, new_page):
    host, mid = _start_meeting_page(server, new_page, "Shared recording")
    # invite staff, then record
    host.goto(server + f"/meetings/{mid}")
    host.check("text=Sam Staff")
    host.click("button:has-text('Invite selected')")
    host.goto(server + f"/dev/recorder-test?meeting={mid}&part=6&slice=1000")
    _record(host, 8)
    staff = new_page()
    login(staff, server, STAFF)
    staff.goto(server + "/recordings")
    staff.click("text=Shared recording")
    staff.wait_for_selector("#part-list li")
    assert staff.locator("text=Download").count() == 0 and staff.locator("button:has-text('Delete')").count() == 0
    rid = re.search(r"/recordings/([0-9a-f-]{36})", staff.url).group(1)
    assert staff.request.get(server + f"/recordings/{rid}/parts/1/download", max_redirects=0).status == 403


def test_refresh_mid_recording_offers_recovery_ac07(server, new_page):
    host, mid = _start_meeting_page(server, new_page, "Crash recovery", "part=6&slice=1000")
    host.check("#consent")
    host.click("#btn-start")
    host.wait_for_function("document.getElementById('st-line').textContent.startsWith('Recording')")
    time.sleep(9)  # part 1 uploaded, part 2 still being recorded
    # simulate the browser dying: navigate away without stopping
    host.goto(server + f"/dev/recorder-test?meeting={mid}&part=6&slice=1000")
    host.wait_for_selector("#recover:not(.d-none)", timeout=15000)
    assert "unfinished recording" in host.inner_text("#recover-text")
    host.click("#btn-recover")
    host.wait_for_function("document.getElementById('st-line').textContent.includes('Recovered')", timeout=60000)
    host.goto(server + "/recordings")
    host.click("text=Crash recovery")
    host.wait_for_selector("#part-list li")
    assert host.locator("#part-list li").count() >= 2
    host.wait_for_function("document.getElementById('player').readyState >= 1")


def test_offline_during_recording_uploads_after_reconnect_ac06(server, new_page):
    host, mid = _start_meeting_page(server, new_page, "Offline test", "part=5&slice=1000")
    host.check("#consent")
    host.click("#btn-start")
    host.wait_for_function("document.getElementById('st-line').textContent.startsWith('Recording')")
    time.sleep(2)
    host.context.set_offline(True)
    time.sleep(12)  # at least two parts are produced while offline
    assert host.evaluate("navigator.onLine") is False
    host.context.set_offline(False)
    time.sleep(1)
    host.click("#btn-stop")
    host.wait_for_function("document.getElementById('st-line').textContent.includes('done')", timeout=90000)
    host.goto(server + "/recordings")
    host.click("text=Offline test")
    host.wait_for_selector("#part-list li")
    assert host.locator("#part-list li").count() >= 3
    assert host.locator(".alert-warning").count() == 0, host.inner_text("main")  # nothing missing
