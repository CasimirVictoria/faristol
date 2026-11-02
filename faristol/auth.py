"""Autenticació per sessió amb cookies HttpOnly signades (HMAC-SHA256).

Disseny pensat per a ús personal sobre LAN/Tailscale:
- Credencials per variables d'entorn (``FARISTOL_AUTH_USER`` / ``FARISTOL_AUTH_PASSWORD``).
- El token de sessió és *stateless*: ``base64(payload).hmac``, amb caducitat.
  No cal cap base de dades i sobreviu reinicis gràcies a un secret persistent.
- La cookie és ``HttpOnly`` + ``SameSite=Lax`` i dura 30 dies per defecte
  (el clàssic «Recorda'm»). ``Secure`` és opcional via entorn, perquè l'accés
  habitual és per HTTP dins la LAN o Tailscale.

Si no es defineixen les credencials, l'autenticació queda DESACTIVADA i tot
és accessible (comportament retrocompatible per a instal·lacions existents).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from faristol.config import CONFIG_DIR

COOKIE_NAME = "faristol_session"
DIES_PER_DEFECTE = 30

USUARI = os.environ.get("FARISTOL_AUTH_USER", "").strip()
CONTRASENYA = os.environ.get("FARISTOL_AUTH_PASSWORD", "")
AUTENTICACIO_ACTIVA = bool(USUARI and CONTRASENYA)

MAX_AGE = int(os.environ.get("FARISTOL_SESSION_DAYS", DIES_PER_DEFECTE)) * 86400
COOKIE_SECURE = os.environ.get("FARISTOL_COOKIE_SECURE", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _carregar_secret() -> bytes:
    """Retorna el secret de signatura: entorn, o un fitxer persistent generat."""
    entorn = os.environ.get("FARISTOL_SESSION_SECRET", "").strip()
    if entorn:
        return entorn.encode("utf-8")

    fitxer = CONFIG_DIR / "session_secret"
    try:
        if fitxer.is_file():
            dades = fitxer.read_bytes().strip()
            if dades:
                return dades
    except Exception:
        pass

    secret = secrets.token_bytes(32)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fitxer.write_bytes(secret)
        os.chmod(fitxer, 0o600)
    except Exception:
        # Si no es pot persistir, almenys funciona durant esta execució.
        pass
    return secret


_SECRET = _carregar_secret()


def _b64e(dades: bytes) -> bytes:
    return base64.urlsafe_b64encode(dades).rstrip(b"=")


def _b64d(dades: bytes) -> bytes:
    return base64.urlsafe_b64decode(dades + b"=" * (-len(dades) % 4))


def _signar(payload: bytes) -> bytes:
    return _b64e(hmac.new(_SECRET, payload, hashlib.sha256).digest())


def crear_token(usuari: str, max_age: int = MAX_AGE) -> str:
    """Crea un token de sessió signat amb caducitat."""
    payload = json.dumps(
        {"u": usuari, "exp": int(time.time()) + max_age}, separators=(",", ":")
    ).encode("utf-8")
    b = _b64e(payload)
    return (b + b"." + _signar(b)).decode("ascii")


def verificar_token(token: Optional[str]) -> Optional[str]:
    """Retorna l'usuari si el token és vàlid i no ha caducat; si no, ``None``."""
    if not token:
        return None
    try:
        b, s = token.encode("ascii").split(b".", 1)
        if not hmac.compare_digest(_signar(b), s):
            return None
        payload = json.loads(_b64d(b))
        if int(payload.get("exp", 0)) < time.time():
            return None
        usuari = payload.get("u")
        return str(usuari) if usuari else None
    except Exception:
        return None


def credencials_valides(usuari: str, contrasenya: str) -> bool:
    """Compara credencials en temps constant per evitar atacs de temporització."""
    if not AUTENTICACIO_ACTIVA:
        return False
    u_ok = hmac.compare_digest((usuari or "").encode("utf-8"), USUARI.encode("utf-8"))
    c_ok = hmac.compare_digest((contrasenya or "").encode("utf-8"), CONTRASENYA.encode("utf-8"))
    return u_ok and c_ok
