"""
Color de marca de la página pública (ítem 41): un hex validado del que se
derivan las variables CSS del tema. Lo que llega al <style> sale siempre de
estas funciones (hex y números), nunca texto del dueño.
"""

import re

# --bg de public_booking.html: el color tiene que leerse sobre este fondo.
PAGE_BG = (8, 8, 11)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
# WCAG: 3:1 para texto grande y elementos de interfaz (bordes, íconos).
MIN_CONTRAST = 3.0
DEFAULT_BRAND_COLOR = "#22d3ee"  # el cian del tema de Juturno

_HEX = re.compile(r"#[0-9a-fA-F]{6}")
# Como se guarda (lo exige el CHECK de la DB).
_STORED = re.compile(r"#[0-9a-f]{6}")

RGB = tuple[int, int, int]


def _rgb(color: str) -> RGB:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def _hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _luminance(rgb: RGB) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    """Relación de contraste WCAG entre dos colores (1 a 21)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(rgb: RGB, other: RGB, t: float) -> RGB:
    r, g, b = (round(c + (o - c) * t) for c, o in zip(rgb, other, strict=True))
    return (r, g, b)


def clean_brand_color(raw: str | None) -> str | None:
    """Hex del formulario → "#rrggbb"; vacío = None (tema por defecto).

    ValueError("invalid") si no es #RRGGBB; ValueError("too_dark") si no se
    lee sobre el fondo oscuro de la página.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if not _HEX.fullmatch(text):
        raise ValueError("invalid")
    color = text.lower()
    if contrast(_rgb(color), PAGE_BG) < MIN_CONTRAST:
        raise ValueError("too_dark")
    return color


def brand_theme(color: str | None) -> dict[str, str] | None:
    """Variables CSS que reemplazan los acentos del tema; None = tema por defecto.

    Revalida el color aunque venga de la DB: va dentro de un <style> y
    _rgb (int(x, 16)) acepta "+", espacios y "_". Todo lo emitido sale de enteros.
    """
    if not color or not _STORED.fullmatch(color):
        return None
    rgb = _rgb(color)
    color = _hex(rgb)
    light, xlight = _mix(rgb, WHITE, 0.35), _mix(rgb, WHITE, 0.6)
    # El botón va del color a un tono más oscuro: el texto se elige por el
    # peor contraste de las dos puntas del degradé.
    deep = _mix(rgb, BLACK, 0.2)
    white_worst = min(contrast(WHITE, rgb), contrast(WHITE, deep))
    dark_worst = min(contrast(PAGE_BG, rgb), contrast(PAGE_BG, deep))
    cta_text = WHITE if white_worst >= dark_worst else PAGE_BG
    rgb_css = ",".join(map(str, rgb))
    return {
        "--cyan": color,
        "--cyan-rgb": rgb_css,
        "--cyan-l": _hex(light),
        "--cyan-l-rgb": ",".join(map(str, light)),
        "--cyan-xl": _hex(xlight),
        "--purple": color,
        "--purple-rgb": rgb_css,
        "--purple-l": _hex(light),
        "--purple-l-rgb": ",".join(map(str, light)),
        "--glow-rgb": rgb_css,
        "--grad": f"linear-gradient(90deg,{color},{_hex(deep)})",
        "--cta-text": _hex(cta_text),
    }
