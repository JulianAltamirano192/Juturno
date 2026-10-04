---
name: prod-readiness
description: Checklist previo al primer cliente real o a manejar dinero real en Juturno. Usar cuando se pida evaluar si está listo para producción.
---
# Checklist pre-cliente real

Verificá cada punto con evidencia (código, config o comando) y marcalo OK / FALTA / NO VERIFICABLE:

1. Secrets: historial de git sin secrets (gitleaks/trufflehog); password de Postgres de prod rotado;
   `SECRET_KEY`, `MP_TOKEN_ENCRYPTION_KEY`, `META_APP_SECRET` presentes en el contenedor de prod.
2. `MP_SANDBOX=false` en prod y sin fallback a la cuenta de plataforma.
3. E2E real: sandbox de Mercado Pago con una segunda cuenta vendedora (pago, webhook, confirmación)
   y sandbox de WhatsApp Business (plantillas Utility aprobadas).
4. Rate limiting en `/login`, `/register`, `/public/bookings`; límites por IP y por teléfono.
5. Webhook MP valida monto y tenant; outbox con commit por evento y reintentos acotados.
6. Backups: cron activo, copia externa, restore probado, `MP_TOKEN_ENCRYPTION_KEY` guardada aparte.
7. Monitoreo externo de `/health` y alerta ante `notification_outbox` en `failed`.
8. CI: tests (y ruff/mypy) obligatorios con branch protection; deploy tras CI verde.
9. Panel: conexión de MP desde la UI, cambio/recupero de contraseña, CSRF completo.
10. Legal: términos, política de privacidad y consentimiento para WhatsApp.

Entregá una tabla con el estado y los 3 bloqueantes principales.
