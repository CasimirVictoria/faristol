"""
Servidor FastAPI per a Faristol.
Serveix la interfície web i munta els endpoints de l'API amb protecció contra CSRF,
DNS rebinding i restricció d'orígens.
"""
import os
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from faristol.api import router as api_router
from faristol.auth import (
    AUTENTICACIO_ACTIVA,
    COOKIE_NAME,
    COOKIE_SECURE,
    MAX_AGE,
    crear_token,
    credencials_valides,
    verificar_token,
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Faristol - Banc de Treball de Lectura i Síntesi",
    description="Entorn lliure d'organització, lectura anotada de PDFs i notes bibliogràfiques inspirat en Org-Noter i Zotero",
    version="1.1.0"
)

# 1. Protecció contra DNS Rebinding i control de capçalera Host.
#    La llista blanca d'amfitrions és configuració, no codi: es llig de la
#    variable d'entorn FARISTOL_ALLOWED_HOSTS (valors separats per comes,
#    s'admet el comodí '*.domini'). Si no es defineix, només s'accepten
#    les adreces locals per seguretat.
_HOSTS_PER_DEFECTE = ["127.0.0.1", "localhost", "testserver"]

def _llegir_hosts_permesos() -> list[str]:
    valor = os.environ.get("FARISTOL_ALLOWED_HOSTS", "").strip()
    if not valor:
        return list(_HOSTS_PER_DEFECTE)
    hosts = [h.strip() for h in valor.split(",") if h.strip()]
    return hosts or list(_HOSTS_PER_DEFECTE)

HOSTS_PERMESOS = _llegir_hosts_permesos()

def _host_permès(host: str) -> bool:
    """Comprova si un amfitrió és a la llista blanca, admetent comodins ('*.exemple.com')."""
    host = (host or "").lower()
    if not host:
        return False
    for patro in HOSTS_PERMESOS:
        p = patro.lower()
        if p.startswith("*."):
            if host == p[2:] or host.endswith(p[1:]):
                return True
        elif host == p:
            return True
    return False

app.add_middleware(TrustedHostMiddleware, allowed_hosts=HOSTS_PERMESOS)

# 2. Middleware d'Aïllament d'Orígens i Protecció contra CSRF
def _afegir_capçaleres_seguretat(response):
    """Aplica les capçaleres de seguretat bàsiques a qualsevol resposta."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

@app.middleware("http")
async def verificar_origen_csrf(request: Request, call_next):
    # Els mètodes segurs de lectura (GET, HEAD, OPTIONS) no alteren estat a disc,
    # així que no requereixen validació d'origen CSRF, però SÍ capçaleres de seguretat.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Validar peticions amb canvi d'estat (POST, PUT, DELETE, PATCH)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        sec_fetch_site = request.headers.get("sec-fetch-site", "").lower()
        host_header = request.headers.get("host", "").lower().split(":")[0]

        if sec_fetch_site == "cross-site":
            return _afegir_capçaleres_seguretat(JSONResponse(
                {"detail": "Petició cross-site bloquejada per protecció CSRF (Sec-Fetch-Site)."},
                status_code=403
            ))

        font_verificacio = origin or referer
        if font_verificacio:
            if origin and origin.strip().lower() == "null":
                return _afegir_capçaleres_seguretat(JSONResponse(
                    {"detail": "Origen 'null' no autoritzat per protecció CSRF."},
                    status_code=403
                ))
            try:
                parsed = urllib.parse.urlparse(font_verificacio)
                origin_host = (parsed.hostname or "").lower()

                # Comprovar si l'origen coincideix amb el host sol·licitat o és a la llista blanca configurada
                es_valid = (
                    origin_host == host_header or
                    _host_permès(origin_host)
                )
                if not es_valid:
                    return _afegir_capçaleres_seguretat(JSONResponse(
                        {"detail": f"Origen no autoritzat ({origin_host}). Petició bloquejada per protecció CSRF."},
                        status_code=403
                    ))
            except Exception:
                return _afegir_capçaleres_seguretat(JSONResponse(
                    {"detail": "Capçalera d'origen no vàlida"},
                    status_code=400
                ))

    # Afegir capçaleres de seguretat bàsiques a TOTES les respostes (incloses les de lectura)
    response = await call_next(request)
    return _afegir_capçaleres_seguretat(response)

# 3. Autenticació per sessió (cookie HttpOnly signada).
#    Si no hi ha credencials configurades (FARISTOL_AUTH_USER/PASSWORD),
#    l'autenticació queda desactivada i tot és accessible.
RUTES_PUBLIQUES = {"/login", "/logout"}

def _es_ruta_publica(path: str) -> bool:
    return path in RUTES_PUBLIQUES or path.startswith("/static/")

@app.middleware("http")
async def require_auth(request: Request, call_next):
    if not AUTENTICACIO_ACTIVA or _es_ruta_publica(request.url.path):
        return await call_next(request)

    if verificar_token(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)

    # Sense sessió vàlida: 401 per a l'API, redirecció al login per a l'HTML.
    if request.url.path.startswith("/api/"):
        return _afegir_capçaleres_seguretat(JSONResponse(
            {"detail": "Cal autenticar-se per accedir a l'API."},
            status_code=401,
        ))
    return _afegir_capçaleres_seguretat(RedirectResponse(url="/login", status_code=303))

# Incloure endpoints de l'API
app.include_router(api_router)

# Ruta arrel
@app.get("/")
async def servir_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(
            str(index_path),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return {"message": "Faristol Web API activa. Falta index.html a static/"}

# Ruta /faristol
@app.get("/faristol")
async def servir_faristol():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(
            str(index_path),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return {"message": "Faristol Web API activa. Falta index.html a static/"}

# --- Autenticació: login / logout / estat de sessió ---

@app.get("/login")
async def servir_login(request: Request):
    """Mostra el formulari de login (o redirigeix si ja hi ha sessió)."""
    if AUTENTICACIO_ACTIVA and verificar_token(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(
        str(STATIC_DIR / "login.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.post("/login")
async def processar_login(request: Request):
    """Valida les credencials i, si són correctes, establix la cookie de sessió."""
    form = await request.form()
    usuari = str(form.get("username", ""))
    contrasenya = str(form.get("password", ""))
    if not credencials_valides(usuari, contrasenya):
        return RedirectResponse(url="/login?error=1", status_code=303)

    resposta = RedirectResponse(url="/", status_code=303)
    resposta.set_cookie(
        key=COOKIE_NAME,
        value=crear_token(usuari),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return resposta

@app.get("/logout")
@app.post("/logout")
async def tancar_sessio():
    """Esborra la cookie de sessió i torna al formulari de login."""
    resposta = RedirectResponse(url="/login", status_code=303)
    resposta.delete_cookie(COOKIE_NAME, path="/")
    return resposta

@app.get("/api/session")
async def estat_sessio(request: Request):
    """Estat de la sessió actual (només accessible amb sessió vàlida)."""
    return {"authenticated": True, "user": verificar_token(request.cookies.get(COOKIE_NAME))}

# Muntar fitxers estàtics (PDF.js, Vditor, KaTeX, FontAwesome, CSS, JS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
