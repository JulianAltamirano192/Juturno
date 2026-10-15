"""
Logo del negocio (ítem 15). Toda imagen subida se re-codifica: así lo que se
sirve es siempre un WebP chico generado por nosotros (nunca bytes del usuario,
ni SVG, ni el EXIF de la foto, que en un celular trae la ubicación GPS).
"""

import io
import struct
from datetime import datetime

from PIL import Image, ImageOps

# Una foto de celular pesa 3-6 MB; se achica igual, así que solo se acota la subida.
LOGO_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
LOGO_MAX_SIDE = 512
# Imagen "bomba": pocos bytes, mucha RAM al decodificarla. Dos topes: el tamaño
# declarado (un JPEG progresivo carga todo aunque draft lo achique; 50 MP deja
# pasar una foto de celular de 48 MP) y lo que de verdad se decodifica tras
# draft (25 MP en RGBA son ~100 MB). Las subidas concurrentes las acota el panel.
_MAX_DECLARED_PIXELS = 50_000_000
_MAX_PIXELS = 25_000_000


def process_logo(raw: bytes) -> bytes:
    """Imagen JPEG/PNG/WebP → WebP de hasta 512 px de lado y sin metadatos.

    ValueError si no es una imagen de esos formatos o es demasiado grande.
    Bloquea (decodifica la imagen): llamarla con run_in_threadpool.
    """
    try:
        with Image.open(io.BytesIO(raw), formats=["JPEG", "PNG", "WEBP"]) as img:
            if img.width * img.height > _MAX_DECLARED_PIXELS:
                raise ValueError("too_many_pixels")
            # JPEG: decodifica directamente a una escala menor (más rápido).
            img.draft(None, (LOGO_MAX_SIDE, LOGO_MAX_SIDE))
            if img.width * img.height > _MAX_PIXELS:
                raise ValueError("too_many_pixels")
            # Fotos "de costado": aplica la rotación del EXIF antes de borrarlo.
            logo = ImageOps.exif_transpose(img)
            logo.thumbnail((LOGO_MAX_SIDE, LOGO_MAX_SIDE))
            if logo.mode not in ("RGB", "RGBA"):
                logo = logo.convert("RGBA")
            out = io.BytesIO()
            logo.save(out, "WEBP", quality=85, exif=b"", icc_profile=None)
            return out.getvalue()
    except (
        OSError,
        EOFError,
        SyntaxError,
        ValueError,
        struct.error,
        Image.DecompressionBombError,
    ) as exc:
        # Archivo corrupto o raro: "no es una imagen" en vez de un 500.
        raise ValueError("not_image") from exc


def logo_version(updated_at: datetime | None) -> int | None:
    """Valor del ?v= del logo: cambia al reemplazarlo, así la caché no
    muestra el viejo. En milisegundos: dos subidas en el mismo segundo."""
    return int(updated_at.timestamp() * 1000) if updated_at else None
