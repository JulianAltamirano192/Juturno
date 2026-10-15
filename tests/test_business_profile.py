"""
Perfil del negocio (Fase 4, ítem 15): localidad, Instagram, Facebook y logo,
editables en Ajustes y mostrados en la página pública /t/{slug}.

El logo se guarda en Postgres (tabla tenant_logo) re-codificado a WebP de hasta
512 px y sin metadatos, y se sirve en /t/{slug}/logo.
"""

import io
import re

import pytest
from PIL import Image

from app.logo import LOGO_MAX_SIDE, LOGO_UPLOAD_MAX_BYTES
from app.models import TenantLogo
from tests import test_rate_limiting
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _image_bytes(fmt: str, size=(800, 400), exif=None) -> bytes:
    buf = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" else "RGB"
    img = Image.new(mode, size, (200, 30, 90, 255) if mode == "RGBA" else (200, 30, 90))
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


async def _post_profile(client, tenant, **fields):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"locality": "", "instagram": "", "facebook": "", **fields}
    data["csrf_token"] = csrf
    client.cookies.clear()
    return await client.post(
        "/panel/settings/profile", data=data, cookies=cookies, follow_redirects=False
    )


async def _post_logo(client, tenant, content: bytes, *, with_csrf=True, headers=None):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    if not with_csrf:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/logo",
        data={"csrf_token": csrf},
        files={"logo": ("logo.png", content, "image/png")},
        cookies=cookies,
        headers=headers or {},
        follow_redirects=False,
    )


async def _remove_logo(client, tenant):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/logo/remove",
        data={"csrf_token": csrf},
        cookies=cookies,
        follow_redirects=False,
    )


async def _settings_page(client, tenant, query: str):
    return await client.get(
        f"/panel/settings?{query}", cookies={"juturno_session": _make_cookie(tenant)}
    )


# ---------------------------------------------------------------------------
# Localidad y redes: POST /panel/settings/profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_saves_profile_normalizing_handles(client, db_session):
    tenant = await _create_tenant(db_session, "profile-save")
    other = await _create_tenant(db_session, "profile-other")

    res = await _post_profile(
        client,
        tenant,
        locality="  Palermo,   CABA ",
        instagram="https://www.instagram.com/Mi.Negocio_/?hl=es",
        facebook="https://m.facebook.com/profile.php?id=100012345678&ref=x",
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?profile=saved"

    await db_session.refresh(tenant)
    assert tenant.locality == "Palermo, CABA"
    # Se guarda solo el usuario: el link lo arma la plantilla.
    assert tenant.instagram == "mi.negocio_"
    assert tenant.facebook == "profile.php?id=100012345678"
    # El negocio de al lado no se toca.
    await db_session.refresh(other)
    assert (other.locality, other.instagram, other.facebook) == (None, None, None)

    page = await _settings_page(client, tenant, "profile=saved")
    assert "banner-ok" in page.text
    assert "Guardamos el perfil" in page.text
    assert 'value="Palermo, CABA"' in page.text
    assert 'value="mi.negocio_"' in page.text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@minegocio", "minegocio"),
        ("instagram.com/minegocio/reels", "minegocio"),
        ("MiNegocio", "minegocio"),
    ],
)
@pytest.mark.asyncio
async def test_instagram_accepts_handle_or_link(client, db_session, raw, expected):
    tenant = await _create_tenant(db_session, "profile-ig")
    res = await _post_profile(client, tenant, instagram=raw)
    assert res.headers["location"] == "/panel/settings?profile=saved"
    await db_session.refresh(tenant)
    assert tenant.instagram == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fb.com/Mi.Negocio", "Mi.Negocio"),
        ("@minegocio", "minegocio"),
        ("https://www.facebook.com/minegocio/", "minegocio"),
    ],
)
@pytest.mark.asyncio
async def test_facebook_accepts_handle_or_link(client, db_session, raw, expected):
    tenant = await _create_tenant(db_session, "profile-fb")
    res = await _post_profile(client, tenant, facebook=raw)
    assert res.headers["location"] == "/panel/settings?profile=saved"
    await db_session.refresh(tenant)
    assert tenant.facebook == expected


