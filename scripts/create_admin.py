"""Create the first administrator. The password is typed at a hidden prompt (not in shell history).

Usage (from the project folder, with your .env in place and migrations applied):
    python -m scripts.create_admin
"""
from __future__ import annotations

import asyncio
import getpass
import re
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import User
from app.security.passwords import hash_password, validate_new_password
from app.services import audit
from app.services.meetings import valid_timezone

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ask() -> tuple[str, str, str, str] | None:
    settings = get_settings()
    email = input("Admin email: ").strip().lower()
    if not _EMAIL_RE.match(email):
        print("That does not look like an email address.")
        return None
    name = input("Display name: ").strip()
    if not name:
        print("Name is required.")
        return None
    timezone = input(f"Time zone [{settings.default_timezone}]: ").strip() or settings.default_timezone
    if not valid_timezone(timezone):
        print("Unknown time zone.")
        return None
    password = getpass.getpass("Password (min 12 characters): ")
    problem = validate_new_password(password)
    if problem:
        print(problem)
        return None
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords do not match.")
        return None
    return email, name, timezone, password


async def create(email: str, name: str, timezone: str, password: str) -> int:
    async with get_sessionmaker()() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            print("An account with this email already exists.")
            return 1
        user = User(
            email=email,
            display_name=name,
            password_hash=hash_password(password),
            role="admin",
            timezone=timezone,
        )
        db.add(user)
        await db.flush()
        await audit.log(db, "user.create", target_type="user", target_id=user.id, details={"role": "admin", "via": "cli"})
        await db.commit()
    print("Administrator created. You can now log in.")
    return 0


if __name__ == "__main__":
    answers = ask()
    sys.exit(1 if answers is None else asyncio.run(create(*answers)))
