"""
Генерация API-ключа для тенанта (platform.api_keys).

Ключ выводится ОДИН РАЗ — в БД хранится только SHA-256 хэш.
Запускать внутри контейнера api:

  # Ключ внешней медсистемы (дефолтные скоупы claims:*):
  docker compose exec api python -m scripts.create_api_key \\
      --tenant-slug default --name "Medsystem production"

  # Ключ оператора ручной проверки:
  docker compose exec api python -m scripts.create_api_key \\
      --tenant-slug default --name "Operator Ivanov" \\
      --scopes reviews:read reviews:write claims:read analytics:read

  # Отзыв ключа — вручную:
  UPDATE platform.api_keys SET revoked_at = NOW() WHERE name = '...';
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.auth import ALL_SCOPES, hash_api_key
from core.database import AsyncSessionLocal
from core.models.platform import ApiKey, Tenant

VALID_SCOPES = set(ALL_SCOPES) | {"admin"}


async def create_key(args: argparse.Namespace) -> int:
    invalid = [s for s in args.scopes if s not in VALID_SCOPES]
    if invalid:
        print(f"Неизвестные скоупы: {invalid}. Допустимые: {sorted(VALID_SCOPES)}")
        return 1

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(
            select(Tenant).where(Tenant.slug == args.tenant_slug)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"Тенант со slug={args.tenant_slug!r} не найден.")
            return 1

        raw_key = f"icps_{args.environment}_{secrets.token_urlsafe(32)}"

        api_key = ApiKey(
            tenant_id=tenant.id,
            key_hash=hash_api_key(raw_key),
            name=args.name,
            environment=args.environment,
            scopes=args.scopes,
            rate_limit_rpm=args.rpm,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(days=args.expires_days)
                if args.expires_days else None
            ),
        )
        db.add(api_key)
        await db.commit()
        await db.refresh(api_key)

    print("API-ключ создан. Сохраните его сейчас — повторно показать невозможно:")
    print()
    print(f"  {raw_key}")
    print()
    print(f"  key_id:      {api_key.id}")
    print(f"  tenant:      {tenant.slug} ({tenant.id})")
    print(f"  name:        {args.name}")
    print(f"  environment: {args.environment}")
    print(f"  scopes:      {args.scopes}")
    print(f"  rate limit:  {args.rpm} req/min")
    print(f"  expires_at:  {api_key.expires_at or '—'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать API-ключ для тенанта")
    parser.add_argument("--tenant-slug", required=True, help="slug тенанта (platform.tenants)")
    parser.add_argument("--name", required=True, help="назначение ключа (кому выдан)")
    parser.add_argument(
        "--scopes", nargs="+", default=["claims:write", "claims:read"],
        help="скоупы (default: claims:write claims:read)",
    )
    parser.add_argument("--environment", default="production", choices=["production", "test"])
    parser.add_argument("--rpm", type=int, default=60, help="лимит запросов/мин (default: 60)")
    parser.add_argument("--expires-days", type=int, default=None,
                        help="срок жизни в днях (default: бессрочно)")
    args = parser.parse_args()
    return asyncio.run(create_key(args))


if __name__ == "__main__":
    sys.exit(main())
