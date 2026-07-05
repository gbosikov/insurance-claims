"""
Создание пользователя веб-портала (platform.users).

Запускать внутри контейнера api:

  docker compose exec api python -m scripts.create_user \\
      --tenant-slug default --email admin@example.com --full-name "Admin User" --role admin

Роли: viewer (только чтение) | operator (ручная проверка) | admin (полный доступ)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models.platform import Tenant
from core.models.user import PortalUser
from core.portal_auth import hash_password

VALID_ROLES = ("viewer", "operator", "admin")


async def create_user(args: argparse.Namespace) -> int:
    if args.role not in VALID_ROLES:
        print(f"Неверная роль: {args.role!r}. Допустимые: {VALID_ROLES}")
        return 1

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(
            select(Tenant).where(Tenant.slug == args.tenant_slug)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"Тенант со slug={args.tenant_slug!r} не найден.")
            return 1

        email = args.email.strip().lower()

        existing = (await db.execute(
            select(PortalUser).where(PortalUser.email == email)
        )).scalar_one_or_none()
        if existing is not None:
            print(f"Пользователь с email {email!r} уже существует (id={existing.id}).")
            return 1

        import getpass
        if args.password:
            password = args.password
        else:
            password = getpass.getpass("Пароль: ")
            confirm = getpass.getpass("Подтвердите пароль: ")
            if password != confirm:
                print("Пароли не совпадают.")
                return 1

        if len(password) < 8:
            print("Пароль должен содержать не менее 8 символов.")
            return 1

        user = PortalUser(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(password),
            full_name=args.full_name,
            role=args.role,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    print("Пользователь создан:")
    print(f"  id:        {user.id}")
    print(f"  email:     {user.email}")
    print(f"  full_name: {user.full_name or '—'}")
    print(f"  role:      {user.role}")
    print(f"  tenant:    {tenant.slug} ({tenant.id})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать пользователя веб-портала")
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", default=None)
    parser.add_argument("--role", default="viewer", choices=VALID_ROLES)
    parser.add_argument("--password", default=None,
                        help="Пароль (если не задан — запросит интерактивно)")
    args = parser.parse_args()
    return asyncio.run(create_user(args))


if __name__ == "__main__":
    sys.exit(main())
