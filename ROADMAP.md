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
- **Nombre de cuenta MP roto**: es `tenant.mp_alias` (el nickname de MP) en `app/templates/settings.html:32`. **Hecho (rama `fix/mp-account-display-name`)**: se guarda el nombre del titular y el texto largo se corta dentro de la tarjeta.

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
11. ~~Rate limit por IP real del cliente detrás del proxy (Cloudflare + Traefik).~~ **Hecho (rama `fix/rate-limit-real-ip`, D-033)**: `client_ip` toma `CF-Connecting-IP` solo si el último salto de `X-Forwarded-For` (lo agrega Traefik) es de Cloudflare; IPv6 por /64. No hizo falta observar prod antes: se verifica después del deploy (ver RUNBOOK).

### Fase 2b — Hallazgos sin fase (aprobada 2026-10-12; una rama por ítem, en este orden)
0. ~~Errores de formularios del navegador como página HTML (CSRF vencido → confirmar logout / recargar; 429 → "Demasiados intentos") en vez de JSON que el celular descarga.~~ **Hecho (rama `fix/html-form-errors`)**.
1. ~~`GET /public/available-slots` con servicio/staff inactivo → 404 (igual que la reserva).~~ **Hecho (rama `fix/slots-inactive-service-staff`)**: también `GET /bookings/available-slots` y staff de otro tenant.
2. ~~Validar `client_name` en reservas públicas.~~ **Hecho (rama `fix/validate-client-name`)**: 2-60 caracteres, sin links, espacios/saltos colapsados; también por API.
3. ~~`process_outbox`: no mantener el lock de la fila durante la llamada a Meta.~~ **Hecho (rama `fix/outbox-cancel-lock`)**: cancelar saltea con `SKIP LOCKED` el evento en vuelo (no espera ni pisa `sent`) y el worker no manda si el turno ya no está `confirmed`.
4. ~~Orden de locks Booking→Payment en webhook y reconciliación.~~ **Hecho (rama `fix/mp-lock-order`)**: el webhook bloquea el booking antes que el Payment, igual que la reconciliación.
5. ~~Tope de anticipación al reservar.~~ **Hecho (rama `feat/booking-horizon`, D-034)**: cada negocio elige 15/30/60/90 días (default 90) desde `/panel/settings`; más allá no se ofrecen slots y la reserva da 422.

#### Hallazgos nuevos de las revisiones del 2026-10-10 (anteriores al código revisado; detalle; ver Fase 2b)
- ~~Tope de anticipación al reservar: hoy se puede crear un `pending` para el año 9999 (decisión de producto: ¿90 días? ¿configurable?).~~ Ver 2b.5.
- ~~`GET /public/available-slots` ofrece horarios de servicios/staff inactivos (la reserva después da 404).~~ Ver 2b.1.
- `process_outbox` mantiene el lock de la fila mientras llama a Meta (hasta ~33 s): un cancelar concurrente espera con el booking bloqueado y después pisa `sent` con `cancelled`.
- ~~Orden de locks invertido: reconciliación Booking→Payment vs webhook Payment→Booking (deadlock posible; ambos reintentan).~~ Ver 2b.4.
- Validar `client_name` en reservas públicas (largo, sin URLs): llega al template de WhatsApp.

#### Pendientes de la 2b.4 (revisión del 2026-10-12; para más adelante)
- La rama de actualización del webhook no compara `payment.booking_id` con el booking del `external_reference` (hoy no puede diferir desde el código, solo editando la base a mano).

#### Pendientes de la 2b.0 (revisión del 2026-10-12; endurecimiento, para más adelante)
- ~~Panel sin `X-Frame-Options: DENY` / `Content-Security-Policy: frame-ancestors 'none'`.~~ **Hecho (rama `fix/clickjacking-headers`, D-040)**: middleware con headers de seguridad en toda respuesta.
- Rechazar `CORS_ORIGINS="*"` en el validador de producción.
- `GET /public/available-slots` sin rate limit (solo lo tiene `POST /public/bookings`).