@pytest.mark.parametrize(
    "fields,error",
    [
        ({"locality": "X"}, "invalid_locality"),
        ({"locality": "a" * 61}, "invalid_locality"),
        ({"locality": "Palermo\x00"}, "invalid_locality"),
        ({"instagram": "javascript:alert(1)"}, "invalid_instagram"),
        ({"instagram": "https://facebook.com/minegocio"}, "invalid_instagram"),
        ({"instagram": "a" * 31}, "invalid_instagram"),
        ({"facebook": "https://evil.com/minegocio"}, "invalid_facebook"),
        ({"facebook": "abc"}, "invalid_facebook"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_profile_is_rejected_without_saving(
    client, db_session, fields, error
):
    tenant = await _create_tenant(db_session, "profile-bad")
    tenant.locality = "Rosario"
    db_session.add(tenant)
    await db_session.commit()

    # Los demás campos válidos tampoco se guardan si uno falla.
    res = await _post_profile(
        client, tenant, **{"locality": "Córdoba", "instagram": "ok", **fields}
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/panel/settings?profile={error}"
    await db_session.refresh(tenant)
    assert (tenant.locality, tenant.instagram) == ("Rosario", None)

    page = await _settings_page(client, tenant, f"profile={error}")
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_blank_profile_clears_fields(client, db_session):
    tenant = await _create_tenant(db_session, "profile-clear")
    tenant.locality, tenant.instagram, tenant.facebook = "Rosario", "ig", "fbpage"
    db_session.add(tenant)
    await db_session.commit()

    res = await _post_profile(client, tenant, locality=" ", instagram="", facebook="")
    assert res.headers["location"] == "/panel/settings?profile=saved"
    await db_session.refresh(tenant)
    assert (tenant.locality, tenant.instagram, tenant.facebook) == (None, None, None)


@pytest.mark.asyncio
async def test_profile_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "profile-csrf")
    client.cookies.clear()
    res = await client.post(
        "/panel/settings/profile",
        data={"locality": "Rosario"},
        cookies={"juturno_session": _make_cookie(tenant)},
        follow_redirects=False,
    )
    assert res.status_code == 403
    await db_session.refresh(tenant)
    assert tenant.locality is None


# ---------------------------------------------------------------------------
# Logo: POST /panel/settings/logo y /panel/settings/logo/remove
# ---------------------------------------------------------------------------


async def _stored_logo(db_session, tenant) -> TenantLogo | None:
    # populate_existing: relee de la DB aunque la fila ya esté en la sesión.
    return await db_session.get(TenantLogo, tenant.id, populate_existing=True)


@pytest.mark.asyncio
async def test_upload_logo_reencodes_to_small_webp(client, db_session):
    tenant = await _create_tenant(db_session, "logo-up")
    other = await _create_tenant(db_session, "logo-other")

    res = await _post_logo(client, tenant, _image_bytes("PNG", size=(2000, 1000)))
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=saved"

    logo = await _stored_logo(db_session, tenant)
    assert logo is not None
    img = Image.open(io.BytesIO(logo.data))
    assert img.format == "WEBP"
    assert img.size == (LOGO_MAX_SIDE, LOGO_MAX_SIDE // 2)
    assert await _stored_logo(db_session, other) is None

    page = await _settings_page(client, tenant, "logo=saved")
    assert "banner-ok" in page.text
    assert f'src="/t/{tenant.slug}/logo?v=' in page.text


@pytest.mark.asyncio
async def test_upload_logo_strips_metadata_and_applies_rotation(client, db_session):
    tenant = await _create_tenant(db_session, "logo-exif")
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotar 90° (foto de celular de costado)
    exif[0x010F] = "Camara del celular"
    res = await _post_logo(client, tenant, _image_bytes("JPEG", (400, 200), exif))
    assert res.headers["location"] == "/panel/settings?logo=saved"

    img = Image.open(io.BytesIO((await _stored_logo(db_session, tenant)).data))
    assert img.size == (200, 400)
    assert len(img.getexif()) == 0


@pytest.mark.asyncio
async def test_replacing_logo_keeps_one_row(client, db_session):
    tenant = await _create_tenant(db_session, "logo-replace")
    await _post_logo(client, tenant, _image_bytes("PNG", size=(100, 100)))
    res = await _post_logo(client, tenant, _image_bytes("JPEG", size=(300, 100)))
    assert res.headers["location"] == "/panel/settings?logo=saved"
    img = Image.open(io.BytesIO((await _stored_logo(db_session, tenant)).data))
    assert img.size == (300, 100)


@pytest.mark.parametrize(
    "content",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"\x89PNG\r\n\x1a\n" + b"no es una imagen",
        b"",
    ],
)
@pytest.mark.asyncio
async def test_upload_rejects_non_images(client, db_session, content):
    tenant = await _create_tenant(db_session, "logo-bad")
    res = await _post_logo(client, tenant, content)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=not_image"
    assert await _stored_logo(db_session, tenant) is None

    page = await _settings_page(client, tenant, "logo=not_image")
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_upload_rejects_gif(client, db_session):
    tenant = await _create_tenant(db_session, "logo-gif")
    res = await _post_logo(client, tenant, _image_bytes("GIF", size=(50, 50)))
    assert res.headers["location"] == "/panel/settings?logo=not_image"
    assert await _stored_logo(db_session, tenant) is None


@pytest.mark.asyncio
async def test_upload_rejects_too_big(client, db_session):
    tenant = await _create_tenant(db_session, "logo-big")
    big = b"\x00" * (LOGO_UPLOAD_MAX_BYTES + 1)
    res = await _post_logo(client, tenant, big)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=too_big"
    assert await _stored_logo(db_session, tenant) is None
    page = await _settings_page(client, tenant, "logo=too_big")
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_upload_logo_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "logo-csrf")
    res = await _post_logo(client, tenant, _image_bytes("PNG"), with_csrf=False)
    assert res.status_code == 403
    assert await _stored_logo(db_session, tenant) is None


@pytest.mark.asyncio
async def test_remove_logo(client, db_session):
    tenant = await _create_tenant(db_session, "logo-rm")
    other = await _create_tenant(db_session, "logo-rm-other")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    await _post_logo(client, other, _image_bytes("PNG"))

    res = await _remove_logo(client, tenant)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=removed"
    assert await _stored_logo(db_session, tenant) is None
    assert await _stored_logo(db_session, other) is not None
    assert (await client.get(f"/t/{tenant.slug}/logo")).status_code == 404


# ---------------------------------------------------------------------------
# Página pública
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_logo_is_served_as_webp_with_safe_headers(client, db_session):
    tenant = await _create_tenant(db_session, "logo-pub")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    client.cookies.clear()

    res = await client.get(f"/t/{tenant.slug}/logo")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/webp"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "public" in res.headers["cache-control"]
    assert Image.open(io.BytesIO(res.content)).format == "WEBP"

    page = await client.get(f"/t/{tenant.slug}")
    m = re.search(
        rf'<img class="biz-avatar" src="/t/{tenant.slug}/logo\?v=\d+"', page.text
    )
    assert m, page.text


@pytest.mark.asyncio
async def test_public_logo_404_without_logo_or_tenant(client, db_session):
    tenant = await _create_tenant(db_session, "logo-none")
    assert (await client.get(f"/t/{tenant.slug}/logo")).status_code == 404
    assert (await client.get("/t/no-existe/logo")).status_code == 404
    # Sin logo sigue la inicial del negocio.
    page = await client.get(f"/t/{tenant.slug}")
    assert '<span class="biz-avatar" aria-hidden="true">T</span>' in page.text


@pytest.mark.asyncio
async def test_public_page_shows_locality_and_social_links(client, db_session):
    tenant = await _create_tenant(db_session, "profile-pub")
    tenant.locality = "Villa <b>Crespo</b>"
    tenant.instagram = "mi.negocio"
    tenant.facebook = "profile.php?id=1000123"
    db_session.add(tenant)
    await db_session.commit()

    page = await client.get(f"/t/{tenant.slug}")
    assert "Villa &lt;b&gt;Crespo&lt;/b&gt;" in page.text
    assert 'href="https://www.instagram.com/mi.negocio/"' in page.text
    assert 'href="https://www.facebook.com/profile.php?id=1000123"' in page.text
    assert 'rel="noopener noreferrer"' in page.text


@pytest.mark.asyncio
async def test_public_page_without_profile_has_no_links(client, db_session):
    tenant = await _create_tenant(db_session, "profile-empty")
    page = await client.get(f"/t/{tenant.slug}")
    assert "instagram.com" not in page.text
    assert "facebook.com" not in page.text
    assert 'class="biz-meta"' not in page.text


# ---------------------------------------------------------------------------
# Hallazgos del code-review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Páginas sin usuario: slug con guiones y el id al final.
        ("facebook.com/Peluqueria-Juan-100063123456", "Peluqueria-Juan-100063123456"),
        # Forma /people/Nombre/id → mismo perfil por id.
        (
            "https://www.facebook.com/people/Peluqueria-Juan/100063123456/",
            "profile.php?id=100063123456",
        ),
    ],
)
@pytest.mark.asyncio
async def test_facebook_accepts_pages_without_username(
    client, db_session, raw, expected
):
    tenant = await _create_tenant(db_session, "profile-fb-page")
    res = await _post_profile(client, tenant, facebook=raw)
    assert res.headers["location"] == "/panel/settings?profile=saved"
    await db_session.refresh(tenant)
    assert tenant.facebook == expected


