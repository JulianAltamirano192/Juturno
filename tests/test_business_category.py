"""
Rubro del negocio (Fase 4, ítem 14): lista genérica + "Otro" con texto libre.
Obligatorio al registrarse y editable en Ajustes.
"""

import pytest
from sqlalchemy import select

from app.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from app.models import BUSINESS_CATEGORIES, Tenant
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

REGISTER_ERROR = "Elegí el rubro de tu negocio"
OTHER_ERROR = "Escribí el rubro de tu negocio"

# ---------------------------------------------------------------------------
# Registro: POST /register
# ---------------------------------------------------------------------------


async def _register(client, email: str, **fields):
    csrf = generate_csrf_token()
    data = {
        "name": "Negocio Rubro",
        "owner_email": email,
        "password": "supersecretpassword123",
        "csrf_token": csrf,
        **fields,
    }
    return await client.post(
        "/register",
        data=data,
        cookies={CSRF_COOKIE_NAME: csrf},
        follow_redirects=False,
    )


async def _tenant_by_email(db_session, email: str) -> Tenant | None:
    result = await db_session.execute(select(Tenant).where(Tenant.owner_email == email))
    return result.scalar_one_or_none()


def test_category_list_matches_product_decision():
    assert list(BUSINESS_CATEGORIES.values()) == [
        "Peluquería y barbería",
        "Estética y belleza (uñas, cejas, pestañas)",
        "Tatuajes y piercing",
        "Masajes y spa",
        "Salud (kinesiología, nutrición, psicología, odontología)",
        "Entrenamiento y deporte",
        "Clases y tutorías",
        "Lavadero de autos",
        "Mascotas (peluquería canina, veterinaria)",
        "Fotografía",
        "Reparaciones y servicio técnico",
        "Otro",
    ]


@pytest.mark.asyncio
async def test_register_page_lists_categories(client):
    res = await client.get("/register")
    assert 'name="business_category"' in res.text
    assert 'name="business_category_other"' in res.text
    for code, label in BUSINESS_CATEGORIES.items():
        assert f'<option value="{code}"' in res.text
        assert label in res.text


@pytest.mark.asyncio
async def test_register_saves_category(client, db_session):
    # El texto de "Otro" se ignora si el rubro es de la lista.
    res = await _register(
        client,
        "rubro-ok@test.com",
        business_category="tatuajes",
        business_category_other="algo",
    )
    assert res.status_code == 303, res.text
    tenant = await _tenant_by_email(db_session, "rubro-ok@test.com")
    assert tenant is not None
    assert tenant.business_category == "tatuajes"
    assert tenant.business_category_other is None


@pytest.mark.asyncio
async def test_register_saves_other_category_text(client, db_session):
    res = await _register(
        client,
        "rubro-otro@test.com",
        business_category="otro",
        business_category_other="  Cerrajería   24 h ",
    )
    assert res.status_code == 303, res.text
    tenant = await _tenant_by_email(db_session, "rubro-otro@test.com")
    assert tenant is not None
    assert tenant.business_category == "otro"
    assert tenant.business_category_other == "Cerrajería 24 h"


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{}, {"business_category": "inventado"}])
async def test_register_requires_valid_category(client, db_session, fields):
    res = await _register(client, "rubro-falta@test.com", **fields)
    assert res.status_code == 400
    assert REGISTER_ERROR in res.text
    assert await _tenant_by_email(db_session, "rubro-falta@test.com") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("other", ["", "  ", "a", "x" * 61, "Hola\x00"])
async def test_register_other_requires_valid_text(client, db_session, other):
    res = await _register(
        client,
        "rubro-otro-mal@test.com",
        business_category="otro",
        business_category_other=other,
    )
    assert res.status_code == 400
    assert OTHER_ERROR in res.text
    # El formulario conserva el rubro elegido para que lo corrija.
    assert '<option value="otro" selected>' in res.text
    assert await _tenant_by_email(db_session, "rubro-otro-mal@test.com") is None


# ---------------------------------------------------------------------------
# Panel: POST /panel/settings/category
# ---------------------------------------------------------------------------


async def _post_category(client, tenant, *, with_csrf=True, **fields):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = dict(fields)
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/category",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _tenant_with_category(db_session, suffix, category=None, other=None):
    tenant = await _create_tenant(db_session, suffix)
    tenant.business_category = category
    tenant.business_category_other = other
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _stored(db_session, tenant):
    await db_session.refresh(tenant)
    return tenant.business_category, tenant.business_category_other


@pytest.mark.asyncio
async def test_panel_saves_category(client, db_session):
    tenant = await _tenant_with_category(db_session, "cat-save")
    other = await _tenant_with_category(db_session, "cat-other", "fotografia")

    res = await _post_category(
        client, tenant, business_category="otro", business_category_other="Cerrajería"
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?category=saved"
    assert await _stored(db_session, tenant) == ("otro", "Cerrajería")
    # El rubro de otro negocio no se toca.
    assert await _stored(db_session, other) == ("fotografia", None)

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-ok" in page.text
    assert "Guardamos el rubro" in page.text
    assert '<option value="otro" selected>' in page.text
    assert 'value="Cerrajería"' in page.text


@pytest.mark.asyncio
async def test_panel_switching_from_other_clears_text(client, db_session):
    tenant = await _tenant_with_category(db_session, "cat-switch", "otro", "Cerrajería")
    res = await _post_category(client, tenant, business_category="mascotas")
    assert res.headers["location"] == "/panel/settings?category=saved"
    assert await _stored(db_session, tenant) == ("mascotas", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"business_category": "inventado"},
        {"business_category": ""},
        {"business_category": "otro", "business_category_other": " "},
    ],
)
async def test_panel_invalid_category_keeps_previous(client, db_session, fields):
    tenant = await _tenant_with_category(db_session, "cat-inv", "clases")
    res = await _post_category(client, tenant, **fields)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?category=invalid"
    assert await _stored(db_session, tenant) == ("clases", None)

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_panel_category_requires_csrf(client, db_session):
    tenant = await _tenant_with_category(db_session, "cat-csrf")
    res = await _post_category(
        client, tenant, with_csrf=False, business_category="clases"
    )
    assert res.status_code == 403
    assert await _stored(db_session, tenant) == (None, None)


@pytest.mark.asyncio
async def test_panel_settings_without_category_selects_placeholder(client, db_session):
    # Negocios anteriores al ítem 14: sin rubro, el desplegable pide elegir uno.
    tenant = await _tenant_with_category(db_session, "cat-none")
    page = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert '<option value="" selected disabled>' in page.text


@pytest.mark.asyncio
async def test_register_error_escapes_other_text(client):
    # El texto de "Otro" vuelve al formulario escapado (no rompe el atributo).
    res = await _register(
        client,
        "rubro-xss@test.com",
        business_category="otro",
        business_category_other='"><script>x</script>' + "y" * 60,
    )
    assert res.status_code == 400
    assert "<script>x</script>" not in res.text
    assert "&#34;&gt;&lt;script&gt;x&lt;/script&gt;" in res.text
