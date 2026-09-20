"""
Servidor FastAPI per a Plec.
Serveix la interfície web i munta els endpoints de l'API.
"""
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from plec.api import router as api_router

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Plec - Banc de Treball de Lectura i Síntesi",
    description="Entorn lliure d'organització, lectura anotada de PDFs i notes bibliogràfiques inspirat en Org-Noter i Zotero",
    version="1.0.0"
)

# CORS per a desenvolupament flexible
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return {"message": "Plec Web API activa. Falta index.html a static/"}

# Ruta /plec compatible
@app.get("/plec")
async def servir_plec():
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
    return {"message": "Plec Web API activa. Falta index.html a static/"}

# Muntar fitxers estàtics (PDF.js, Vditor, KaTeX, FontAwesome, CSS, JS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