@pytest.mark.parametrize(
    "fields,error",
    [
        ({"instagram": "https://www.instagram.com/p/Cx1abc/"}, "invalid_instagram"),
        ({"instagram": "instagram.com/explore"}, "invalid_instagram"),
        ({"instagram": "instagram.com/stories/minegocio/123"}, "invalid_instagram"),
        ({"instagram": "instagram.com/reel/abc"}, "invalid_instagram"),
        ({"facebook": "facebook.com/share/abc123"}, "invalid_facebook"),
        ({"facebook": "facebook.com/groups/vecinos"}, "invalid_facebook"),
        ({"facebook": "facebook.com/profile.php"}, "invalid_facebook"),
        # Dígitos no ASCII (árabes) en el id.
        ({"facebook": "facebook.com/profile.php?id=١٢٣٤٥"}, "invalid_facebook"),
    ],
)
@pytest.mark.asyncio
async def test_links_to_sections_are_not_saved_as_users(
    client, db_session, fields, error
):
    tenant = await _create_tenant(db_session, "profile-section")
    res = await _post_profile(client, tenant, **fields)
    assert res.headers["location"] == f"/panel/settings?profile={error}"
    await db_session.refresh(tenant)
    assert (tenant.instagram, tenant.facebook) == (None, None)


@pytest.mark.asyncio
async def test_upload_rejects_big_content_length_before_parsing(client, db_session):
    tenant = await _create_tenant(db_session, "logo-cl")
    big = b"\x00" * (LOGO_UPLOAD_MAX_BYTES + 128 * 1024)
    # Sin CSRF válido: si leyera el form antes, daría 403 en vez de too_big.
    res = await _post_logo(client, tenant, big, with_csrf=False)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=too_big"
    assert await _stored_logo(db_session, tenant) is None