#### Pendientes del 26 (turno manual; para el próximo PR)
- ~~Reservar el prefijo `panel-` de `idempotency_key`: hoy el cliente público o de la API puede usarlo. Si el negocio cancela o confirma desde la agenda esas reservas dentro de las 24 h, pasan a actor `owner` y gastan el cupo diario de 50 altas manuales (peor caso: 24 h sin poder cargar turnos a mano). Arreglo: validador que rechace claves `panel-*` en `BookingCreate` y en el schema público.~~ **Hecho (rama `fix/reserve-panel-idempotency-prefix`)**: `BookingCreate` rechaza claves `panel-*` con 422; el panel y el validador comparten `MANUAL_BOOKING_KEY_PREFIX`.

#### Pendientes del 27 (aviso al dueño)
- **Obligatorio antes de conectar el número real de producción**: verificar el número del dueño por código (OTP) antes de guardarlo. Hoy el número no se verifica y el tope de 30 avisos por negocio en 24 h es solo una mitigación parcial.
- Plantilla `owner_new_booking` aprobada en Meta antes del deploy.

#### Pendientes del 11 (seña opcional; para más adelante)
- Límite diario por negocio o verificación de teléfono para turnos sin seña: el tope por teléfono no frena a quien rota números.
- `POST /bookings` (API) no auto-confirma servicios sin seña: sigue creando `pending`.
- Antes de desplegar: revisar cuántos servicios ya tienen seña 0; van a empezar a confirmar sin pago.

#### Pendientes del 16 (el cliente elige profesional; para más adelante)
- Tope de profesionales activos por negocio: "Sin preferencia" hace ~7 consultas por profesional, así que un negocio con muchos encarece cada consulta de horarios (hoy solo lo frena el rate limit de 60/minuto por IP).
- Los ids de staff/tenant/service no tienen tope int4 en los schemas (ya existía).
- Carrera aceptada entre un turno sin profesional y uno asignado creados a la vez (el `EXCLUDE` no los compara); la solución de fondo es asignar siempre un profesional o un lock por tenant.

#### Pendientes de la 2.11 (revisión del 2026-10-12; para más adelante)
- ~~Verificar después del deploy: más de 10 logins fallidos desde el celular (datos móviles) dan 429 ahí y otra red sigue entrando.~~ Verificado en prod el 2026-10-12.
- Login limitado solo por IP: sumar un límite por cuenta (email) contra credential stuffing distribuido.
- `client_ip` confía en cualquier peer privado: acotarlo a la red de Traefik si algún día se publica el puerto o se suman contenedores a la red.

#### Pendientes de la 2.10 (revisión del 2026-10-12; para más adelante)
- Avisar al dueño (ayuda o panel) que devolver la seña desde MP cancela el turno solo; si quiere mantenerlo (ej. pagó en efectivo), lo tiene que volver a cargar.
- El chequeo de "otro pago aprobado cubre el turno" mira solo la DB, no MP: si se perdió el webhook del pago duplicado, el reembolso del otro cancela el turno igual. Arreglo posible: consultar `search_approved_payment_ids` antes de cancelar.
- Un contracargo que el vendedor gana en disputa queda registrado como `charged_back` (se ignora el `approved` posterior). El turno ya estaba cancelado; solo afecta el registro.
- Reembolso parcial: MP deja el pago en `approved` (solo cambia `status_detail`) y el webhook no guarda `transaction_amount_refunded`. La agenda (ítem 38) muestra la seña completa como cobrada y un saldo menor al real. Arreglo: guardar el monto reembolsado en `Payment` (migración) y restarlo. Inferido de la documentación de MP, sin probar.
- Después de restaurar un backup, la secuencia de bookings puede reusar ids: un reembolso tardío de `booking-N` podría pegarle a otro turno del mismo negocio (inferido, sin probar). Ver RUNBOOK.

