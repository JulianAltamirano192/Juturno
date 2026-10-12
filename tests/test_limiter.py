import pytest
from starlette.requests import Request

from app.limiter import client_ip

TRAEFIK = "10.0.1.5"  # peer real del contenedor: el proxy en la red de Docker
CLOUDFLARE_EDGE = "162.158.1.1"


def _request(peer: str, headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
        }
    )


@pytest.mark.parametrize(
    "peer, headers, expected",
    [
        # Cliente → Cloudflare → Traefik → app: la IP real es CF-Connecting-IP.
        (
            TRAEFIK,
            {"X-Forwarded-For": CLOUDFLARE_EDGE, "CF-Connecting-IP": "200.1.2.3"},
            "200.1.2.3",
        ),
        # X-Forwarded-For falsificado: solo cuenta el último salto (lo agrega Traefik).
        (
            TRAEFIK,
            {
                "X-Forwarded-For": f"9.9.9.9, {CLOUDFLARE_EDGE}",
                "CF-Connecting-IP": "200.1.2.3",
            },
            "200.1.2.3",
        ),
        # Directo al VPS salteando Cloudflare: CF-Connecting-IP falsificado no cuenta.
        (
            TRAEFIK,
            {"X-Forwarded-For": "9.9.9.9, 190.12.34.56", "CF-Connecting-IP": "8.8.8.8"},
            "190.12.34.56",
        ),
        # Salto de Cloudflare sin CF-Connecting-IP válido: se usa el salto.
        (
            TRAEFIK,
            {"X-Forwarded-For": CLOUDFLARE_EDGE, "CF-Connecting-IP": "basura"},
            CLOUDFLARE_EDGE,
        ),
        # IPv6: la clave es el /64 (rotar direcciones dentro de él no saltea).
        (
            TRAEFIK,
            {"X-Forwarded-For": "2606:4700::1", "CF-Connecting-IP": "2800:810:1:2::5"},
            "2800:810:1:2::/64",
        ),
        (
            TRAEFIK,
            {"X-Forwarded-For": "2606:4700::1", "CF-Connecting-IP": "2800:810:1:2::9"},
            "2800:810:1:2::/64",
        ),
        # Último salto privado (ej. un túnel local): clave compartida, falla estricto.
        (TRAEFIK, {"X-Forwarded-For": "10.0.0.9"}, "10.0.0.9"),
        # Peer público (sin proxy): los headers no se miran.
        ("190.12.34.56", {"X-Forwarded-For": "9.9.9.9"}, "190.12.34.56"),
        # Local sin proxy.
        (TRAEFIK, {}, TRAEFIK),
        # Último salto inválido.
        (TRAEFIK, {"X-Forwarded-For": "basura"}, TRAEFIK),
    ],
)
def test_client_ip(peer, headers, expected):
    assert client_ip(_request(peer, headers)) == expected
