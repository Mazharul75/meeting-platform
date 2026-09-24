"""Shared fixtures: in-memory SQLite database, fake storage, fake clock, HTTP client."""
from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

os.environ.update(
    APP_ENV="test",
    SECRET_KEY="test-secret-key-" + "x" * 48,
    MASTER_KEY="test-master-key-" + "y" * 48,
    DATABASE_URL="sqlite+aiosqlite://",
    STORAGE_BACKEND="memory",
    APP_BASE_URL="http://testserver",
)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import clock, db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base, Meeting, MeetingMember, User  # noqa: E402
from app.security import ratelimit  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402
from app.services.storage import MemoryStorage, get_storage  # noqa: E402

PASSWORD = "correct horse battery"
_CSRF_RE = re.compile(r'name="csrf-token" content="([0-9a-f]+)"')


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db.set_engine_for_tests(eng)
    yield eng
    db.set_engine_for_tests(None)
    await eng.dispose()


@pytest.fixture(autouse=True)
def _reset_state():
    ratelimit.reset_all()
    ratelimit.set_clock(None)
    clock.set_clock(None)
    yield
    clock.set_clock(None)
    ratelimit.set_clock(None)


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def app(engine, storage):
    application = create_app()
    application.dependency_overrides[get_storage] = lambda: storage
    return application


@pytest.fixture
def make_client(app) -> Callable[[], httpx.AsyncClient]:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    return factory


@pytest_asyncio.fixture
async def client(make_client) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture
def make_user(session):
    async def _make(email: str, role: str = "member", name: str | None = None, password: str = PASSWORD) -> User:
        user = User(
            email=email,
            display_name=name or email.split("@")[0].title(),
            password_hash=hash_password(password),
            role=role,
            timezone="Asia/Dhaka",
        )
        session.add(user)
        await session.commit()
        return user

    return _make


@pytest.fixture
def make_meeting(session):
    async def _make(host: User, invited: list[User] | None = None, status: str = "scheduled", **kw) -> Meeting:
        start = kw.pop("start", datetime.now(UTC) + timedelta(days=1))
        meeting = Meeting(
            title=kw.pop("title", "Weekly sync"),
            description="",
            mode="online",
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
            timezone="Asia/Dhaka",
            host_id=host.id,
            status=status,
        )
        meeting.members.append(MeetingMember(user_id=host.id, role="host"))
        for person in invited or []:
            meeting.members.append(MeetingMember(user_id=person.id, role="member"))
        session.add(meeting)
        await session.commit()
        return meeting

    return _make


async def csrf_of(client: httpx.AsyncClient, path: str = "/login") -> str:
    resp = await client.get(path, follow_redirects=True)
    match = _CSRF_RE.search(resp.text)
    assert match, f"no csrf token on {path}: {resp.status_code}"
    return match.group(1)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    token = await csrf_of(client, "/login")
    return await client.post("/login", data={"email": email, "password": password, "csrf_token": token})


@pytest_asyncio.fixture
async def login_as(make_client):
    """Return a factory for logged-in clients (each call gets its own cookie jar)."""
    opened: list[httpx.AsyncClient] = []

    async def _login(user: User, password: str = PASSWORD) -> httpx.AsyncClient:
        c = make_client()
        opened.append(c)
        resp = await login(c, user.email, password)
        assert resp.status_code == 303, resp.text
        c.csrf = await csrf_of(c, "/")  # type: ignore[attr-defined]
        return c

    yield _login
    for c in opened:
        await c.aclose()