@pytest.mark.asyncio
async def test_upload_rejects_chunked_body_without_length(client, db_session):
    tenant = await _create_tenant(db_session, "logo-chunked")

    async def body():
        yield b"--x\r\n"

    client.cookies.clear()
    res = await client.post(
        "/panel/settings/logo",
        content=body(),
        headers={"content-type": "multipart/form-data; boundary=x"},
        cookies={"juturno_session": _make_cookie(tenant)},
        follow_redirects=False,
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?logo=too_big"


@pytest.mark.asyncio
async def test_upload_rejects_pixel_bomb(client, db_session):
    tenant = await _create_tenant(db_session, "logo-bomb")
    buf = io.BytesIO()
    # Pocos KB comprimido, 30 MP decodificado.
    Image.new("L", (6000, 5000)).save(buf, "PNG")
    assert len(buf.getvalue()) < LOGO_UPLOAD_MAX_BYTES
    res = await _post_logo(client, tenant, buf.getvalue())
    assert res.headers["location"] == "/panel/settings?logo=not_image"
    assert await _stored_logo(db_session, tenant) is None


def _mode_image(mode: str, fmt: str) -> bytes:
    buf = io.BytesIO()
    img = Image.new(mode, (120, 60))
    kwargs = {"transparency": 0} if mode == "P" else {}
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


@pytest.mark.parametrize(
    "mode,fmt",
    [("P", "PNG"), ("L", "PNG"), ("LA", "PNG"), ("CMYK", "JPEG"), ("RGBA", "WEBP")],
)
@pytest.mark.asyncio
async def test_upload_accepts_other_image_modes(client, db_session, mode, fmt):
    tenant = await _create_tenant(db_session, "logo-mode")
    res = await _post_logo(client, tenant, _mode_image(mode, fmt))
    assert res.headers["location"] == "/panel/settings?logo=saved"
    img = Image.open(io.BytesIO((await _stored_logo(db_session, tenant)).data))
    assert (img.format, img.size) == ("WEBP", (120, 60))


@pytest.mark.asyncio
async def test_public_logo_is_per_tenant(client, db_session):
    a = await _create_tenant(db_session, "logo-iso-a")
    b = await _create_tenant(db_session, "logo-iso-b")
    await _post_logo(client, a, _image_bytes("PNG", size=(100, 100)))
    await _post_logo(client, b, _image_bytes("PNG", size=(200, 100)))
    client.cookies.clear()
    res_a = await client.get(f"/t/{a.slug}/logo")
    res_b = await client.get(f"/t/{b.slug}/logo")
    assert res_a.content == (await _stored_logo(db_session, a)).data
    assert res_b.content == (await _stored_logo(db_session, b)).data
    assert Image.open(io.BytesIO(res_a.content)).size == (100, 100)


@pytest.mark.parametrize(
    "path",
    ["/panel/settings/profile", "/panel/settings/logo", "/panel/settings/logo/remove"],
)
@pytest.mark.asyncio
async def test_profile_endpoints_require_session(client, path):
    client.cookies.clear()
    res = await client.post(path, data={"locality": "Rosario"}, follow_redirects=False)
    assert res.status_code in (302, 303), res.text
    assert "/login" in res.headers["location"]


@pytest.mark.asyncio
async def test_remove_logo_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "logo-rm-csrf")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    client.cookies.clear()
    res = await client.post(
        "/panel/settings/logo/remove",
        cookies={"juturno_session": _make_cookie(tenant)},
        follow_redirects=False,
    )
    assert res.status_code == 403
    assert await _stored_logo(db_session, tenant) is not None


