"""Security headers, including a strict Content-Security-Policy (no inline scripts, no CDNs)."""
from __future__ import annotations

from app.config import Settings


def build_csp(settings: Settings) -> str:
    supabase = settings.supabase_origin
    livekit = " ".join(settings.livekit_origins)
    connect = " ".join(x for x in ["'self'", supabase, livekit] if x)
    media = " ".join(x for x in ["'self'", "blob:", supabase] if x)
    directives = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        f"media-src {media}",
        f"connect-src {connect}",
        "worker-src 'self' blob:",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


def security_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": build_csp(settings),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(self), microphone=(self), geolocation=(), payment=()",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if settings.is_prod:
        headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return headers
