"""Biblioteca, fonts, esdeveniments SSE i referències BibTeX."""

import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from faristol.bibtex import GestorBibtex
from faristol.config import BIB_FILE_PATH, COLLECTIONS_FILE, FONTS_DIR, NOTAS_DIR

router = APIRouter()

def get_library_signature() -> str:
    """Calcula una signatura ràpida basada en els mtimes dels directoris i fitxers clau."""
    try:
        b_mtime = BIB_FILE_PATH.stat().st_mtime if BIB_FILE_PATH.exists() else 0
    except Exception:
        b_mtime = 0
    try:
        c_mtime = COLLECTIONS_FILE.stat().st_mtime if COLLECTIONS_FILE.exists() else 0
    except Exception:
        c_mtime = 0
    try:
        f_mtime = FONTS_DIR.stat().st_mtime if FONTS_DIR.exists() else 0
    except Exception:
        f_mtime = 0
    try:
        n_mtime = NOTAS_DIR.stat().st_mtime if NOTAS_DIR.exists() else 0
    except Exception:
        n_mtime = 0

    return f"{b_mtime:.3f}_{c_mtime:.3f}_{f_mtime:.3f}_{n_mtime:.3f}"

class NotifyLibraryRequest(BaseModel):
    doc: Optional[str] = None

sse_subscribers: set[asyncio.Queue] = set()

async def broadcast_sse_event(event_type: str, data: dict):
    """Envia un esdeveniment a totes les pestanyes connectades per SSE."""
    for q in list(sse_subscribers):
        try:
            q.put_nowait({"event": event_type, "data": data})
        except Exception:
            pass

@router.get("/library/status")
async def obtenir_estat_biblioteca():
    """Retorna la signatura actual de la biblioteca per a comprovació ràpida."""
    sig = get_library_signature()
    return {
        "status": "ok",
        "signature": sig,
        "timestamp": time.time()
    }

@router.post("/library/notify-change")
async def notificar_canvi_biblioteca(req: Optional[NotifyLibraryRequest] = None):
    """Permet a zotero-receiver, add-paper o altres eines notificar canvis immediatament."""
    sig = get_library_signature()
    payload = {"signature": sig, "event": "library-changed"}
    if req and req.doc:
        payload["doc"] = req.doc
    await broadcast_sse_event("change", payload)
    if req and req.doc:
        await broadcast_sse_event("open-document", {"doc": req.doc})
    return {"status": "ok", "message": "Canvi notificat correctament"}

@router.post("/library/open-document")
async def obrir_document_remot(req: NotifyLibraryRequest):
    """Demana a les pestanyes de Faristol que òbriguen un document immediatament."""
    if not req or not req.doc:
        raise HTTPException(status_code=400, detail="Cal especificar 'doc'")
    await broadcast_sse_event("open-document", {"doc": req.doc})
    return {"status": "ok", "doc": req.doc}

@router.get("/library/events")
async def stream_esdeveniments_biblioteca():
    """SSE (Server-Sent Events) per sincronitzar la interfície de Faristol a l'instant en segon pla."""
    queue = asyncio.Queue()
    sse_subscribers.add(queue)

    async def generador():
        last_sig = get_library_signature()
        yield f"event: connected\ndata: {json.dumps({'signature': last_sig})}\n\n"
        try:
            while True:
                try:
                    # Espera un esdeveniment específic per cua fins a 10 segons
                    item = await asyncio.wait_for(queue.get(), timeout=10.0)
                    evt_name = item.get("event", "change")
                    evt_data = json.dumps(item.get("data", {}))
                    yield f"event: {evt_name}\ndata: {evt_data}\n\n"
                except asyncio.TimeoutError:
                    # Polling suau passiu cada 10 segons si hi ha hagut canvis sense notificar
                    cur_sig = get_library_signature()
                    if cur_sig != last_sig:
                        last_sig = cur_sig
                        payload = json.dumps({"signature": cur_sig, "event": "library-changed"})
                        yield f"event: change\ndata: {payload}\n\n"
                    else:
                        yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_subscribers.discard(queue)

    return StreamingResponse(
        generador(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/fonts/list")
async def llistar_fonts():
    """Llista els PDFs i Snapshots HTML existents a la carpeta de la biblioteca."""
    pdfs = []
    for ext in ("*.pdf", "*.html"):
        for p in FONTS_DIR.glob(ext):
            try:
                stat = p.stat()
                pdfs.append({
                    "filename": p.name,
                    "path": str(p.resolve()),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_html": p.suffix.lower() == ".html"
                })
            except Exception:
                continue
    pdfs.sort(key=lambda x: x["filename"])
    return {"fonts": pdfs, "total": len(pdfs), "folder": str(FONTS_DIR.resolve())}

@router.get("/bibtex/references")
async def obtenir_referencies_bibtex(q: Optional[str] = None):
    """Retorna les referències canòniques de biblioteca.bib (zoterless)."""
    if not BIB_FILE_PATH.exists():
        return {"references": [], "total": 0, "file": str(BIB_FILE_PATH)}
    refs = GestorBibtex.cercar(BIB_FILE_PATH, q or "")
    return {
        "references": refs,
        "total": len(refs),
        "file": str(BIB_FILE_PATH),
        "folder": str(FONTS_DIR)
    }


