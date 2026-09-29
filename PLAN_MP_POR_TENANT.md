# Plan: Cuenta Mercado Pago por tenant (dinero directo al propietario)

> Estado: **aprobado, pendiente de implementación**. Aprobado el 2026-09-29.
> Registro de decisión: [`DECISIONS.md`](DECISIONS.md) → D-012.

## Objetivo

Que cada negocio reciba las señas de sus reservas **directamente en su propia cuenta
de Mercado Pago**, sin que dinero de terceros pase por la cuenta del operador de la
plataforma.

## Aclaración técnica clave

El checkout de Mercado Pago no se puede apuntar a un alias arbitrario: el dinero de un
pago aterriza siempre en la cuenta del vendedor cuyo token creó la preferencia. Por eso
el mecanismo correcto es **conectar la cuenta de cada negocio vía OAuth** (flujo
authorization code) y crear cada preferencia con el token de ese tenant. El alias queda
como dato informativo: se obtiene de `/users/me` al conectar y se expone en el estado
de conexión, nada más.

## Regla de dinero

| Modo | Tenant con cuenta conectada | Tenant sin cuenta conectada |
|---|---|---|
| `MP_SANDBOX=true` (desarrollo) | Se usa su token | Fallback al token de la plataforma |
| `MP_SANDBOX=false` (producción) | Se usa su token | La reserva pública se rechaza hasta que conecte su cuenta |

Conectar la cuenta MP es **requisito de alta del negocio** en producción. Con esta regla,
ningún dinero real de un tercero aterriza en la cuenta del operador.

## Flujo OAuth

```
Dueño del negocio                     Mercado Pago
     │                                      │
     │ 1. GET /mp/connect/start             │
     ├─────────────────────────────────────►│ auth.mercadopago.com/authorization
     │ 2. Autoriza su cuenta                │ (login + consentimiento)
     │◄─────────────────────────────────────┤
     │ 3. GET /mp/connect/callback?code=    │
     │    → POST /oauth/token               │
     ├─────────────────────────────────────►│
     │    access_token + refresh_token      │
     │ ◄── guardados cifrados en tenant ────┤
```

- El `code` vale 10 minutos y es de un solo uso; el `access_token` vale 180 días.
- La renovación usa `refresh_token` sin volver a requerir intervención del dueño.
- En sandbox, el canje usa `test_token=true` (cuentas de prueba de MP).
- `state` firmado (anti-CSRF) con nonce de un solo uso en Redis; el endpoint de inicio
  se autentica con la API key del tenant (`X-Tenant-API-Key`), igual que `/tenants/me`.

## Tareas

| # | Tarea | Contenido |
|---|---|---|
| 1 | Modelo, cifrado y migración | Columnas en `tenant`: `mp_user_id`, `mp_alias`, `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_token_expires_at` (todas nullable). Nuevo `app/mp_crypto.py` (`encrypt_token` / `decrypt_token` con Fernet; si falta `MP_TOKEN_KEY`, error claro). Settings nuevos en `app/config.py`. Migración `d2e3f4a5b6c7`. Sin cambio de comportamiento: todo null = se usa el token de la plataforma como hoy |
| 2 | Flujo OAuth | `GET /mp/connect/start` (API key del tenant) que arma la URL de autorización con `state` firmado y nonce en Redis. `GET /mp/connect/callback` que canjea el código, persiste los tokens cifrados y consulta `/users/me` para guardar `mp_user_id` + `mp_alias`. Helper `get_tenant_mp_token()` con renovación on-demand (vencimiento próximo o 401 → refresh → reintentar una vez). Manejo claro de código vencido o autorización cancelada |
| 3 | Pagar con el token del tenant | `create_mp_preference(..., access_token)` y `get_payment_details(..., access_token)` usan el parámetro si viene; si no, el token de la plataforma. El endpoint público resuelve `tenant → token` y aplica la regla de dinero (en producción, tenant sin conectar → rechazo con mensaje). Webhook: resolver el token vía `user_id` del payload matcheado contra `tenant.mp_user_id`; sin match → token de la plataforma. Firma, replay protection e idempotencia sin cambios |
| 4 | Estado y desconexión (API) | `GET /tenants/me/mp`: `{conectado, alias, mp_user_id, expira_en}` — nunca devuelve tokens. `DELETE /tenants/me/mp`: borra la conexión y vuelve a aplicar la regla de fallback |
| 5 | E2E en sandbox | Conectar una segunda cuenta de prueba (no la del operador), reservar y pagar: verificar que el pago aterrizó en la cuenta del segundo vendedor, que el webhook confirma la reserva con el token correcto y que la firma sigue validando |

Cada tarea se implementa una por vez, con revisión de diff, confirmación, commit, push
y deploy individuales, como en las fases anteriores.

## Prerrequisitos manuales (antes de la Tarea 5)

1. En *Tus integraciones → aplicación → Detalles*, agregar la Redirect URL:
   `https://api.juturno.com/mp/connect/callback`.
2. Obtener **Client ID** y **Client Secret** de la aplicación.
3. Generar la clave de cifrado (una sola vez, guardarla en Coolify):
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Variables de entorno nuevas

| Variable | Descripción |
|---|---|
| `MP_CLIENT_ID` | Application ID de la app de MP (OAuth) |
| `MP_CLIENT_SECRET` | Client secret de la app de MP (OAuth) |
| `MP_CONNECT_REDIRECT_URL` | URL de callback registrada en MP |
| `MP_TOKEN_KEY` | Clave Fernet para cifrar los tokens de los tenants en la DB |

## Ítem a validar experimentalmente (Tarea 5)

El webhook de MP envía `user_id` (cuenta vendedora) en el payload. El diseño del webhook
depende de ese campo para resolver el token con el que consultar el pago. Si algún tipo
de evento no lo incluyera, la contingencia es probar los tokens de los tenants conectados
(pocos) y documentarlo.

## Seguridad

- Tokens cifrados en reposo con Fernet (`MP_TOKEN_KEY`). Un volcado de la DB no expone
  credenciales de cobro de los negocios.
- Ningún endpoint devuelve tokens; el estado de conexión expone solo alias, `mp_user_id`
  y fecha de expiración.
- `state` firmado con nonce de un solo uso (anti-CSRF y anti-replay del callback).

## Fuera de alcance (documentado)

- Renovación proactiva de tokens por scheduler (solo on-demand por ahora).
- Página de conexión con botón (llega con el dashboard, Fase 2).
- Pagos manuales por alias/transferencia (flujo de validación distinto al checkout).
- Split payments / marketplace de MP.
- Rotación de la clave de cifrado.
