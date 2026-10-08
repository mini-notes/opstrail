"""Operator CLI.

    python -m app.cli create-user --username admin --role admin
    python -m app.cli set-password --username admin
    python -m app.cli list-users

The password is always read interactively via getpass: passing it as an
argument would place it in shell history and in the process list.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User, UserRole
from app.security import hash_password

MIN_PASSWORD_LEN = 8


def _prompt_password() -> str:
    first = getpass.getpass("Password: ")
    if len(first) < MIN_PASSWORD_LEN:
        sys.exit(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("passwords do not match")
    return first


async def create_user(username: str, role: str, email: str | None, upn: str | None) -> None:
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing:
            sys.exit(f"user {username!r} already exists")

        password = _prompt_password()
        session.add(
            User(
                username=username,
                email=email,
                upn=upn,
                password_hash=hash_password(password),
                role=UserRole(role),
                is_active=True,
            )
        )
        await session.commit()
    print(f"created user {username!r} with role {role}")


async def set_password(username: str) -> None:
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            sys.exit(f"no such user: {username!r}")
        user.password_hash = hash_password(_prompt_password())
        await session.commit()
    print(f"password updated for {username!r}")


async def list_users() -> None:
    async with SessionLocal() as session:
        users = (
            (await session.execute(select(User).order_by(User.username))).scalars().all()
        )
    if not users:
        print("no users yet -- create one with: python -m app.cli create-user ...")
        return
    print(f"{'username':<24}{'role':<10}{'active':<8}{'email'}")
    for u in users:
        print(f"{u.username:<24}{u.role.value:<10}{str(u.is_active):<8}{u.email or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="OpsTrail admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user (password prompted)")
    create.add_argument("--username", required=True)
    create.add_argument(
        "--role", default="viewer", choices=[r.value for r in UserRole]
    )
    create.add_argument("--email")
    create.add_argument("--upn", help="Entra ID UPN; used by Phase 2 Teams mentions")

    pwd = sub.add_parser("set-password", help="change a user's password")
    pwd.add_argument("--username", required=True)

    sub.add_parser("list-users", help="list users")

    args = parser.parse_args()
    if args.command == "create-user":
        asyncio.run(create_user(args.username, args.role, args.email, args.upn))
    elif args.command == "set-password":
        asyncio.run(set_password(args.username))
    else:
        asyncio.run(list_users())


if __name__ == "__main__":
    main()