### Fase 3 — Quick wins del panel (ideas)
11. ~~Seña opcional por servicio: un checkbox "cobrar seña". Sin seña, el turno se confirma directo sin MP y se encola la confirmación. MP solo es obligatorio si algún servicio cobra seña.~~ **Hecho (rama `feat/optional-deposit`, D-035)**: sin seña = `deposit_amount = 0`; la reserva pública nace `confirmed`, con tope de 3 turnos futuros sin seña por teléfono y negocio.
12. ~~Horarios: elegir varios días en un solo submit.~~ **Hecho (rama `feat/multi-day-hours`)**: casillas de días en "Nuevo horario"; una fila por día, todo o nada si algún día se solapa. Editar sigue siendo de a un día.
13. ~~Arreglar cómo se muestra el nombre de la cuenta MP.~~ **Hecho (rama `fix/mp-account-display-name`)**: al conectar se guarda `first_name last_name` de `/users/me` (si faltan, el nickname autogenerado de MP) y el nombre largo hace salto de línea en celular. Las cuentas ya conectadas muestran el nickname hasta que reconecten.
13b. ~~Plazos configurables por el negocio en Ajustes.~~ **Hecho (rama `feat/deposit-expiration-setting`)**: desplegable "Plazo para pagar la seña" (15 min a 24 h, sin "sin vencimiento"). El tope de 48 h del link queda como constante técnica: con plazos de hasta 24 h nunca se activa.
26. ~~Turno manual desde el panel (cliente sin turno o reserva por teléfono).~~ **Hecho (rama `feat/manual-booking`)**: botón "Nuevo turno" en la agenda; hora libre (sin grilla, horario ni horizonte) pero siempre futura; WhatsApp obligatorio. Nace `confirmed` sin seña vía `transition_booking_status` (encola la confirmación); superposición → 409. Tope de 50 altas manuales por negocio en 24 h (anti-spam de WhatsApp, pedido por el `security-auditor`).
27. ~~Aviso al dueño cuando entra un turno confirmado (WhatsApp o email), reusando la outbox.~~ **Hecho (rama `feat/owner-booking-alert`)**: WhatsApp al número que el negocio carga en el registro o en Ajustes (vacío desactiva los avisos); no se avisa cuando confirma el propio dueño. Tope de 30 avisos por negocio en 24 h (anti-spam, pedido por el `security-auditor`). Email queda afuera. Ver D-036.
28. ~~Notas internas por turno + exportar turnos a CSV.~~ **Hecho (rama `feat/booking-notes-csv`)**: nota privada de hasta 500 caracteres por turno, editable en cualquier estado desde la agenda, y descarga en CSV de los turnos de un rango de fechas (hasta 366 días) pensada para Excel en es-AR.
29. ~~QR descargable de `/t/{slug}` (generado en el navegador, sin dependencia nueva en el backend) + vista semanal de la agenda.~~ **Hecho (rama `feat/qr-weekly-agenda`)**: el Inicio muestra el QR del link de reservas con botón "Descargar QR" (PNG con la URL debajo); la agenda tiene vista semanal de solo lectura (`?view=week`), lunes a domingo, con enlace a cada día.
38. ~~Agenda: seña cobrada y saldo a cobrar por turno + resumen del día y pestaña Estadísticas.~~ **Hecho (rama `feat/agenda-deposit-balance`)**: la vista diaria muestra por turno "Seña cobrada" (suma de pagos aprobados, no el snapshot `deposit_at_booking`) y "A cobrar" (`price_at_booking` menos lo cobrado, mínimo 0; solo en confirmados y completados), más un "Resumen del día" con señas cobradas, cantidad de cancelados / no se presentaron (los vencidos no cuentan) y total a cobrar (seña + resto en el local, de los turnos confirmados y completados). Suma la pestaña "Estadísticas" (`/panel/estadisticas`) con los mismos tres datos por semana o mes y navegación al período anterior y siguiente. La vista semanal de la agenda y el CSV no cambian. Avanza en parte el ítem 18 de la Fase 5 (estadísticas mensuales): falta completados, servicios top y días fuertes/débiles.
39. ~~Aviso destacado del negocio en la página pública (texto corto, editable en Ajustes, escapado al renderizar).~~ **Hecho (rama `feat/public-notice`)**: sección "Aviso en tu página de reservas" en Ajustes (hasta 280 caracteres, vacío lo borra) y se muestra debajo del header de `/t/{slug}`, escapado y con los saltos de línea respetados. Requiere migración `82b61531a397` (`Tenant.public_notice`): correr `alembic upgrade head` en el deploy.
40. ~~Panel instalable (PWA): `manifest` + ícono; sin service worker por ahora.~~ **Hecho (rama `feat/pwa-manifest`)**: `app/static/manifest.webmanifest` (abre en `/dashboard`, scope `/` para que login y registro queden dentro de la app, modo standalone) con íconos de 192 y 512 px y uno "maskable" para Android, enlazado desde `base.html`. Sin service worker: sin conexión el panel no abre.

