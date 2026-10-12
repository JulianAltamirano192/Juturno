# Roadmap Juturno: hallazgos abiertos + ideas

## Contexto

Hay dos listas sueltas: los 12 hallazgos del code-review (CLAUDE.md) y la lista local de ideas de Julián.
Este plan las junta en un solo orden. La regla es **primero lo que rompe o es inseguro, después lo que suma**.
Cada ítem es una tarea con su propia rama/PR, test primero, y "hecho" = tests + ruff + mypy.

Ya verifiqué contra el código los hallazgos 2, 3, 4, 5, 7, 8 y 9 (se confirman). En el 11 lo del float es parcial: `Payment.amount` ya es `Decimal`; solo `create_mp_preference` usa float.

### Ideas que en realidad son bugs (confirmado en código)

- **Seña > precio**: el form de servicios (`app/routers/panel.py:62-82`) no compara la seña con el precio. Va junto con el hallazgo 11 (Infinity y overflow de `Numeric(10,2)`).
- **Servicio sin seña**: hoy `deposit_amount = None` significa 30% del precio (`app/services.py:5-9`). Con seña 0 igual se llama a MP con monto 0 (`app/routers/public.py:296-323`). No existe un camino "sin seña", y un negocio sin MP no puede tomar ningún turno.
- **Logout a `#logout`**: no existe ese anchor en el repo. Las causas probables son el 403 JSON de CSRF cuando otra pestaña rotó la cookie (hallazgo 9) o un `GET /logout` que da 405. Puede que arreglando el 9 desaparezca; hay que reproducirlo primero.
- **Nombre de cuenta MP roto**: es `tenant.mp_alias` (el nickname de MP) en `app/templates/settings.html:32`. Se arregla con CSS/truncado.

### Correcciones a ideas

- **Panel admin con contraseñas hasheadas**: no conviene. No aportan nada y convierten al panel en un blanco para crackeo offline. Mostrar en su lugar email, fecha de alta, estado, MP conectado y cantidad de turnos.
- **WhatsApp por negocio**: hoy hay un solo número de plataforma (`app/config.py:9-10`). Hacerlo por negocio exige Meta Embedded Signup (Tech Provider + verificación de negocio). Es lo más pesado; va al final.

## Fases (en orden)

