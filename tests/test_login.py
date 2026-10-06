import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Tenant
from app.csrf import generate_csrf_token, CSRF_COOKIE_NAME
from app.password import hash_password
from app.session import (
    create_session_token,
    SESSION_COOKIE_NAME,
    sanitize_next_url,
)


@pytest.mark.asyncio
async def test_get_login_page(client: AsyncClient):
    """GET /login muestra el formulario y setea la cookie CSRF."""
    response = await client.get("/login?registered=1")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "Iniciar sesión" in html
    assert "Tu cuenta fue creada con éxito" in html
    assert 'name="csrf_token"' in html
    assert CSRF_COOKIE_NAME in response.cookies


@pytest.mark.asyncio
async def test_get_login_page_with_active_session_redirects(
    client: AsyncClient, db_session: AsyncSession
):
    """Si el usuario ya tiene una sesión válida, GET /login lo redirige al dashboard."""
    tenant = Tenant(
        name="Mi Negocio Activo",
        slug="negocio-activo",
        owner_email="activo@negocio.com",
        password_hash=hash_password("clave12345"),
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    token = create_session_token(tenant.id, tenant.session_version)
    response = await client.get(
        "/login",
        cookies={SESSION_COOKIE_NAME: token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_post_login_success(client: AsyncClient, db_session: AsyncSession):
    """Inicio de sesión exitoso setea la cookie de sesión firmada."""
    tenant = Tenant(
        name="Peluquería Studio",
        slug="peluqueria-studio",
        owner_email="studio@pelu.com",
        password_hash=hash_password("miclavesecreta123"),
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()

    csrf_token = generate_csrf_token()
    payload = {
        "owner_email": " STUDIO@PELU.COM ",
        "password": "miclavesecreta123",
        "csrf_token": csrf_token,
    }

    response = await client.post(
        "/login",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert SESSION_COOKIE_NAME in response.cookies


@pytest.mark.asyncio
async def test_post_login_sanitizes_next_parameter(
    client: AsyncClient, db_session: AsyncSession
):
    """Previene ataques de redirección abierta (open redirect / phishing)."""
    tenant = Tenant(
        name="Seguridad SRL",
        slug="seguridad-srl",
        owner_email="seguro@srl.com",
        password_hash=hash_password("password123"),
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()

    csrf_token = generate_csrf_token()

    # Intento 1: URL externa maliciosa
    payload_malicious = {
        "owner_email": "seguro@srl.com",
        "password": "password123",
        "next": "https://sitio-malicioso.com",
        "csrf_token": csrf_token,
    }
    resp1 = await client.post(
        "/login",
        data=payload_malicious,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert resp1.status_code == 303
    assert resp1.headers["location"] == "/dashboard"

    # Intento 2: URL protocolo-relativa
    payload_proto = {
        "owner_email": "seguro@srl.com",
        "password": "password123",
        "next": "//sitio-malicioso.com",
        "csrf_token": csrf_token,
    }
    resp2 = await client.post(
        "/login",
        data=payload_proto,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/dashboard"

    # Intento 3: URL interna válida
    payload_valid = {
        "owner_email": "seguro@srl.com",
        "password": "password123",
        "next": "/servicios",
        "csrf_token": csrf_token,
    }
    resp3 = await client.post(
        "/login",
        data=payload_valid,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert resp3.status_code == 303
    assert resp3.headers["location"] == "/servicios"


@pytest.mark.asyncio
async def test_post_login_wrong_password(client: AsyncClient, db_session: AsyncSession):
    """Contraseña incorrecta devuelve 400 y mensaje de error genérico."""
    tenant = Tenant(
        name="Test Password",
        slug="test-password",
        owner_email="user@test.com",
        password_hash=hash_password("correctpassword"),
    )
    db_session.add(tenant)
    await db_session.commit()

    csrf_token = generate_csrf_token()
    payload = {
        "owner_email": "user@test.com",
        "password": "wrongpassword",
        "csrf_token": csrf_token,
    }
    response = await client.post(
        "/login",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Correo electrónico o contraseña incorrectos" in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_post_login_unknown_email(client: AsyncClient):
    """Email no registrado devuelve el mismo mensaje genérico para evitar enumeración."""
    csrf_token = generate_csrf_token()
    payload = {
        "owner_email": "noexiste@test.com",
        "password": "cualquierpassword",
        "csrf_token": csrf_token,
    }
    response = await client.post(
        "/login",
        data=payload,
        cookies={CSRF_COOKIE_NAME: csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Correo electrónico o contraseña incorrectos" in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_protected_dashboard_requires_session(client: AsyncClient):
    """Acceder a /dashboard sin cookie redirige a /login?next=/dashboard."""
    response = await client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/dashboard"


@pytest.mark.asyncio
async def test_protected_dashboard_with_valid_session(
    client: AsyncClient, db_session: AsyncSession
):
    """Acceder a /dashboard con sesión válida devuelve 200 y el template."""
    tenant = Tenant(
        name="Consultorio Médico",
        slug="consultorio-medico",
        owner_email="dr@consultorio.com",
        password_hash=hash_password("doctor1234"),
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    token = create_session_token(tenant.id, tenant.session_version)
    response = await client.get(
        "/dashboard",
        cookies={SESSION_COOKIE_NAME: token},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Consultorio Médico" in response.text
    assert "consultorio-medico" in response.text


@pytest.mark.asyncio
async def test_session_version_invalidation(
    client: AsyncClient, db_session: AsyncSession
):
    """Al incrementar session_version en la base de datos, la cookie previa queda invalidada."""
    tenant = Tenant(
        name="Auto Lavadero",
        slug="auto-lavadero",
        owner_email="dueno@lavadero.com",
        password_hash=hash_password("lavadero123"),
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    # 1. Cookie emitida con version 1
    token_v1 = create_session_token(tenant.id, 1)

    # Verifica que con version 1 accede correctamente
    resp_ok = await client.get(
        "/dashboard",
        cookies={SESSION_COOKIE_NAME: token_v1},
        follow_redirects=False,
    )
    assert resp_ok.status_code == 200

    # 2. Se actualiza session_version a 2 (ej. cambio de clave o revocación manual)
    tenant.session_version = 2
    db_session.add(tenant)
    await db_session.commit()

    # 3. La cookie previa con version 1 ahora es rechazada
    resp_revoked = await client.get(
        "/dashboard",
        cookies={SESSION_COOKIE_NAME: token_v1},
        follow_redirects=False,
    )
    assert resp_revoked.status_code == 303
    assert "/login" in resp_revoked.headers["location"]


@pytest.mark.asyncio
async def test_deleted_tenant_clears_cookie_and_redirects(client: AsyncClient):
    """Cookie firmada válida pero para un tenant_id inexistente redirige y borra la cookie sin tirar 500."""
    non_existent_tenant_id = 99999
    token = create_session_token(non_existent_tenant_id, 1)

    response = await client.get(
        "/dashboard",
        cookies={SESSION_COOKIE_NAME: token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


@pytest.mark.asyncio
async def test_logout_clears_session(client: AsyncClient):
    """POST /logout elimina la cookie de sesión y redirige a /login."""
    csrf_token = "test-csrf-token-for-logout"
    response = await client.post(
        "/logout",
        data={"csrf_token": csrf_token},
        cookies={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_logout_csrf_missing_cookie_returns_403(client: AsyncClient):
    """POST /logout sin cookie CSRF devuelve 403."""
    response = await client.post(
        "/logout",
        data={"csrf_token": "any-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_logout_csrf_mismatch_returns_403(client: AsyncClient):
    """POST /logout con token de formulario distinto a la cookie devuelve 403."""
    response = await client.post(
        "/logout",
        data={"csrf_token": "token-a"},
        cookies={"csrf_token": "token-b"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_sanitize_next_url_unit():
    """Prueba unitaria de sanitización de open redirect."""
    assert sanitize_next_url("/dashboard") == "/dashboard"
    assert sanitize_next_url("/servicios?tab=1") == "/servicios?tab=1"
    assert sanitize_next_url("https://malicious.com") == "/dashboard"
    assert sanitize_next_url("//malicious.com") == "/dashboard"
    assert sanitize_next_url("javascript:alert(1)") == "/dashboard"
    assert sanitize_next_url("") == "/dashboard"
    assert sanitize_next_url(None) == "/dashboard"
