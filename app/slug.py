"""
Normalización y generación de slugs únicos para negocios (tenants).
"""

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant


def slugify(text: str) -> str:
    """
    Normaliza una cadena de texto para usarla como slug URL seguro.
    Ejemplo: 'Corte & Estilo!' -> 'corte-estilo'
    """
    if not text:
        return "negocio"
    # Normalizar acentos y diacríticos (NFKD)
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    # Minúsculas y reemplazar caracteres no alfanuméricos por guiones
    s = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return s if s else "negocio"


async def generate_unique_slug(
    session: AsyncSession,
    base_text: str,
    exclude_tenant_id: int | None = None,
) -> str:
    """
    Genera un slug único para un Tenant. Si ya existe, añade un sufijo numérico (-2, -3, ...).
    """
    base_slug = slugify(base_text)
    candidate = base_slug
    suffix = 2

    while True:
        query = select(Tenant).where(Tenant.slug == candidate)
        if exclude_tenant_id is not None:
            query = query.where(Tenant.id != exclude_tenant_id)
        result = await session.execute(query)
        existing = result.scalar_one_or_none()

        if existing is None:
            return candidate

        candidate = f"{base_slug}-{suffix}"
        suffix += 1
