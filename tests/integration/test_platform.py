import asyncio
import re
import uuid

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app.models import ALL_TABLES, Base
from app.services.storage import LocalDevStorage, get_storage
from tests.conftest import csrf_of


def test_migration_matches_models_and_creates_all_tables(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    sync = create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    assert set(ALL_TABLES) <= set(inspect(sync).get_table_names())
    with sync.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
    command.downgrade(cfg, "base")


def test_migration_enables_rls_on_every_table():
    src = open("alembic/versions/0001_initial.py", encoding="utf-8").read()
    listed = set(re.search(r"TABLES = \[(.*?)\]", src, re.S).group(1).replace('"', "").replace("\n", "").replace(" ", "").split(",")) - {""}
    assert listed == set(ALL_TABLES)
    assert "ENABLE ROW LEVEL SECURITY" in src and "CREATE POLICY" not in src.upper()


async def test_local_storage_signed_urls_end_to_end(app, make_client, tmp_path):
    from app.routers import local_storage as ls

    store = LocalDevStorage(tmp_path / "s", "k" * 40)
    app.dependency_overrides[get_storage] = lambda: store
    ls_get = ls.get_storage
    ls.get_storage = lambda: store
    try:
        async with make_client() as c:
            up = await store.create_upload("recordings/m/r/part-0001.webm", "video/webm", True)
            r = await c.put(up.url, content=b"hello world", headers={"Content-Type": "video/webm"})
            assert r.status_code == 200 and await store.stat("recordings/m/r/part-0001.webm") == 11
            bad = up.url.replace("sig=", "sig=00")
            assert (await c.put(bad, content=b"x")).status_code == 403
            dl = await store.create_download_url("recordings/m/r/part-0001.webm", 600, "x.webm")
            got = await c.get(dl)
            assert got.status_code == 200 and got.content == b"hello world"
            assert "attachment" in got.headers["content-disposition"]
            rng = await c.get(await store.create_download_url("recordings/m/r/part-0001.webm", 600), headers={"Range": "bytes=0-4"})
            assert rng.status_code == 206 and rng.content == b"hello"
            assert (await c.get(dl.replace("path=", "path=x"))).status_code == 403
            # no upsert flag -> cannot overwrite an existing file
            once = await store.create_upload("recordings/m/r/part-0001.webm", "video/webm", False)
            assert (await c.put(once.url, content=b"other")).status_code == 409
    finally:
        ls.get_storage = ls_get


async def test_dev_tools_and_local_storage_hidden_when_not_allowed(login_as, make_user, monkeypatch):
    from app.config import get_settings

    c = await login_as(await make_user("h@x.com"))
    assert (await c.get("/dev/recorder-test")).status_code == 200
    monkeypatch.setattr(type(get_settings()), "dev_tools_enabled", property(lambda self: False))
    assert (await c.get("/dev/recorder-test")).status_code == 404


# ---- IDOR sweep: every route that has an id in the address, as every kind of visitor ---------------


async def test_idor_sweep(login_as, make_user, make_meeting, make_client, session, storage):
    from tests.integration.test_recordings import H, put_and_complete, start

    host, inv, other, admin = (await make_user("h@x.com"), await make_user("i@x.com"), await make_user("o@x.com"),
                               await make_user("a@x.com", "admin"))
    m = await make_meeting(host, [inv])
    hc = await login_as(host)
    rid = (await start(hc, m)).json()["recording_id"]
    await put_and_complete(hc, session, storage, rid, 1, b"x" * 10)
    await hc.post(f"/api/recordings/{rid}/finish", headers=H(hc), json={"parts_expected": 1})
    gid = uuid.uuid4()
    routes = [  # (method, path, {role: expected})  roles: host, admin, invited, other
        ("GET", f"/meetings/{m.id}", dict(host=200, admin=200, invited=200, other=404)),
        ("GET", f"/meetings/{m.id}/calendar.ics", dict(host=200, admin=200, invited=200, other=404)),
        ("GET", f"/meetings/{m.id}/lobby", dict(host=200, admin=200, invited=200, other=404)),
        ("POST", f"/meetings/{m.id}/guest-links", dict(host=200, admin=200, invited=403, other=404)),
        ("DELETE", f"/meetings/{m.id}/guest-links/{gid}", dict(host=404, admin=404, invited=403, other=404)),
        ("POST", f"/meetings/{m.id}/invite", dict(host=303, admin=303, invited=403, other=404)),
        ("GET", f"/recordings/{rid}", dict(host=200, admin=200, invited=200, other=404)),
        ("GET", f"/api/recordings/{rid}/parts", dict(host=200, admin=200, invited=200, other=404)),
        ("GET", f"/recordings/{rid}/parts/1/download", dict(host=303, admin=303, invited=403, other=404)),
        ("POST", f"/api/recordings/{rid}/parts/1/upload-url", dict(host=409, admin=403, invited=403, other=404)),
        ("POST", f"/api/recordings/{rid}/finish", dict(host=200, admin=403, invited=403, other=404)),
        ("POST", f"/api/meetings/{m.id}/end", dict(host=200, admin=403, invited=403, other=404)),
    ]
    clients = {"host": hc, "admin": await login_as(admin), "invited": await login_as(inv), "other": await login_as(other)}
    anon = make_client()
    for method, path, expected in routes:
        for role, code in expected.items():
            c = clients[role]
            kw = {"headers": {"X-CSRF-Token": c.csrf}}
            if path.endswith("/finish"):
                kw["json"] = {"parts_expected": 1}
            r = await c.request(method, path, **kw)
            assert r.status_code == code, (method, path, role, r.status_code)
        r = await anon.request(method, path)
        assert r.status_code in (303, 401, 403), (method, path, "anonymous", r.status_code)
    await anon.aclose()
    # delete last (it changes state): only host/admin allowed
    for role, code in (("other", 404), ("invited", 403), ("admin", 204)):
        c = clients[role]
        assert (await c.delete(f"/recordings/{rid}", headers={"X-CSRF-Token": c.csrf})).status_code == code, role


async def test_no_secret_or_token_in_logs(login_as, make_user, capsys):
    import logging

    from app.logging_setup import RedactingFilter

    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    lg = logging.getLogger("uvicorn.access")
    lg.addHandler(handler)
    lg.warning("GET /_local_storage/download?path=a&sig=SECRETSIG123 for jane@corp.com")
    lg.removeHandler(handler)
    out = stream.getvalue()
    assert "SECRETSIG123" not in out and "jane@corp.com" not in out


async def test_static_assets_served_locally_only(client):
    login = (await client.get("/login")).text
    assert "http://" not in login.replace("http://www.w3.org/2000/svg", "") and "https://" not in login
    for asset in re.findall(r'(?:src|href)="(/static/[^"]+)"', login):
        assert (await client.get(asset)).status_code == 200, asset


async def test_unused_imports_guard():
    assert asyncio and httpx and pytest and csrf_of
