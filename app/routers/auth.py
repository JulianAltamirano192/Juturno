from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csrf import (
    CSRF_COOKIE_NAME,
    generate_csrf_token,
    set_csrf_cookie,
    validate_csrf,
    validate_csrf_double_submit,
)
from app.database import get_db
from app.limiter import limiter
from app.models import Tenant
from app.password import hash_password, verify_password
from app.session import (
    SESSION_COOKIE_NAME,
    delete_session_cookie,
    parse_session_token,
    sanitize_next_url,
    set_session_cookie,
)
from app.slug import generate_unique_slug
from app.templates import templates

router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Muestra el formulario de registro de negocio."""
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "register.html",
        {
            "csrf_token": csrf_token,
            "form_data": {},
            "error_message": None,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/register", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def register_submit(
    request: Request,
    name: Annotated[str, Form()],
    owner_email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    whatsapp_number: Annotated[str | None, Form()] = None,
    slug: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_db),
):
    """
    Registra un nuevo negocio y dueño de forma autoservicio.
    Valida CSRF (double-submit), contraseña mínima, unicidad de email normalizado
    y resuelve colisiones de slug automáticamente.
    """
    form_data = {
        "name": name,
        "owner_email": owner_email,
        "whatsapp_number": whatsapp_number or "",
        "slug": slug or "",
    }

    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not validate_csrf_double_submit(csrf_token, cookie_csrf):
        new_csrf = generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "El formulario expiró o es inválido. Por favor, intentá nuevamente.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    if len(password) < 8:
        new_csrf = generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "La contraseña debe tener al menos 8 caracteres.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    normalized_email = owner_email.strip().lower()
    existing_owner = (
        await session.execute(
            select(Tenant).where(Tenant.owner_email == normalized_email)
        )
    ).scalar_one_or_none()

    if existing_owner is not None:
        new_csrf = generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "Ya existe un negocio registrado con este correo electrónico.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    base_slug_text = slug.strip() if slug and slug.strip() else name.strip()
    final_slug = await generate_unique_slug(session, base_slug_text)

    pwd_hash = hash_password(password)

    clean_whatsapp = (
        whatsapp_number.strip() if whatsapp_number and whatsapp_number.strip() else None
    )
    new_tenant = Tenant(
        name=name.strip(),
        slug=final_slug,
        owner_email=normalized_email,
        password_hash=pwd_hash,
        whatsapp_number=clean_whatsapp,
    )
    session.add(new_tenant)
    await session.commit()
    await session.refresh(new_tenant)

    return RedirectResponse(
        url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    registered: str | None = Query(None),
    next: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """Muestra el formulario de inicio de sesión."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        parsed = parse_session_token(session_cookie)
        if parsed:
            tenant_id, session_version = parsed
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None and tenant.session_version == session_version:
                safe_next = sanitize_next_url(next)
                return RedirectResponse(
                    url=safe_next, status_code=status.HTTP_303_SEE_OTHER
                )

    csrf_token = generate_csrf_token(request)
    info_message = (
        "Tu cuenta fue creada con éxito. Iniciá sesión para continuar."
        if registered == "1"
        else None
    )
    safe_next = sanitize_next_url(next)

    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf_token": csrf_token,
            "info_message": info_message,
            "error_message": None,
            "owner_email": "",
            "next_url": safe_next,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login_submit(
    request: Request,
    owner_email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_db),
):
    """Valida credenciales e inicia sesión estableciendo cookie firmada."""
    safe_next = sanitize_next_url(next)

    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not validate_csrf_double_submit(csrf_token, cookie_csrf):
        new_csrf = generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": new_csrf,
                "info_message": None,
                "error_message": "El formulario expiró o es inválido. Por favor, intentá nuevamente.",
                "owner_email": owner_email,
                "next_url": safe_next,
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    normalized_email = owner_email.strip().lower()
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.owner_email == normalized_email)
        )
    ).scalar_one_or_none()

    if (
        tenant is None
        or not tenant.password_hash
        or not verify_password(password, tenant.password_hash)
    ):
        new_csrf = generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": new_csrf,
                "info_message": None,
                "error_message": "Correo electrónico o contraseña incorrectos.",
                "owner_email": owner_email,
                "next_url": safe_next,
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    redirect = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(redirect, tenant.id, tenant.session_version)
    # Token nuevo por sesión: el de antes del login lo pudo ver otro usuario
    # del mismo navegador.
    set_csrf_cookie(redirect, generate_csrf_token())
    return redirect


@router.post("/logout")
async def logout(request: Request):
    """Cierra la sesión eliminando la cookie."""
    await validate_csrf(request)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    delete_session_cookie(response)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return response
