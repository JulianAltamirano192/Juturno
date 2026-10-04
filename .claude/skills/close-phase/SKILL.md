---
name: close-phase
description: Cierre de una fase o bloque de tareas de Juturno: verificación, documentación y registro de decisiones. Usar al terminar un bloque.
---
# Cierre de fase

1. `./scripts/test.sh` completo, `ruff check app/ tests/`, `mypy app/`; reportá el conteo real de tests.
2. Agente `code-reviewer` sobre el diff acumulado.
3. Agente `docs-keeper`: README (tests, estructura, env vars), ARCHITECTURE, API_REFERENCE, RUNBOOK;
   nuevas decisiones en DECISIONS.md (siguiente D-0XX) y roadmap de deuda actualizado.
4. Verificá que no queden env vars nuevas fuera de `.env.example` y de DEPLOYMENT.md.
5. Resumen para Julián: qué se hizo, deuda nueva y próximo paso. No hagas push ni deploy.
