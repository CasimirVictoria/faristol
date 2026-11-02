"""Accions del banc de treball Faristol (integració externa)."""

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from faristol.api.common import _document_segur, _ruta_segura_nota
from faristol.api.models import FaristolCompanionNoteRequest, FaristolIngestUrlRequest, FaristolOpenExternalRequest, FaristolOpenNoteExternalRequest
from faristol.config import BIB_FILE_PATH, FONTS_DIR, HIGHLIGHTS_FILE, NOTAS_DIR
from faristol.fsatomic import bloqueig, escriure_atomic, llegir_json_segur

router = APIRouter()

@router.get("/faristol/status")
async def faristol_status():
    """Retorna l'estat i recompte de fitxers per a Faristol Web."""
    total_notes = len([p for p in NOTAS_DIR.glob("*") if p.is_file() and p.suffix.lower() in [".md", ".org"]])
    total_pdfs = len(list(FONTS_DIR.glob("*.pdf")))
    return {
        "status": "ok",
        "notes_dir": str(NOTAS_DIR),
        "fonts_dir": str(FONTS_DIR),
        "bib_file": str(BIB_FILE_PATH),
        "total_notes": total_notes,
        "total_pdfs": total_pdfs,
    }

@router.post("/faristol/open-external")
async def faristol_open_external(req: FaristolOpenExternalRequest):
    """Obre el PDF amb el visor natiu del sistema (Okular / xdg-open) amb comprovació estricta de ruta."""
    p = _document_segur(req.pdf)

    try:
        subprocess.Popen(
            ["okular", str(p.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
        return {"status": "ok", "message": f"Obert {p.name} amb Okular"}
    except Exception:
        try:
            subprocess.Popen(
                ["xdg-open", str(p.resolve())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True
            )
            return {"status": "ok", "message": f"Obert {p.name} amb xdg-open"}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"No s'ha pogut obrir el visor extern: {e2}")

@router.post("/faristol/open-note-external")
async def faristol_open_note_external(req: FaristolOpenNoteExternalRequest):
    """Obre la nota amb l'editor natiu del sistema (Kate / Obsidian / xdg-open) amb comprovació estricta de ruta."""
    p = _ruta_segura_nota(req.filename)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        subprocess.Popen(
            ["kate", str(p.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
        return {"status": "ok", "message": f"Obert {p.name} amb Kate"}
    except Exception:
        try:
            subprocess.Popen(
                ["xdg-open", str(p.resolve())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True
            )
            return {"status": "ok", "message": f"Obert {p.name} amb xdg-open"}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"No s'ha pogut obrir l'editor extern: {e2}")

@router.post("/faristol/save-pdf")
async def faristol_save_pdf(request: Request, filename: str = Query(..., description="Nom del fitxer PDF o HTML a desar")):
    """Desa les modificacions i anotacions del PDF o Snapshot HTML directament sobre el fitxer físic original a disc de forma atòmica."""
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Contingut binari buit")

    clean_raw = Path(urllib.parse.unquote(filename)).name
    if not clean_raw or clean_raw in (".", "..") or "/" in clean_raw or "\\" in clean_raw:
        raise HTTPException(status_code=400, detail="Nom de fitxer no vàlid")

    target_pdf = _document_segur(clean_raw)
    ext = target_pdf.suffix.lower()
    if ext not in (".pdf", ".html", ".htm"):
        raise HTTPException(status_code=400, detail="Només es permet desar fitxers .pdf, .html o .htm")

    if ext == ".pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="El contingut no correspon a un fitxer PDF vàlid (falta capçalera %PDF)")

    try:
        with bloqueig(target_pdf):
            escriure_atomic(target_pdf, content, backup=True)
        return {
            "status": "ok",
            "message": f"Fitxer {target_pdf.name} desat correctament a disc",
            "size": len(content)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error escrivint fitxer a disc: {e}")

@router.get("/faristol/extract-highlights")
async def faristol_extract_highlights(pdf: str = Query(..., description="Nom o ruta del fitxer PDF o HTML")):
    """Extreu tots els subratllats i anotacions d'un PDF (via PyMuPDF) o d'un Snapshot HTML (<mark>) i highlights.json."""
    target_pdf = _document_segur(pdf)
    clean_name = target_pdf.name

    def _extreure_sync() -> dict:
        if target_pdf.suffix.lower() in [".html", ".htm"]:
            highlights = []
            seen_texts = set()
            try:
                html_content = target_pdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'<mark\b[^>]*class=["\'][^"\']*\bfaristol-highlight\b[^"\']*["\'][^>]*>([\s\S]*?)</mark>', html_content, re.I):
                    raw_t = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    clean_t = re.sub(r'\s+', ' ', raw_t)
                    if clean_t and clean_t not in seen_texts:
                        seen_texts.add(clean_t)
                        highlights.append({"page": 1, "text": clean_t})
            except Exception:
                pass
            if HIGHLIGHTS_FILE.exists():
                try:
                    with bloqueig(HIGHLIGHTS_FILE):
                        tots = llegir_json_segur(HIGHLIGHTS_FILE, {})
                    for key in [str(target_pdf.resolve()), clean_name]:
                        for h in tots.get(key, []):
                            clean_t = re.sub(r'\s+', ' ', (h.get("text") or "").strip())
                            if clean_t and clean_t not in seen_texts:
                                seen_texts.add(clean_t)
                                highlights.append({"page": h.get("page", 1), "text": clean_t})
                except Exception:
                    pass
            return {
                "status": "ok",
                "pdf": clean_name,
                "total": len(highlights),
                "highlights": highlights
            }

        import fitz
        doc = fitz.open(str(target_pdf))
        highlights = []
        try:
            for page_num in range(len(doc)):
                page = doc[page_num]
                for annot in page.annots():
                    if annot.type[0] == 8:  # 8 és Highlight a PyMuPDF
                        quads = annot.vertices
                        text_parts = []
                        if quads and len(quads) >= 4:
                            for i in range(0, len(quads), 4):
                                r = fitz.Quad(quads[i:i+4]).rect
                                t = page.get_text("text", clip=r).replace("\n", " ").strip()
                                if t:
                                    text_parts.append(t)
                        if not text_parts:
                            t = page.get_text("text", clip=annot.rect).replace("\n", " ").strip()
                            if t:
                                text_parts.append(t)

                        clean_text = " ".join(text_parts).strip()
                        clean_text = re.sub(r'\s+', ' ', clean_text)
                        if clean_text:
                            highlights.append({
                                "page": page_num + 1,
                                "text": clean_text,
                                "rect": [annot.rect.x0, annot.rect.y0, annot.rect.x1, annot.rect.y1]
                            })
        finally:
            doc.close()
        return {
            "status": "ok",
            "pdf": clean_name,
            "total": len(highlights),
            "highlights": highlights
        }

    try:
        return await asyncio.to_thread(_extreure_sync)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extreient subratllats: {e}")

@router.post("/faristol/create-companion-note")
async def faristol_create_companion_note(req: FaristolCompanionNoteRequest):
    """Crea una nota associada a un PDF existent si encara no en té."""
    real_pdf = _document_segur(req.pdf)
    stem = real_pdf.stem
    clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '', stem)

    candidats = [f"@{clean_stem}.md", f"@{clean_stem}.org", f"{clean_stem}.md", f"@{stem}.md", f"{stem}.md"]
    for c in candidats:
        if (NOTAS_DIR / c).exists():
            return {"status": "exists", "note": c, "message": "La nota ja existeix"}

    titol = stem
    if real_pdf.exists():
        try:
            import fitz
            doc = fitz.open(str(real_pdf))
            if doc.metadata and doc.metadata.get("title"):
                meta_title = doc.metadata.get("title", "").strip()
                if meta_title and len(meta_title) > 3 and not meta_title.lower().endswith((".doc", ".docx", ".pdf")):
                    titol = meta_title
            doc.close()
        except Exception:
            pass

    nota_name = f"@{clean_stem}.md"
    nota_path = _ruta_segura_nota(nota_name)
    avui = datetime.now().strftime("%Y-%m-%d")

    contingut = f"""---
title: {json.dumps(titol, ensure_ascii=False)}
date: {avui}
tags: ["lectura"]
citekey: "@{clean_stem}"
pdf: "{real_pdf.name}"
---

# {titol}

## Metadades
- **Fitxer PDF:** [[{real_pdf.name}]]
- **Data d'obertura:** {avui}

## Resum & Idees Clau

- 

## Cites i Anotacions

"""
    with bloqueig(nota_path):
        escriure_atomic(nota_path, contingut)
    return {"status": "created", "note": nota_name, "message": "Nota creada correctament"}

@router.post("/faristol/upload-pdf")
async def faristol_upload_pdf(request: Request, filename: str = Query(..., description="Nom del fitxer PDF")):
    """Puja un PDF, el desa a FONTS_DIR, n'extrau metadades i crea la seua nota Markdown associada de forma segura."""
    content = await request.body()
    if not content or not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Contingut no vàlid: cal un fitxer PDF amb capçalera %PDF")

    try:
        import fitz
    except ImportError:
        fitz = None

    raw_name = Path(urllib.parse.unquote(filename)).name
    clean_name = re.sub(r'[^\w\-_\.]', '_', raw_name)
    if not clean_name.lower().endswith(".pdf"):
        clean_name += ".pdf"

    dest_pdf = FONTS_DIR / clean_name
    if dest_pdf.exists():
        s_base = dest_pdf.stem
        cnt = 2
        while (FONTS_DIR / f"{s_base}_{cnt}.pdf").exists():
            cnt += 1
        dest_pdf = FONTS_DIR / f"{s_base}_{cnt}.pdf"
        clean_name = dest_pdf.name

    with bloqueig(dest_pdf):
        escriure_atomic(dest_pdf, content)

    stem = dest_pdf.stem
    clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '', stem)
    titol = stem
    num_pagines = 1

    if fitz:
        try:
            doc = fitz.open(str(dest_pdf))
            num_pagines = len(doc)
            if doc.metadata and doc.metadata.get("title"):
                meta_titol = doc.metadata.get("title", "").strip()
                if meta_titol and len(meta_titol) > 3 and not meta_titol.lower().endswith((".doc", ".docx", ".pdf")):
                    titol = meta_titol
            doc.close()
        except Exception:
            pass

    nota_name = f"@{clean_stem}.md"
    nota_path = _ruta_segura_nota(nota_name)
    avui = datetime.now().strftime("%Y-%m-%d")

    if not nota_path.exists():
        nota_content = f"""---
title: {json.dumps(titol, ensure_ascii=False)}
date: {avui}
tags: ["paper", "lectura"]
citekey: "@{clean_stem}"
pdf: "{clean_name}"
---

# {titol}

## Metadades
- **Fitxer PDF:** [[{clean_name}]]
- **Pàgines:** {num_pagines}
- **Data d'ingesta:** {avui}

## Resum & Idees Clau

- 

## Cites i Anotacions

"""
        with bloqueig(nota_path):
            escriure_atomic(nota_path, nota_content)

    return {
        "status": "ok",
        "pdf": clean_name,
        "note": nota_name,
        "title": titol,
        "pages": num_pagines,
        "message": f"PDF {clean_name} ingerit i nota {nota_name} creada"
    }

def _validar_host_ssrf(url_str: str) -> urllib.parse.ParseResult:
    """Valida l'esquema d'una URL i comprova que cap IP resolta apunti a xarxes privades o locals."""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Només es permeten URLs http:// o https:// (esquemes com file:// estan prohibits)")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="Nom d'amfitrió invàlid")
    try:
        addr_info = socket.getaddrinfo(parsed.hostname, None)
        for addr in addr_info:
            ip_obj = ipaddress.ip_address(addr[4][0])
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
                raise HTTPException(status_code=400, detail="Accés denegat: la URL apunta a una IP de xarxa privada o reservada")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="No s'ha pogut resoldre el nom d'amfitrió de la URL")
    return parsed

@router.post("/faristol/ingest-url")
async def faristol_ingest_url(req: FaristolIngestUrlRequest):
    """Descarrega un PDF d'un enllaç URL amb protecció contra SSRF (incloent redireccions) i escriptura atòmica."""
    import urllib.request
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL buida")

    def _ingest_sync() -> dict:
        parsed = _validar_host_ssrf(url)

        class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req_redir, fp, code, msg, headers, newurl):
                full_new = urllib.parse.urljoin(req_redir.full_url, newurl)
                _validar_host_ssrf(full_new)
                return super().redirect_request(req_redir, fp, code, msg, headers, full_new)

        path_leaf = Path(parsed.path).name
        if not path_leaf or not path_leaf.lower().endswith(".pdf"):
            path_leaf = f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        clean_name = re.sub(r'[^\w\-_\.]', '_', path_leaf)
        dest_pdf = FONTS_DIR / clean_name
        if dest_pdf.exists():
            s_base = dest_pdf.stem
            cnt = 2
            while (FONTS_DIR / f"{s_base}_{cnt}.pdf").exists():
                cnt += 1
            dest_pdf = FONTS_DIR / f"{s_base}_{cnt}.pdf"
            clean_name = dest_pdf.name

        req_headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}
        request_obj = urllib.request.Request(url, headers=req_headers)
        opener = urllib.request.build_opener(_SafeRedirectHandler)

        MAX_SIZE = 100 * 1024 * 1024  # 100 MB màxim
        with opener.open(request_obj, timeout=30) as response:
            content = response.read(MAX_SIZE + 1)
            if len(content) > MAX_SIZE:
                raise HTTPException(status_code=400, detail="El fitxer supera el límit permès de 100 MB")
            if not content.startswith(b"%PDF"):
                raise HTTPException(status_code=400, detail="El recurs descarregat no és un document PDF vàlid")

        with bloqueig(dest_pdf):
            escriure_atomic(dest_pdf, content)

        try:
            import fitz
            doc = fitz.open(str(dest_pdf))
            num_pagines = len(doc)
            titol = doc.metadata.get("title", "") if doc.metadata else ""
            if not titol or titol.lower().endswith((".doc", ".docx", ".pdf")):
                titol = dest_pdf.stem
            doc.close()
        except Exception:
            num_pagines = 1
            titol = dest_pdf.stem

        stem = dest_pdf.stem
        clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '', stem)
        nota_name = f"@{clean_stem}.md"
        nota_path = _ruta_segura_nota(nota_name)
        avui = datetime.now().strftime("%Y-%m-%d")

        if not nota_path.exists():
            nota_content = f"""---
title: {json.dumps(titol, ensure_ascii=False)}
date: {avui}
tags: ["web", "lectura"]
citekey: "@{clean_stem}"
pdf: "{clean_name}"
url: {json.dumps(url, ensure_ascii=False)}
---

# {titol}

## Metadades
- **Fitxer PDF:** [[{clean_name}]]
- **URL Origen:** {url}
- **Pàgines:** {num_pagines}
- **Data d'ingesta:** {avui}

## Resum & Idees Clau

- 

## Cites i Anotacions

"""
            with bloqueig(nota_path):
                escriure_atomic(nota_path, nota_content)

        return {
            "status": "ok",
            "pdf": clean_name,
            "note": nota_name,
            "title": titol,
            "pages": num_pagines
        }

    try:
        return await asyncio.to_thread(_ingest_sync)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error descarregant el PDF: {e}")


