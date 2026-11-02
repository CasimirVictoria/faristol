"""Streaming de PDF i subratllats."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from faristol.api.common import _document_segur
from faristol.api.models import SaveHighlightRequest
from faristol.config import HIGHLIGHTS_FILE
from faristol.fsatomic import bloqueig, escriure_atomic, llegir_json_segur

router = APIRouter()

@router.get("/pdf/stream")
async def servir_pdf(path: str = Query(..., description="Ruta absoluta o relativa del PDF o Snapshot HTML")):
    """Serveix un fitxer PDF o Snapshot HTML de forma segura per a ser renderitzat al navegador."""
    pdf_path = _document_segur(path)

    ext = pdf_path.suffix.lower()
    if ext not in [".pdf", ".html", ".htm"]:
        raise HTTPException(status_code=400, detail="El fitxer sol·licitat no és un PDF ni un Snapshot HTML")

    media_type = "text/html; charset=utf-8" if ext in [".html", ".htm"] else "application/pdf"
    
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }

    # Aïllament i protecció estricta per a Snapshots HTML (desactiva scripts i aïlla el context)
    if ext in [".html", ".htm"]:
        headers["Content-Security-Policy"] = (
            "sandbox allow-same-origin; "
            "script-src 'none'; "
            "object-src 'none'; "
            "form-action 'none'; "
            "base-uri 'none';"
        )

    return FileResponse(
        str(pdf_path),
        media_type=media_type,
        content_disposition_type="inline",
        filename=pdf_path.name,
        headers=headers
    )

@router.get("/pdf/highlights")
async def obtenir_subratllats_pdf(path: str = Query(..., description="Ruta del PDF")):
    """Retorna tots els subratllats guardats per a un PDF concret."""
    pdf_path = str(_document_segur(path))
    with bloqueig(HIGHLIGHTS_FILE):
        tots = llegir_json_segur(HIGHLIGHTS_FILE, {})
        return {"highlights": tots.get(pdf_path, [])}

@router.post("/pdf/highlights")
async def desar_subratllat_pdf(req: SaveHighlightRequest):
    """Guarda un nou subratllat persistent associat a un PDF de forma atòmica."""
    pdf_path = str(_document_segur(req.pdf_path))
    with bloqueig(HIGHLIGHTS_FILE):
        tots = llegir_json_segur(HIGHLIGHTS_FILE, {})
        if not isinstance(tots, dict):
            tots = {}

        if pdf_path not in tots:
            tots[pdf_path] = []

        nou_item = {
            "id": f"{req.page}_{int(datetime.now().timestamp() * 1000)}",
            "page": req.page,
            "text": req.text,
            "rects": req.rects,
            "color": req.color,
            "comment": req.comment,
            "created_at": datetime.now().isoformat()
        }
        tots[pdf_path].append(nou_item)

        try:
            escriure_atomic(HIGHLIGHTS_FILE, json.dumps(tots, ensure_ascii=False, indent=2))
            return {"status": "ok", "highlight": nou_item}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@router.delete("/pdf/highlights/{highlight_id}")
async def esborrar_subratllat_pdf(highlight_id: str, path: str = Query(...)):
    """Esborra un subratllat concret d'un PDF de forma atòmica."""
    pdf_path = str(_document_segur(path))
    with bloqueig(HIGHLIGHTS_FILE):
        tots = llegir_json_segur(HIGHLIGHTS_FILE, {})
        if isinstance(tots, dict) and pdf_path in tots:
            tots[pdf_path] = [h for h in tots[pdf_path] if h.get("id") != highlight_id]
            try:
                escriure_atomic(HIGHLIGHTS_FILE, json.dumps(tots, ensure_ascii=False, indent=2))
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        return {"status": "ok"}