### Fase 4 — Perfil del negocio
14. ~~Rubro al registrarse: una lista genérica + "otro" con texto libre.~~ **Hecho (rama `feat/business-category`)**: `POST /register` exige `business_category` (12 rubros genéricos; con "otro", `business_category_other` de 2 a 60 caracteres) y Ajustes tiene la sección "Rubro del negocio" (`POST /panel/settings/category`). Los negocios anteriores quedan con rubro `NULL`. Todavía no se muestra en la página pública ni en la API: lo usará el buscador del ítem 23. Requiere migración `dcb3cce62e10`: correr `alembic upgrade head` en el deploy.
15. ~~Localidad, redes sociales (Instagram/Facebook) y logo (decidir dónde guardar la imagen: volumen local o S3).~~ **Hecho (rama `feat/business-profile`, D-037)**: sección "Perfil de tu negocio" en Ajustes (`POST /panel/settings/profile`: localidad hasta 60 caracteres, usuario de Instagram y de Facebook; se guarda solo el usuario y la plantilla arma el link) y logo (`POST /panel/settings/logo`, `/logo/remove`, `GET /t/{slug}/logo`) guardado en Postgres, re-codificado a WebP de 512 px con Pillow. `/t/{slug}` muestra el logo en lugar de la inicial y una línea con localidad y redes. Todavía no se expone en la API JSON; la localidad la usará el buscador del ítem 23. Requiere migración `a12e6a5a2a92` (`Tenant.locality/instagram/facebook` y tabla `tenant_logo`): correr `alembic upgrade head` en el deploy. Dependencia nueva: `pillow>=12.2.0`. La galería (ítem 47) ya no comparte decisión de almacenamiento resuelta: hay que replantearla (D-037).
41. ~~Color de marca para la página pública (hex validado → variable CSS).~~ **Hecho (rama `feat/brand-color`, D-038)**: sección "Color de tu página" en Ajustes (`POST /panel/settings/brand-color`) con un selector de color y el botón "Usar el color de Juturno" (solo aparece si hay color guardado). El hex se valida (`#RRGGBB`, contraste WCAG >= 3:1 contra el fondo) y de él se derivan las variables CSS del tema en `<style id="brand">` de `/t/{slug}`; vacío o el cian por defecto dejan `NULL` (tema de Juturno, página idéntica a la anterior). Requiere migración `2cf8d49114fa` (`Tenant.brand_color` con CHECK `ck_tenant_brand_color`): correr `alembic upgrade head` en el deploy. Todavía no se expone en la API JSON.

