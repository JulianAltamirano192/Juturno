"""
CLI de administración de API keys.

Uso:
    python -m app.cli create-api-key --tenant-id 1 --label "default"
    python -m app.cli list-api-keys --tenant-id 1
    python -m app.cli revoke-api-key --api-key-id 3
"""
import argparse
import asyncio
import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from app.auth import hash_api_key
from app.database import async_session_maker
from app.models import ApiKey, Tenant


async def create_api_key(tenant_id: int, label: str | None) -> None:
    async with async_session_maker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise SystemExit(f"No existe ningún tenant con id={tenant_id}")

        raw_key = f"tf_{secrets.token_urlsafe(32)}"
        api_key = ApiKey(
            tenant_id=tenant_id,
            key_hash=hash_api_key(raw_key),
            label=label,
        )
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)

    print(f"API key creada (id={api_key.id}) para tenant_id={tenant_id}.")
    print("Guardala ahora: no se puede volver a mostrar.")
    print()
    print(raw_key)


async def list_api_keys(tenant_id: int) -> None:
    async with async_session_maker() as session:
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        keys = (await session.execute(stmt)).scalars().all()

    if not keys:
        print(f"El tenant_id={tenant_id} no tiene API keys.")
        return

    for key in keys:
        estado = "revocada" if key.revoked_at else "activa"
        print(
            f"id={key.id}  label={key.label or '-'}  estado={estado}  "
            f"creada={key.created_at}  ultimo_uso={key.last_used_at or 'nunca'}"
        )


async def revoke_api_key(api_key_id: int) -> None:
    async with async_session_maker() as session:
        api_key = await session.get(ApiKey, api_key_id)
        if api_key is None:
            raise SystemExit(f"No existe ninguna api_key con id={api_key_id}")
        if api_key.revoked_at is not None:
            print(f"La api_key {api_key_id} ya estaba revocada.")
            return

        api_key.revoked_at = datetime.now(timezone.utc)
        session.add(api_key)
        await session.commit()

    print(f"API key {api_key_id} revocada.")
    print("Nota: puede seguir aceptándose hasta 60s si estaba cacheada.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-api-key")
    create_parser.add_argument("--tenant-id", type=int, required=True)
    create_parser.add_argument("--label", type=str, default=None)

    list_parser = subparsers.add_parser("list-api-keys")
    list_parser.add_argument("--tenant-id", type=int, required=True)

    revoke_parser = subparsers.add_parser("revoke-api-key")
    revoke_parser.add_argument("--api-key-id", type=int, required=True)

    args = parser.parse_args()

    if args.command == "create-api-key":
        asyncio.run(create_api_key(args.tenant_id, args.label))
    elif args.command == "list-api-keys":
        asyncio.run(list_api_keys(args.tenant_id))
    elif args.command == "revoke-api-key":
        asyncio.run(revoke_api_key(args.api_key_id))


if __name__ == "__main__":
    main()