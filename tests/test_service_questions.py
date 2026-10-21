"""
Preguntas personalizadas por servicio (Fase 5, ítem 42).

T1: el panel de servicios permite cargar hasta 3 preguntas de texto, cada una
obligatoria u opcional (Service.questions).
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import Service
from tests.test_panel_mp_connect import _create_tenant, _make_cookie
from tests.test_service_buffer import _post, _service_form


async def _only_service(db_session, tenant) -> Service:
    rows = await db_session.execute(
        select(Service).where(Service.tenant_id == tenant.id)
    )
    return rows.scalar_one()


def _tenant_suffix(name: str) -> str:
    return f"q-{name}-{uuid.uuid4().hex[:6]}"


@pytest.mark.asyncio
async def test_new_service_saves_questions(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("new"))
    form = _service_form(
        question_1="  ¿Qué modelo   es tu\nauto? ",
        question_1_required="on",
        question_2="",
        question_2_required="on",  # sin texto: la fila se ignora
        question_3="¿Alguna indicación?",
    )

    res = await _post(client, tenant, "/panel/services/new", form)

    assert res.status_code == 303
    service = await _only_service(db_session, tenant)
    assert service.questions == [
        {"text": "¿Qué modelo es tu auto?", "required": True},
        {"text": "¿Alguna indicación?", "required": False},
    ]

    cookies = {"juturno_session": _make_cookie(tenant)}
    client.cookies.clear()
    page = await client.get(f"/panel/services/{service.id}/edit", cookies=cookies)
    assert 'value="¿Qué modelo es tu auto?"' in page.text
    assert 'id="question_1_required" name="question_1_required" value="on" checked' in (
        page.text
    )
    assert 'id="question_2_required" name="question_2_required" value="on">' in (
        page.text
    )
    listing = await client.get("/panel/services", cookies=cookies)
    assert "2 preguntas" in listing.text


@pytest.mark.asyncio
async def test_new_service_without_questions_has_empty_list(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("none"))

    res = await _post(client, tenant, "/panel/services/new", _service_form())

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == []


@pytest.mark.asyncio
async def test_edit_replaces_and_clears_questions(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("edit"))
    await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="¿Primera?")
    )
    service = await _only_service(db_session, tenant)
    url = f"/panel/services/{service.id}/edit"

    res = await _post(
        client,
        tenant,
        url,
        _service_form(question_1="", question_2="¿Segunda?", question_2_required="on"),
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == [{"text": "¿Segunda?", "required": True}]

    res = await _post(
        client, tenant, url, _service_form(question_1="", question_2="", question_3="")
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == []


@pytest.mark.asyncio
async def test_edit_without_question_fields_keeps_them(client, db_session):
    """Un formulario viejo (sin los campos de preguntas) no las borra."""
    tenant = await _create_tenant(db_session, _tenant_suffix("keep"))
    await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="¿Queda?")
    )
    service = await _only_service(db_session, tenant)

    res = await _post(
        client, tenant, f"/panel/services/{service.id}/edit", _service_form()
    )

    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == [{"text": "¿Queda?", "required": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, error",
    [
        ("x" * 121, "La pregunta puede tener hasta 120 caracteres."),
        ("¿Hola\x00?", "La pregunta tiene caracteres no permitidos."),
    ],
)
async def test_invalid_question_is_rejected(client, db_session, text, error):
    tenant = await _create_tenant(db_session, _tenant_suffix("bad"))

    res = await _post(
        client, tenant, "/panel/services/new", _service_form(question_2=text)
    )

    assert res.status_code == 200
    assert error in res.text
    rows = await db_session.execute(
        select(Service).where(Service.tenant_id == tenant.id)
    )
    assert rows.scalars().all() == []


@pytest.mark.asyncio
async def test_question_of_exactly_120_chars_is_accepted(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("max"))

    res = await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="x" * 120)
    )

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == [
        {"text": "x" * 120, "required": False}
    ]


@pytest.mark.asyncio
async def test_empty_middle_row_is_compacted(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("mid"))

    res = await _post(
        client,
        tenant,
        "/panel/services/new",
        _service_form(question_1="", question_2="¿Segunda?", question_3="¿Tercera?"),
    )

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == [
        {"text": "¿Segunda?", "required": False},
        {"text": "¿Tercera?", "required": False},
    ]