# ---------------------------------------------------------------------------
# Hallazgos del security-auditor
# ---------------------------------------------------------------------------

rate_limited_client = test_rate_limiting.rate_limited_client


def _jpeg_declaring(width: int, height: int, progressive: bool) -> bytes:
    """JPEG chico cuyo header (SOF) declara otras dimensiones: Pillow las lee
    sin decodificar, así se prueba el tope sin generar 169 MP en el test."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "JPEG", progressive=progressive)
    data = bytearray(buf.getvalue())
    sof = data.find(b"\xff\xc2" if progressive else b"\xff\xc0")
    data[sof + 5 : sof + 9] = height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return bytes(data)


@pytest.mark.parametrize("progressive", [True, False])
@pytest.mark.asyncio
async def test_upload_rejects_huge_jpeg_before_draft(client, db_session, progressive):
    tenant = await _create_tenant(db_session, "logo-huge-jpeg")
    res = await _post_logo(client, tenant, _jpeg_declaring(13000, 13000, progressive))
    assert res.headers["location"] == "/panel/settings?logo=not_image"
    assert await _stored_logo(db_session, tenant) is None


@pytest.mark.parametrize(
    "fields,error",
    [
        ({"instagram": "."}, "invalid_instagram"),
        ({"instagram": "@.._"}, "invalid_instagram"),
        ({"facebook": "....."}, "invalid_facebook"),
        ({"facebook": "--.--"}, "invalid_facebook"),
    ],
)
@pytest.mark.asyncio
async def test_social_user_needs_a_letter_or_number(client, db_session, fields, error):
    tenant = await _create_tenant(db_session, "profile-dots")
    res = await _post_profile(client, tenant, **fields)
    assert res.headers["location"] == f"/panel/settings?profile={error}"


@pytest.mark.asyncio
async def test_logo_upload_is_rate_limited(rate_limited_client, db_session):
    tenant = await _create_tenant(db_session, "logo-rate")
    cookie = _make_cookie(tenant)
    codes = []
    for _ in range(11):
        res = await rate_limited_client.post(
            "/panel/settings/logo",
            data={"csrf_token": "x"},
            cookies={"juturno_session": cookie},
            follow_redirects=False,
        )
        codes.append(res.status_code)
    assert 429 not in codes[:10]
    assert codes[10] == 429


@pytest.mark.asyncio
async def test_public_logo_is_rate_limited(rate_limited_client, db_session):
    tenant = await _create_tenant(db_session, "logo-pub-rate")
    codes = [
        (await rate_limited_client.get(f"/t/{tenant.slug}/logo?v={i}")).status_code
        for i in range(121)
    ]
    assert set(codes[:120]) == {404}
    assert codes[120] == 429
