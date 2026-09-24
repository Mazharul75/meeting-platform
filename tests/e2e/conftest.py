"""End-to-end fixtures: a real server (SQLite + local storage) and a real Chromium-family browser
with a fake camera and microphone. Uses Microsoft Edge/Chrome already on the machine, or the
Playwright Chromium if installed."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ("admin@e2e.test", "admin-password-123")
HOST = ("host@e2e.test", "host-password-123")
STAFF = ("staff@e2e.test", "staff-password-123")

SEED = """
import asyncio
from app.db import get_engine, get_sessionmaker
from app.models import User
from app.security.passwords import hash_password
async def main():
    async with get_sessionmaker()() as s:
        for email, name, role, pw in [
            ("admin@e2e.test", "Ada Admin", "admin", "admin-password-123"),
            ("host@e2e.test", "Hana Host", "member", "host-password-123"),
            ("staff@e2e.test", "Sam Staff", "member", "staff-password-123"),
        ]:
            s.add(User(email=email, display_name=name, password_hash=hash_password(pw), role=role, timezone="Asia/Dhaka"))
        await s.commit()
    await get_engine().dispose()
asyncio.run(main())
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    env = {
        **os.environ,
        "APP_ENV": "dev",
        "SECRET_KEY": "e2e-secret-" + "k" * 50,
        "MASTER_KEY": "e2e-master-" + "m" * 50,
        "DATABASE_URL": f"sqlite+aiosqlite:///{(work / 'e2e.db').as_posix()}",
        "APP_BASE_URL": f"http://127.0.0.1:{port}",
        "STORAGE_BACKEND": "local",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_KEY": "",
        "PART_SECONDS": "6",
        "LOGIN_RATE_PER_MINUTE": "1000",
        "TIMESLICE_MS": "1000",
        "PYTHONPATH": str(ROOT),
    }
    py = sys.executable
    # alembic.ini lives in the project root
    subprocess.run([py, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"], cwd=ROOT, env=env,
                   check=True, capture_output=True)
    subprocess.run([py, "-c", SEED], cwd=ROOT, env=env, check=True, capture_output=True)
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def browser():
    with playwright_sync.sync_playwright() as pw:
        args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]
        launch = None
        for channel in ("msedge", "chrome", None):
            try:
                if channel is None and not shutil.which("chromium"):
                    launch = pw.chromium.launch(args=args)
                else:
                    launch = pw.chromium.launch(channel=channel, args=args) if channel else pw.chromium.launch(args=args)
                break
            except Exception:  # noqa: BLE001, S112
                continue
        if launch is None:
            pytest.skip("no Chromium-based browser available")
        yield launch
        launch.close()


@pytest.fixture
def new_page(browser, server):
    contexts = []

    def make(permissions=True, viewport=None, bypass_csp=True):
        ctx = browser.new_context(
            permissions=["camera", "microphone"] if permissions else [],
            viewport=viewport or {"width": 1280, "height": 800},
            accept_downloads=True,
            bypass_csp=bypass_csp,  # Playwright's own polling uses eval; CSP is checked in a dedicated test
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(20000)
        return page

    yield make
    for c in contexts:
        c.close()


def login(page, base, creds):
    page.goto(base + "/login")
    page.fill("#email", creds[0])
    page.fill("#password", creds[1])
    page.click("button[type=submit]")
    page.wait_for_url(base + "/")
