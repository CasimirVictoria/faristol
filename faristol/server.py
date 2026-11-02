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
from fastapi.responses import FileResponse, JSONResponse

from faristol.api import router as api_router

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

# Muntar fitxers estàtics (PDF.js, Vditor, KaTeX, FontAwesome, CSS, JS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
