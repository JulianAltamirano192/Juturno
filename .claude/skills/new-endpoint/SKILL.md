---
name: new-endpoint
description: Agregar un endpoint nuevo a Juturno (API key, panel con cookie o público) siguiendo las convenciones del proyecto. Usar cuando se pida crear o cambiar un endpoint.
---
# Nuevo endpoint en Juturno

1. Leé ONBOARDING.md §3-§4 y la sección relevante de API_REFERENCE.md.
2. Definí el schema Pydantic (`Decimal` para dinero, validaciones con `Field`).
3. Elegí auth: `get_current_tenant` (API key), `get_current_tenant_from_session` (panel) o público.
   Panel con POST: validar CSRF comparando token de formulario y cookie.
4. Handler async; **toda query filtra `tenant_id`**; sin commit dentro de servicios.
5. Test primero (pytest-asyncio, fixtures `client` y `db_session`): caso feliz, 401/403,
   aislamiento entre tenants y errores de validación.
6. Si cambia el schema de DB: migración nueva (agente `migration-reviewer`).
7. `./scripts/test.sh`, `ruff check`, `mypy app/`; luego agente `code-reviewer`.
8. Actualizá API_REFERENCE.md; si es público o toca dinero, pasá `security-auditor`.
