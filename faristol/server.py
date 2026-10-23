"""
Servidor FastAPI per a Faristol.
Serveix la interfície web i munta els endpoints de l'API amb protecció contra CSRF,
DNS rebinding i restricció d'orígens.
"""
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

# 1. Protecció contra DNS Rebinding i control de capçalera Host
HOSTS_PERMESOS = [
    "127.0.0.1",
    "localhost",
    "casalap",
    "casalap.tailb8f4b1.ts.net",
    "*.ts.net",
    "100.80.29.31",
    "testserver"
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=HOSTS_PERMESOS)

# 2. Middleware d'Aïllament d'Orígens i Protecció contra CSRF
@app.middleware("http")
async def verificar_origen_csrf(request: Request, call_next):
    # Els mètodes segurs de lectura no alteren estat a disc
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    # Validar peticions amb canvi d'estat (POST, PUT, DELETE, PATCH)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host_header = request.headers.get("host", "").lower().split(":")[0]

    font_verificacio = origin or referer
    if font_verificacio:
        try:
            parsed = urllib.parse.urlparse(font_verificacio)
            origin_host = (parsed.hostname or "").lower()
            
            # Comprovar si l'origen coincideix amb el host sol·licitat o pertany a la xarxa local/Tailscale
            es_valid = (
                origin_host == host_header or
                origin_host in ("127.0.0.1", "localhost", "casalap", "100.80.29.31") or
                origin_host.endswith(".ts.net")
            )
            if not es_valid:
                return JSONResponse(
                    {"detail": f"Origen no autoritzat ({origin_host}). Petició bloquejada per protecció CSRF."},
                    status_code=403
                )
        except Exception:
            return JSONResponse({"detail": "Capçalera d'origen no vàlida"}, status_code=400)

    # Afegir capçaleres de seguretat bàsiques
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

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

# Ruta /plec (retrocompatibilitat)
@app.get("/plec")
async def servir_plec():
    return await servir_faristol()

# Muntar fitxers estàtics (PDF.js, Vditor, KaTeX, FontAwesome, CSS, JS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
