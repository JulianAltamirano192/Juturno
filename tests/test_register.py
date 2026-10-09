import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from app.models import Tenant
from app.password import hash_password, verify_password


@pytest.mark.asyncio
async def test_get_register_page(client: AsyncClient):
    """GET /register debe responder 200 y renderizar el formulario con token CSRF."""
    response = await client.get("/register")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert 'name="csrf_token"' in html
    assert 'name="name"' in html
    assert 'name="owner_email"' in html
    assert 'name="password"' in html
    assert "Registrá tu negocio" in html
    assert CSRF_COOKIE_NAME in response.cookies


@pytest.mark.asyncio
async def test_post_register_success(client: AsyncClient, db_session: AsyncSession):
    """Registro exitoso de un nuevo negocio."""
    csrf_token = generate_csrf_token()
    payload = {
        "name": "Estudio Central",
        "owner_email": "DuenO@Estudio.COM",
        "password": "supersecretpassword123",
        "whatsapp_number": "1155555555",
        "slug": "estudio-central",
        "csrf_token": csrf_token,
    }

    response = await client.post(
        "/register",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?registered=1"

    # Verificar en base de datos
    result = await db_session.execute(
        select(Tenant).where(Tenant.owner_email == "dueno@estudio.com")
    )
    tenant = result.scalar_one_or_none()
    assert tenant is not None
    assert tenant.name == "Estudio Central"
    assert tenant.slug == "estudio-central"
    assert tenant.owner_email == "dueno@estudio.com"
    assert tenant.whatsapp_number == "1155555555"
    assert tenant.session_version == 1
    # Slots, agenda and WhatsApp texts use this zone; UTC shifted them 3 hours.
    assert tenant.timezone == "America/Argentina/Buenos_Aires"
    assert tenant.password_hash is not None
    assert tenant.password_hash.startswith("pbkdf2_sha256$600000$")
    assert verify_password("supersecretpassword123", tenant.password_hash) is True
    assert verify_password("wrongpassword", tenant.password_hash) is False


@pytest.mark.asyncio
async def test_post_register_duplicate_email_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """No permite registrar dos negocios con el mismo email de dueño."""
    first_tenant = Tenant(
        name="Negocio 1",
        slug="negocio-1",
        owner_email="contacto@negocio.com",
        password_hash=hash_password("password1234"),
    )
    db_session.add(first_tenant)
    await db_session.commit()

    csrf_token = generate_csrf_token()
    payload = {
        "name": "Negocio 2",
        "owner_email": "  CONTACTO@NEGOCIO.COM  ",
        "password": "otrapassword123",
        "csrf_token": csrf_token,
    }

    response = await client.post(
        "/register",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert (
        "Ya existe un negocio registrado con este correo electrónico" in response.text
    )


@pytest.mark.asyncio
async def test_post_register_slug_collision_handled(
    client: AsyncClient, db_session: AsyncSession
):
    """Manejo automático de colisión de slugs (-2, -3, ...)."""
    first_tenant = Tenant(
        name="Servicios Express",
        slug="servicios-express",
        owner_email="primero@express.com",
        password_hash=hash_password("password1234"),
    )
    db_session.add(first_tenant)
    await db_session.commit()

    csrf_token = generate_csrf_token()
    payload = {
        "name": "Servicios Express",
        "owner_email": "segundo@express.com",
        "password": "password12345",
        "csrf_token": csrf_token,
    }

    response = await client.post(
        "/register",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = await db_session.execute(
        select(Tenant).where(Tenant.owner_email == "segundo@express.com")
    )
    second_tenant = result.scalar_one_or_none()
    assert second_tenant is not None
    assert second_tenant.slug == "servicios-express-2"


@pytest.mark.asyncio
async def test_post_register_short_password_rejected(client: AsyncClient):
    """Contraseña menor a 8 caracteres es rechazada con mensaje claro."""
    csrf_token = generate_csrf_token()
    payload = {
        "name": "Negocio Corto",
        "owner_email": "corto@negocio.com",
        "password": "12345",
        "csrf_token": csrf_token,
    }

    response = await client.post(
        "/register",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "La contraseña debe tener al menos 8 caracteres" in response.text


@pytest.mark.asyncio
async def test_post_register_invalid_csrf_rejected(client: AsyncClient):
    """Falta de CSRF cookie o token que no coincide rechaza la petición con 400."""
    payload = {
        "name": "Negocio CSRF",
        "owner_email": "csrf@negocio.com",
        "password": "password12345",
        "csrf_token": "token_formulario",
    }

    response = await client.post(
        "/register",
        data=payload,
        cookies={CSRF_COOKIE_NAME: "otro_token_distinto"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "El formulario expiró o es inválido" in response.text


def test_password_hash_and_verify_unit():
    """Prueba unitaria de PBKDF2-HMAC-SHA256 con 600.000 iteraciones."""
    pwd = "MiContraseñaSegura2026!"
    h = hash_password(pwd)
    assert h.startswith("pbkdf2_sha256$600000$")
    assert verify_password(pwd, h) is True
    assert verify_password("otra", h) is False
    assert verify_password("", h) is False
    assert verify_password(pwd, "") is False
    assert verify_password(pwd, "invalid$format") is False
