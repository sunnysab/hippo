"""Operator entry point: schema init and the one-off account commands.

The two long-running processes are started from their own modules
(``python -m hippo.server`` and ``python -m hippo.sync_worker``); this module
holds the commands that run against PostgreSQL outside them.

``--help`` and argument errors never touch ``config.toml`` or the database:
argparse only dispatches, and the DSN is resolved inside the command itself.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from .config import get_settings
from .security import hash_password
from .storage import PostgresStorage, StorageInitError, close_pool, open_storage

#: Same floor as the admin API, so this command is not a way around it.
MIN_PASSWORD_LENGTH = 8


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m hippo.ops',
        description='Hippo operator commands: database init and account bootstrap.',
    )
    subcommands = parser.add_subparsers(dest='command', required=True)

    init_db = subcommands.add_parser(
        'init-db',
        help='Create or migrate the PostgreSQL schema (idempotent).',
    )
    init_db.add_argument('--dsn', help='PostgreSQL DSN (defaults to [database] dsn in config.toml)')
    init_db.set_defaults(handler=_run_init_db)

    create_admin = subcommands.add_parser(
        'create-admin',
        help='Create an administrator account (CLI-created admins are e-mail verified).',
    )
    create_admin.add_argument('--username', required=True, help='Login name')
    create_admin.add_argument('--password', help='Password; prompted for when omitted')
    create_admin.add_argument('--email', help='E-mail address')
    create_admin.add_argument('--timezone', default='Asia/Shanghai', help='IANA time zone')
    create_admin.set_defaults(handler=_run_create_admin)

    assign_owner = subcommands.add_parser(
        'assign-owner',
        help='Hand pre-multiuser groups and account sync settings to an existing user.',
    )
    assign_owner.add_argument('--owner', required=True, help='Username that takes over the orphan rows')
    assign_owner.set_defaults(handler=_run_assign_owner)
    return parser


def _resolve_dsn(dsn: str | None) -> str:
    resolved = dsn or get_settings().database.dsn
    if not resolved:
        raise StorageInitError('Missing PostgreSQL DSN. Set [database] dsn in config.toml or pass --dsn.')
    return resolved


async def init_db(*, dsn: str | None = None) -> None:
    """Create or migrate the schema; repeatable because it just replays schema.sql."""
    resolved_dsn = _resolve_dsn(dsn)
    try:
        async with PostgresStorage(resolved_dsn, auto_init=True):
            pass
    finally:
        await close_pool()
    print('PostgreSQL schema initialized.')


async def create_admin(
    *,
    username: str,
    password: str,
    email: str | None = None,
    timezone: str = 'Asia/Shanghai',
) -> int:
    """Create an admin account moved straight to e-mail verified.

    Whoever runs this already holds the database, so there is no verification
    loop to go through.
    """
    try:
        async with open_storage() as storage:
            if await storage.users.get_with_password(username) is not None:
                print(f'User already exists: {username}', file=sys.stderr)
                return 1
            async with storage.transaction():
                user = await storage.users.create(
                    username=username,
                    password_hash=await asyncio.to_thread(hash_password, password),
                    email=email,
                    role='admin',
                    email_verified=True,
                    timezone=timezone,
                )
        print(f'Created user #{user.id} {user.username} ({user.role})')
        return 0
    finally:
        await close_pool()


async def assign_owner(*, owner: str) -> int:
    """Give the rows left without an owner by the multiuser change to ``owner``.

    Repeatable: rows that already have an owner are left alone.
    """
    try:
        async with open_storage() as storage:
            credentials = await storage.users.get_with_password(owner)
            if credentials is None:
                print(
                    f'User not found: {owner}. Create it first with `python -m hippo.ops create-admin`.',
                    file=sys.stderr,
                )
                return 1
            user, _ = credentials
            async with storage.transaction():
                groups = await storage.subscriptions.claim_orphan_groups(user.id)
                subscriptions = await storage.subscriptions.backfill_from_accounts(user.id)
        print(f'Assigned {groups} group(s) and created {subscriptions} subscription(s) for {owner}.')
        return 0
    finally:
        await close_pool()


def _prompt_password() -> str:
    """Read the password twice without echoing it."""
    first = getpass.getpass('Password: ')
    second = getpass.getpass('Repeat password: ')
    if first != second:
        raise ValueError('Passwords do not match.')
    return first


def _run_init_db(args: argparse.Namespace) -> int:
    asyncio.run(init_db(dsn=args.dsn))
    return 0


def _run_create_admin(args: argparse.Namespace) -> int:
    username = args.username.strip()
    if not username:
        print('Username cannot be empty.', file=sys.stderr)
        return 2
    password = args.password
    if password is None:
        try:
            password = _prompt_password()
        except (EOFError, ValueError) as exc:
            print(str(exc) or 'Password input aborted.', file=sys.stderr)
            return 2
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', file=sys.stderr)
        return 2
    return asyncio.run(
        create_admin(
            username=username,
            password=password,
            email=args.email,
            timezone=args.timezone,
        )
    )


def _run_assign_owner(args: argparse.Namespace) -> int:
    owner = args.owner.strip()
    if not owner:
        print('Owner cannot be empty.', file=sys.stderr)
        return 2
    return asyncio.run(assign_owner(owner=owner))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except StorageInitError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