### Fase 5 — Experiencia del cliente y datos
30. ~~Horarios por profesional en el panel (`BusinessHours.staff_id` ya existe; hoy el panel solo maneja `staff_id IS NULL`). Prerrequisito de 16.~~ **Hecho (rama `feat/staff-hours`, D-041)**: `/panel/horarios?staff=ID` maneja los horarios de cada profesional (selector "Negocio" + profesionales y link "Horarios" en Personal). Un profesional con al menos una franja reemplaza por completo el horario del negocio y sus días sin franja quedan cerrados. Pueden tener horario partido, como el negocio. Requiere migración `9a9d04b9ff5e` (borra el UNIQUE `uq_business_hours_tenant_staff_day`): correr `alembic upgrade head` en el deploy.
31. ~~Días bloqueados / feriados / vacaciones por fecha, del negocio o de un profesional. Toca `compute_available_slots`.~~ **Hecho (rama `feat/blocked-days`, D-043)**: tabla `blocked_period` (negocio con `staff_id` NULL, o un profesional; rango inclusive con motivo opcional). `resolve_day_windows` devuelve `[]` en un día bloqueado, así que no hay slots y las reservas dan 422. Sección "Días bloqueados" en `/panel/horarios` (y `?staff=ID`). Si hay turnos pendientes o confirmados en el rango, el bloqueo se rechaza y se listan hasta 5. La reserva manual del panel no respeta los bloqueos a propósito. Requiere migración `f4e2a9d38afb` (tabla nueva): correr `alembic upgrade head` en el deploy.
32. ~~Tiempo libre entre turnos por servicio, **opt-in**: el negocio lo activa por servicio y elige los minutos; por defecto no hay tiempo libre. Toca slots y el `EXCLUDE` de `booking`.~~ **Hecho (rama `feat/service-buffer`, D-044)**: columna `service.buffer_minutes` (0, 5, 10, 15, 20, 30, 45 o 60; default 0, CHECK `ck_service_buffer_minutes`). El tiempo libre se guarda dentro de `booking.end_time`, así que el `EXCLUDE` no cambia; `compute_available_slots` corre hacia atrás el inicio de los turnos existentes según el tiempo libre del servicio nuevo. Select "Tiempo libre después del turno" en el formulario de servicio y "+N min libres" en la lista. Requiere migración `e7a8de2da19a` (columna nueva con default 0): correr `alembic upgrade head` en el deploy.
16. ~~El cliente elige el profesional. El prerrequisito (horarios por profesional en el panel, ítem 30) ya está hecho; `compute_available_slots` ya recibe `staff_id`.~~ **Hecho (rama `feat/client-picks-staff`, D-045)**: `/t/{slug}` suma el paso "¿Con quién querés atenderte?" (Sin preferencia + cada profesional) solo con 2 o más profesionales activos; con 0 o 1 todo sigue igual. `GET /public/available-slots` sin `staff_id` devuelve la unión de los horarios de cada profesional (rate limit 60/minuto por IP) y `POST /public/bookings` asigna automáticamente al de menos turnos ese día (409 si ninguno está libre, 422 si ninguno puede por horario o día bloqueado); la respuesta suma `staff_name`. Regla nueva: un turno sin profesional ocupa a todos (`has_overlapping_booking` en `POST /public/bookings` y `POST /bookings`; `compute_available_slots` con `staff_id` también los cuenta). Sin migración. Pendiente: tope de profesionales activos por negocio (ver "Pendientes del 16").
17. ~~Historial por teléfono del cliente. Primero una vista agrupando `booking` por `client_phone`; la tabla `Client` solo si hace falta.~~ **Hecho (rama `feat/client-history`, D-046)**: `GET /panel/clientes?q=` lista un cliente por teléfono (agrupa `booking` por `client_phone`, el del turno más reciente primero) con el nombre del último turno, cantidad de turnos, ausencias y fecha del último; excluye los `expired`, tope de 50 con aviso para usar el buscador (`q`: nombre o dígitos del teléfono, hasta 60 caracteres). `GET /panel/clientes/{phone}` muestra todos sus turnos (tope 200) con fecha y hora local (link a la agenda de ese día), servicio, profesional, precio, estado y nota interna, más contadores de completados, no se presentó y cancelados (404 si no hay turnos o el teléfono no es solo dígitos). En la agenda diaria el nombre del cliente linkea a su historial. "Clientes" es pestaña de la barra lateral de escritorio; en celular vive dentro de Agenda (pestaña activa + "Clientes" en el selector Día | Semana), porque 8 pestañas no entraban a 320 px. Sin tabla `Client`, sin migración y sin cambios en la API JSON.
18. ~~Estadísticas mensuales: completados, no presentados, servicios top, días fuertes/débiles (agregaciones SQL sobre `booking`).~~ **Hecho (rama `feat/monthly-stats`, D-047)**: `/panel/estadisticas` (semana o mes) suma gráficos en SVG/CSS armados en el servidor, sin JS: barra apilada de confirmados, completados, no se presentaron y cancelados; hasta 5 servicios más pedidos; turnos por día de la semana con "Más movido" y "Más tranquilo" (el de menos turnos entre los días con alguno; en un empate se marcan todos los empatados); y, en el mes, columnas por día. Se agrega en Python sobre las mismas filas del resumen de plata (sin queries nuevas). Servicios y días cuentan solo confirmados y completados. Sin migración.
42. ~~Preguntas personalizadas por servicio en el formulario de reserva (respuestas guardadas en el turno).~~ **Hecho (rama `feat/service-questions`, D-048)**: hasta 3 preguntas de texto por servicio (120 caracteres, cada una obligatoria u opcional) en el formulario de servicio. La página pública las muestra en "Tus datos" y `POST /public/bookings` exige las obligatorias; `POST /bookings` (API key) acepta `answers` sin exigirlas. Las respuestas (hasta 200 caracteres) se guardan en el turno como copia de pregunta y respuesta y se ven en la agenda diaria, el historial del cliente y la columna "Respuestas" del CSV. Requiere migración `46fd57b22b48` (columnas nuevas `service.questions` y `booking.answers`): correr `alembic upgrade head` en el deploy.
43. Varios servicios en un mismo turno: duración y precio sumados, una sola seña. `Booking` tiene un solo `service_id` → tabla intermedia. **En curso, en 3 PRs encadenados**: PR 1 (rama `feat/multi-service-views`): tabla `booking_extra_service` (migración `e2ca7d155205`) y el combo "A + B" en las vistas del dueño y el WhatsApp de aviso. Faltan la creación de turnos con varios servicios (PR 2) y la página pública (PR 3).
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
47. Galería de trabajos en la página pública. El logo (15) se guardó en Postgres (D-037); replantear si la galería, con muchas más imágenes, aguanta el mismo criterio o necesita S3.

## Cómo se ejecuta cada tarea

Rama → test que falla → fix → `./scripts/test.sh` + `ruff check app/ tests/` + `mypy app/` → agente `code-reviewer` (y `security-auditor` si toca auth, webhooks o pagos) → commit/PR cuando Julián lo pida → `docs-keeper` si cambió comportamiento. Al cerrar cada ítem se tacha en CLAUDE.md.

## Verificación del plan

Las fases 1-2 cierran los 12 hallazgos. Se chequea revisando que la sección "Hallazgos abiertos" de CLAUDE.md quede vacía.

Siguiente tarea concreta: **Fase 5, ítem 43** (varios servicios en un mismo turno). Los ítems 16, 17, 18, 30, 31, 32 y 42 de la Fase 5 están hechos. La Fase 4 está completa (ítems 14, 15 y 41). La Fase 3 está completa (ítems 11, 12, 13, 13b, 26, 27, 28, 29, 38, 39 y 40); quedan los pendientes listados arriba.
