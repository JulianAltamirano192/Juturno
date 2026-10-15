"""
Color de marca de la página pública (Fase 4, ítem 41): el negocio elige un hex
libre en Ajustes; se rechazan los que no se leen sobre el fondo oscuro y el
texto del botón principal va en blanco o negro según cuál contraste más.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.brand import MIN_CONTRAST, PAGE_BG, brand_theme, clean_brand_color, contrast
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

# ---------------------------------------------------------------------------
# Validación y tema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("#2563EB", "#2563eb"),
        ("  #22d3ee ", "#22d3ee"),
        ("#ffffff", "#ffffff"),
        ("", None),
        (None, None),
    ],
)
def test_clean_brand_color_accepts_readable_hex(raw, expected):
    assert clean_brand_color(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["red", "#fff", "#12345g", "2563eb", "#2563eb;}body{display:none", "#2563eb\nx"],
)
def test_clean_brand_color_rejects_bad_format(raw):
    with pytest.raises(ValueError, match="invalid"):
        clean_brand_color(raw)


@pytest.mark.parametrize("raw", ["#000000", "#08080b", "#1e3a8a", "#3b0764"])
def test_clean_brand_color_rejects_colors_too_dark_for_the_page(raw):
    with pytest.raises(ValueError, match="too_dark"):
        clean_brand_color(raw)


def test_contrast_matches_wcag():
    assert contrast((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0)
    assert contrast((0, 0, 0), (0, 0, 0)) == pytest.approx(1.0)


def test_theme_picks_dark_text_on_light_colors_and_white_on_saturated():
    assert brand_theme("#facc15")["--cta-text"] == "#08080b"
    assert brand_theme("#2563eb")["--cta-text"] == "#ffffff"


def test_theme_derives_all_accent_variables_from_the_color():
    theme = brand_theme("#2563eb")
    assert theme["--cyan"] == "#2563eb"
    assert theme["--cyan-rgb"] == "37,99,235"
    assert theme["--purple-rgb"] == "37,99,235"
    assert theme["--glow-rgb"] == "37,99,235"
    # Variantes más claras, para texto y bordes sobre el fondo oscuro.
    assert theme["--cyan-l"] != "#2563eb"
    assert theme["--grad"].startswith("linear-gradient(90deg,#2563eb,")


# ---------------------------------------------------------------------------
# Panel: POST /panel/settings/brand-color
# ---------------------------------------------------------------------------


async def _post_color(client, tenant, value, *, with_csrf=True):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"brand_color": value}
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/brand-color",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _settings_page(client, tenant, query):
    return await client.get(
        f"/panel/settings?{query}", cookies={"juturno_session": _make_cookie(tenant)}
    )


@pytest.mark.asyncio
async def test_panel_saves_brand_color(client, db_session):
    tenant = await _create_tenant(db_session, "brand-save")
    other = await _create_tenant(db_session, "brand-other")

    res = await _post_color(client, tenant, "#2563EB")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?brand=saved"
    await db_session.refresh(tenant)
    assert tenant.brand_color == "#2563eb"
    await db_session.refresh(other)
    assert other.brand_color is None

    page = await _settings_page(client, tenant, "brand=saved")
    assert "banner-ok" in page.text
    assert "Guardamos el color" in page.text
    assert (
        'type="color" id="brand_color" name="brand_color" value="#2563eb"' in page.text
    )


@pytest.mark.parametrize(
    "value,error",
    [("rojo", "invalid"), ("#2563eb;}body{", "invalid"), ("#000000", "too_dark")],
)
@pytest.mark.asyncio
async def test_panel_rejects_bad_brand_color(client, db_session, value, error):
    tenant = await _create_tenant(db_session, "brand-bad")
    tenant.brand_color = "#22d3ee"
    db_session.add(tenant)
    await db_session.commit()

    res = await _post_color(client, tenant, value)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/panel/settings?brand={error}"
    await db_session.refresh(tenant)
    assert tenant.brand_color == "#22d3ee"

    page = await _settings_page(client, tenant, f"brand={error}")
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_panel_empty_brand_color_restores_default(client, db_session):
    tenant = await _create_tenant(db_session, "brand-reset")
    tenant.brand_color = "#2563eb"
    db_session.add(tenant)
    await db_session.commit()

    res = await _post_color(client, tenant, "")
    assert res.headers["location"] == "/panel/settings?brand=removed"
    await db_session.refresh(tenant)
    assert tenant.brand_color is None

    # Sin color propio, el selector arranca en el cian de Juturno.
    page = await _settings_page(client, tenant, "brand=removed")
    assert "banner-ok" in page.text
    assert 'name="brand_color" value="#22d3ee"' in page.text


@pytest.mark.asyncio
async def test_panel_brand_color_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "brand-csrf")
    res = await _post_color(client, tenant, "#2563eb", with_csrf=False)
    assert res.status_code == 403
    await db_session.refresh(tenant)
    assert tenant.brand_color is None


@pytest.mark.asyncio
async def test_panel_brand_color_requires_session(client):
    client.cookies.clear()
    res = await client.post(
        "/panel/settings/brand-color",
        data={"brand_color": "#2563eb"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303), res.text
    assert "/login" in res.headers["location"]


# ---------------------------------------------------------------------------
# Página pública y DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_page_uses_brand_color(client, db_session):
    tenant = await _create_tenant(db_session, "brand-pub")
    tenant.brand_color = "#facc15"
    db_session.add(tenant)
    await db_session.commit()

    page = await client.get(f"/t/{tenant.slug}")
    assert '<style id="brand">' in page.text
    assert "--cyan:#facc15;" in page.text
    assert "--cyan-rgb:250,204,21;" in page.text
    assert "--cta-text:#08080b;" in page.text


@pytest.mark.asyncio
async def test_public_page_without_brand_color_keeps_default_theme(client, db_session):
    tenant = await _create_tenant(db_session, "brand-default")
    page = await client.get(f"/t/{tenant.slug}")
    assert '<style id="brand">' not in page.text
    assert "--cyan:#22d3ee;" in page.text
    assert "--cyan-rgb:34,211,238;" in page.text
    # Los acentos usan las variables, no colores fijos.
    assert "rgba(34,211,238" not in page.text


@pytest.mark.asyncio
async def test_db_rejects_invalid_brand_color(db_session):
    tenant = await _create_tenant(db_session, "brand-check")
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE tenant SET brand_color = '#FFF;}' WHERE id = :id"),
            {"id": tenant.id},
        )
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Hallazgos del code-review
# ---------------------------------------------------------------------------


def test_theme_mix_values_are_exact():
    theme = brand_theme("#2563eb")
    # 35% y 60% hacia blanco, 20% hacia negro, redondeando cada canal.
    assert theme["--cyan-l"] == "#719af2"
    assert theme["--cyan-l-rgb"] == "113,154,242"
    assert theme["--cyan-xl"] == "#a8c1f7"
    assert theme["--purple-l"] == "#719af2"
    assert theme["--purple-l-rgb"] == "113,154,242"
    assert theme["--grad"] == "linear-gradient(90deg,#2563eb,#1e4fbc)"


def test_contrast_threshold_boundary():
    # El primer gris que llega a 3:1 contra el fondo se acepta; el anterior no.
    gray = next(g for g in range(256) if contrast((g, g, g), PAGE_BG) >= MIN_CONTRAST)
    assert clean_brand_color(f"#{gray:02x}{gray:02x}{gray:02x}")
    with pytest.raises(ValueError, match="too_dark"):
        clean_brand_color("#{0:02x}{0:02x}{0:02x}".format(gray - 1))


@pytest.mark.asyncio
async def test_saving_the_default_color_keeps_the_juturno_theme(client, db_session):
    # El <input type="color"> siempre manda valor: "Guardar" sin tocarlo no
    # tiene que fijar un cian plano en lugar del tema con degradé.
    tenant = await _create_tenant(db_session, "brand-default-save")
    res = await _post_color(client, tenant, "#22D3EE")
    assert res.headers["location"] == "/panel/settings?brand=removed"
    await db_session.refresh(tenant)
    assert tenant.brand_color is None


@pytest.mark.asyncio
async def test_reset_button_only_shows_with_a_brand_color(client, db_session):
    tenant = await _create_tenant(db_session, "brand-reset-btn")
    page = await _settings_page(client, tenant, "")
    assert "Usar el color de Juturno" not in page.text
    tenant.brand_color = "#2563eb"
    db_session.add(tenant)
    await db_session.commit()
    page = await _settings_page(client, tenant, "")
    assert "Usar el color de Juturno" in page.text


@pytest.mark.asyncio
async def test_default_theme_keeps_every_original_accent(client, db_session):
    tenant = await _create_tenant(db_session, "brand-parity")
    page = await client.get(f"/t/{tenant.slug}")
    # Cada literal reemplazado sigue valiendo lo mismo sin color de marca.
    for declaration in (
        "--cyan-rgb:34,211,238;",
        "--cyan-l-rgb:103,232,249;",
        "--purple-rgb:168,85,247;",
        "--purple-l:#c084fc;",
        "--purple-l-rgb:192,132,252;",
        "--glow-rgb:217,70,239;",
        "--cta-text:#fff;",
    ):
        assert declaration in page.text, declaration
    assert "border:1px solid rgba(var(--purple-l-rgb),.6)" in page.text
    # Los íconos del resultado también siguen el tema (antes, colores fijos).
    assert 'stroke="#67e8f9"' not in page.text
    assert 'stroke="#c084fc"' not in page.text
    assert "stroke:var(--cyan-l)" in page.text
    assert "stroke:var(--purple-l)" in page.text


@pytest.mark.parametrize(
    "stored",
    ["#+f+f+f", "#2563eb;}body{background:url(https://evil/x)}", "#2563EB", "", None],
)
def test_theme_ignores_values_that_did_not_pass_validation(stored):
    # Si algún día llega un valor sin validar (otro endpoint, un script), no
    # se escribe en el <style>: la página queda con el tema por defecto.
    assert brand_theme(stored) is None
