from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings

# https://www.cloudflare.com/ips/ (2026-10-12).
# ponytail: lista fija; si Cloudflare suma rangos, esos clientes caen en el
# límite por IP del edge (más estricto, no se saltea). Actualizar a mano.
CLOUDFLARE_NETWORKS = tuple(
    ip_network(cidr)
    for cidr in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _key(ip: IPv4Address | IPv6Address) -> str:
    # Cualquier VPS tiene un /64 de IPv6: por dirección, rotarla saltea el límite.
    if ip.version == 6:
        return str(ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip(request: Request) -> str:
    """
    IP real del cliente para el rate limit (Cliente → Cloudflare → Traefik → app).

    Traefik agrega al final de X-Forwarded-For la IP que se le conectó, confíe
    o no en los headers entrantes: ese último salto es lo único no falsificable.
    CF-Connecting-IP solo se cree si ese salto es de Cloudflare; si alguien va
    directo al VPS, la clave es su propia IP. Los headers solo se miran si el
    peer es privado (el proxy en la red de Docker).

    ponytail: confía en cualquier peer privado; requiere que el puerto 8000 no
    esté publicado (ver DEPLOYMENT). Si hace falta, acotarlo al CIDR de la red
    de Traefik.
    """
    peer = get_remote_address(request)
    try:
        if not ip_address(peer).is_private:
            return peer
        hop = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        hop_ip = ip_address(hop)
    except ValueError:
        return peer
    if any(hop_ip in net for net in CLOUDFLARE_NETWORKS):
        try:
            return _key(ip_address(request.headers.get("cf-connecting-ip", "").strip()))
        except ValueError:
            pass
    return _key(hop_ip)


limiter = Limiter(
    key_func=client_ip,
    enabled=not bool(settings.TEST_DATABASE_URL),
)