### Fase 0 — Housekeeping (sin código)
- ~~Branch protection en `main`~~ — **Hecho** (2026-10-09, aplicada por Julián; PR #27 lo documenta): PR + check `test` obligatorios, también para admins.
- ~~Borrar ramas mergeadas~~ — **Hecho**.
- `--forwarded-allow-ips` → **pasó a tarea de código 2.11** (no es solo ops: hay Cloudflare delante de Traefik).
- ~~Ver la DB~~ — **Hecho** (local y producción).
- ~~Mantenimiento del VPS~~ — **Hecho** (2026-10-10).

### Fase 1 — Seguridad y bugs chicos (hallazgos)
1. ~~#10 verify token de Meta con `hmac.compare_digest` (`app/webhooks.py`).~~ **Hecho (PR #16)**.
2. ~~#7 try/except por tenant en `process_mp_token_refresh` (`app/scheduler.py:298-307`).~~ **Hecho (PR #17)**.
2b. Sentry captura variables locales (`include_local_variables=True` por defecto, scrubber no recursivo): un error en el refresh MP manda `client_secret` y refresh tokens en texto plano. Fix en `sentry_sdk.init` (`app/main.py:39-51`). **Hecho (PR #20)**, incluye API key, bodies y query string.
2c. **Hecho (PR #26)** (Baja) Restos de PII hacia Sentry: breadcrumbs del access log de uvicorn con query string, `app/whatsapp_service.py:74` loguea el body 4xx de Meta, `app/webhooks.py:96` loguea `{e}`.
3. #1 timezone. **Hecho (PR #21)**: default Buenos Aires + migración; sin selector (producto solo Argentina).
4. Validación de montos. **Hecho (PR #22)**.
5. #9 CSRF. **Hecho (PR #23)**, incluye cookie de 14 días (causa probable del logout roto).
6. #5 + #8. **Hecho (PR #24)**: link de pago vence con la reserva (tope 48 h), guard compartido `has_payable_mp_payment` (D-031).
7. **Hecho (PR #25)** Exigir `META_VERIFY_TOKEN` en producción (`model_post_init`, `app/config.py`): vacío hace fallar el handshake de Meta en silencio.

### Fase 2 — Integridad de turnos
7. ~~#2 validar `start_time` en el alta pública y por API: futuro, en la grilla de `compute_available_slots`, servicio y staff activos.~~ **Hecho (PR #28)**: `is_bookable_start` → 422; servicio/staff inactivo → 404.
8. ~~#3 + #4 acciones de agenda con `FOR UPDATE`, capturar `IntegrityError` → 409, y que confirmar desde el panel encole la confirmación.~~ **Hecho (PR #30)**: `enqueue_confirmation` en toda transición a `confirmed` (confirmar desde el panel manda WhatsApp aunque no haya seña paga: decisión de Julián).
9. ~~#6 recordatorios: ventana "pendientes y no enviados" en lugar de la ventana fija de 5 min.~~ **Hecho (PR #31)**: todo `confirmed` sin recordatorio que empiece dentro de 24 h, con `SKIP LOCKED`.
   (Fuera de serie: **PR #29** fijó `ruff==0.16.10`; ruff 0.17 rompía el CI.)
10. ~~#11 resto: reembolso/contracargo, booking inexistente en el webhook, `Decimal` en `create_mp_preference`.~~ **Hecho (rama `fix/mp-webhook-refunds-and-orphans`)**: reembolso total o contracargo cancela el turno que no empezó (decisión de Julián); `BOOKING_NOT_FOUND` sin insertar el pago; un `approved` viejo no pisa un reembolso.
11. Rate limit por IP real del cliente detrás del proxy (Cloudflare + Traefik). Paso 1: observar en prod qué headers llegan (`X-Forwarded-For`, `CF-Connecting-IP`) y la config de Traefik en Coolify. Paso 2: `key_func` con la IP real (Cloudflare) solo si el request vino por el proxy confiable.

#### Hallazgos nuevos de las revisiones del 2026-10-10 (anteriores al código revisado; sin fase asignada)
- Tope de anticipación al reservar: hoy se puede crear un `pending` para el año 9999 (decisión de producto: ¿90 días? ¿configurable?).
- `GET /public/available-slots` ofrece horarios de servicios/staff inactivos (la reserva después da 404).
- `process_outbox` mantiene el lock de la fila mientras llama a Meta (hasta ~33 s): un cancelar concurrente espera con el booking bloqueado y después pisa `sent` con `cancelled`.
- Orden de locks invertido: reconciliación Booking→Payment vs webhook Payment→Booking (deadlock posible; ambos reintentan).
- Validar `client_name` en reservas públicas (largo, sin URLs): llega al template de WhatsApp.

#### Pendientes de la 2.10 (revisión del 2026-10-12; para más adelante)
- Avisar al dueño (ayuda o panel) que devolver la seña desde MP cancela el turno solo; si quiere mantenerlo (ej. pagó en efectivo), lo tiene que volver a cargar.
- El chequeo de "otro pago aprobado cubre el turno" mira solo la DB, no MP: si se perdió el webhook del pago duplicado, el reembolso del otro cancela el turno igual. Arreglo posible: consultar `search_approved_payment_ids` antes de cancelar.
- Un contracargo que el vendedor gana en disputa queda registrado como `charged_back` (se ignora el `approved` posterior). El turno ya estaba cancelado; solo afecta el registro.
- Después de restaurar un backup, la secuencia de bookings puede reusar ids: un reembolso tardío de `booking-N` podría pegarle a otro turno del mismo negocio (inferido, sin probar). Ver RUNBOOK.

### Fase 3 — Quick wins del panel (ideas)
11. Seña opcional por servicio: un checkbox "cobrar seña". Sin seña, el turno se confirma directo sin MP y se encola la confirmación. MP solo es obligatorio si algún servicio cobra seña.
12. Horarios: elegir varios días en un solo submit.
13. Arreglar cómo se muestra el nombre de la cuenta MP.
13b. Plazos configurables por el negocio en Ajustes: plazo de seña (`deposit_expiration_minutes`, hoy solo por API) y tope del link de pago (48 h fijo). Ver deuda en D-031.
26. Turno manual desde el panel (cliente sin turno o reserva por teléfono): nace `confirmed` sin seña, vía `transition_booking_status` + `enqueue_confirmation`; captura `IntegrityError` → 409.
27. Aviso al dueño cuando entra un turno confirmado (WhatsApp o email), reusando la outbox.
28. Notas internas por turno + exportar turnos a CSV.
29. QR descargable de `/t/{slug}` (generado en el navegador, sin dependencia nueva en el backend) + vista semanal de la agenda.
38. Agenda: seña cobrada y saldo a cobrar por turno (`price_at_booking - deposit_at_booking`) + total del día.
39. Aviso destacado del negocio en la página pública (texto corto, editable en Ajustes, escapado al renderizar).
40. Panel instalable (PWA): `manifest` + ícono; sin service worker por ahora.

### Fase 4 — Perfil del negocio
14. Rubro al registrarse: una lista genérica + "otro" con texto libre.
15. Localidad, redes sociales (Instagram/Facebook) y logo (decidir dónde guardar la imagen: volumen local o S3).
41. Color de marca para la página pública (hex validado → variable CSS).

### Fase 5 — Experiencia del cliente y datos
30. Horarios por profesional en el panel (`BusinessHours.staff_id` ya existe; hoy el panel solo maneja `staff_id IS NULL`). Prerrequisito de 16.
31. Días bloqueados / feriados / vacaciones por fecha, del negocio o de un profesional. Toca `compute_available_slots`.
32. Tiempo libre entre turnos por servicio, **opt-in**: el negocio lo activa por servicio y elige los minutos; por defecto no hay tiempo libre. Toca slots y el `EXCLUDE` de `booking`.
16. El cliente elige el profesional. Antes hace falta UI de horarios por staff, porque hoy el panel solo maneja horarios del negocio.
17. Historial por teléfono del cliente. Primero una vista agrupando `booking` por `client_phone`; la tabla `Client` solo si hace falta.
18. Estadísticas mensuales: completados, no presentados, servicios top, días fuertes/débiles (agregaciones SQL sobre `booking`).
42. Preguntas personalizadas por servicio en el formulario de reserva (respuestas guardadas en el turno).
43. Varios servicios en un mismo turno: duración y precio sumados, una sola seña. `Booking` tiene un solo `service_id` → tabla intermedia.
44. Mover un turno desde el panel con aviso por WhatsApp al cliente. Comparte validación con 33.
45. Seña obligatoria según historial: un `no_show` previo del mismo teléfono obliga a pagar seña. Depende de 11 y 17.

### Fase 5b — Autogestión del cliente
33. Cancelar/reprogramar desde un link firmado y con vencimiento que llega en el WhatsApp de confirmación. Endpoint público: test primero + `security-auditor`.
34. "Agregar a mi calendario": `.ics` + link de Google Calendar en la confirmación y en el WhatsApp.
35. Lista de espera: si el día está lleno, el cliente deja su número; al cancelarse un turno se avisa al primero.

### Fase 6 — Ciclo de vida de la cuenta + admin
19. `Tenant.is_active` y baja de la cuenta (desconectar MP, conservar historial).
20. Cambio y recupero de contraseña, verificación de email; #12 `session_version` (logout global). Junto con esto: atar el token CSRF a la sesión (`HMAC(secret, tenant_id|session_version)`) y/o cookie `__Host-csrf_token` en prod, contra cookie tossing desde subdominios.
21. Panel admin de solo lectura para Julián.

### Fase 7 — Rediseño del panel
22. Llevar el panel al nivel visual del landing (skill `impeccable`). Va después de las fases 3-5 para no rediseñar dos veces las pantallas nuevas.

### Fase 8 — Marketplace y monetización
23. Buscador público por rubro + localidad (depende de la fase 4).
24. Plan premium: suscripción cobrada a la cuenta de la plataforma, con prioridad en el buscador. Requiere definir precio y modelo antes de codear.

### Fase 9 — WhatsApp por negocio
25. Meta Embedded Signup y credenciales por tenant en el outbox.
36. Botones "Confirmo / No voy" en el recordatorio de 24 h (mensajes interactivos; hoy el webhook solo loggea los entrantes). Comparte la lógica de cancelación con 33.
37. Pedido de reseña de Google después de un turno `completed`; el negocio lo puede desactivar. 36 y 37 requieren plantillas aprobadas por Meta.
46. Recordatorio de regreso: intervalo opcional por servicio; un job del scheduler manda un WhatsApp para volver a reservar. Requiere plantilla de Meta.

### Sin fase (en duda)
47. Galería de trabajos en la página pública. Comparte la decisión de almacenamiento de imágenes con el logo (15).

## Cómo se ejecuta cada tarea

Rama → test que falla → fix → `./scripts/test.sh` + `ruff check app/ tests/` + `mypy app/` → agente `code-reviewer` (y `security-auditor` si toca auth, webhooks o pagos) → commit/PR cuando Julián lo pida → `docs-keeper` si cambió comportamiento. Al cerrar cada ítem se tacha en CLAUDE.md.

## Verificación del plan

Las fases 1-2 cierran los 12 hallazgos. Se chequea revisando que la sección "Hallazgos abiertos" de CLAUDE.md quede vacía.

Siguiente tarea concreta: **Fase 2.11 (rate limit por IP real)**.
