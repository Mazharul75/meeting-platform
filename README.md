# Company Meetings

A private web platform for company meetings: staff schedule online meetings, guests join with an
expiring link, and recordings are saved in parts straight from the browser into a private bucket and
played back inside the platform.

**Stack:** Python 3.12, FastAPI, Jinja2 + HTMX + Bootstrap 5.3 (all served from this server, no CDNs),
SQLAlchemy (async) + Alembic, Supabase Postgres and private Storage, LiveKit (online video room, next build).

## What is included

- Invite-only accounts (admin creates staff), Argon2id passwords, lockout, server-side sessions, CSRF
- Meetings: schedule, invite staff, cancel, calendar (.ics) download, guest links (hashed, expiring, revocable)
- Device-test lobby (camera and microphone check)
- Recording engine: 4-minute parts, crash-safe local copy, automatic retrying upload, recovery after refresh
- Recordings library, playback across parts, host/admin download, delete, audit log, storage meter

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env             # then edit .env (see below)
```

For a quick local run without Supabase put this in `.env`:

```
APP_ENV=dev
SECRET_KEY=<64 random characters>
DATABASE_URL=sqlite+aiosqlite:///./dev.db
```

```bash
python -m alembic upgrade head
python -m scripts.create_admin
python -m uvicorn app.main:app --reload
```

Open http://localhost:8000. Recordings are stored in `.dev_storage/` when Supabase is not configured.
The recorder test page is at `/dev/recorder-test?meeting=<meeting id>`.

## Tests

```bash
ruff check .
mypy app scripts
bandit -q -r app scripts -c pyproject.toml
pip-audit -r requirements.txt
pytest tests/unit tests/integration          # backend
node --test --test-force-exit tests/js/recorder.test.mjs   # recorder engine
pytest tests/e2e                              # real browser (needs Edge, Chrome, or: playwright install chromium)
```

## Configuration

All settings come from environment variables (see `.env.example`). Never commit real values.
In production the app refuses to start when `SECRET_KEY`, `MASTER_KEY`, `DATABASE_URL`, the Supabase
settings or an `https://` `APP_BASE_URL` are missing.

## Deployment

See the deployment steps shared with the project owner; the Render blueprint is `render.yaml`.
