"""
API FastAPI per a Plec - Entorn de lectura, anotació i síntesi bibliogràfica.
"""
import os
import sys
import re
import json
import glob
import shutil
import asyncio
import logging
import urllib.parse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from plec.config import (
    NOTAS_DIR,
    FONTS_DIR,
    BIB_FILE_PATH,
    COLLECTIONS_FILE,
    HIGHLIGHTS_FILE
)
from plec.utils import (
    enviar_a_la_papelera,
    netejar_text_slug,
    detectar_tots_els_pdfs,
    extraure_pagines_nota,
    extraure_identificador_fitxer,
    extraure_tags_nota,
    check_and_import_from_downloads,
    obtenir_pdfs_associats_nota,
    obtenir_notes_associades_pdf
)
from plec.bibtex import GestorBibtex

logger = logging.getLogger("plec.api")
router = APIRouter(prefix="/api")

# =====================================================================
# MODELS PYDANTIC
# =====================================================================

class GuardarNotaRequest(BaseModel):
    contingut: str

class NovaNotaRequest(BaseModel):
    titol: str
    tags: str = ""
    plantilla: str = "estat_art"
    contingut: Optional[str] = None

class SaveHighlightRequest(BaseModel):
    pdf_path: str
    page: int
    text: str
    rects: list
    color: str = "#fef08a"
    comment: str = ""

class AcademicImportRequest(BaseModel):
    identifier: str
    vpn: bool = False
    pdf_url: Optional[str] = None

class PlecCompanionNoteRequest(BaseModel):
    pdf: str

class PlecOpenExternalRequest(BaseModel):
    pdf: str

class PlecOpenNoteExternalRequest(BaseModel):
    filename: str

class PlecIngestUrlRequest(BaseModel):
    url: str

class NovaColleccioRequest(BaseModel):
    name: str
    parent_id: Optional[str] = None
    icon: Optional[str] = None

class ActualitzarColleccioRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None
    icon: Optional[str] = None

class ItemColleccioRequest(BaseModel):
    type: str  # "note" o "pdf"
    filename: str

class SetItemCollectionsRequest(BaseModel):
    type: str
    filename: str
    collection_ids: List[str]

class GuardarColleccionsRequest(BaseModel):
    collections: List[dict]

class NlmQueryRequest(BaseModel):
    notebook_id: str
    query: str
    conversation_id: Optional[str] = None
    source_id: Optional[str] = None
    source_ids: Optional[List[str]] = None

class NlmAddUrlRequest(BaseModel):
    notebook_id: str
    url: str

class NlmDeleteRequest(BaseModel):
    target_type: str  # 'notebook' or 'source'
    item_id: str
    notebook_id: Optional[str] = None

class NlmSintesiRequest(BaseModel):
    fitxer_desti: str
    pregunta: str
    resposta: str
    origen: str


# =====================================================================
# 1. GESTIÓ DE NOTES
# =====================================================================

def _extreure_titol_nota(path: Path, contingut: str) -> str:
    """Extrau el títol d'una nota Markdown o Org."""
    m_yaml = re.search(r'(?m)^title:\s*["\']?(.*?)["\']?$', contingut)
    if m_yaml and m_yaml.group(1).strip():
        return m_yaml.group(1).strip()
    m_org = re.search(r'(?i)#\+title:\s*(.*)', contingut)
    if m_org:
        return m_org.group(1).strip()
    m_md = re.search(r'(?m)^#\s+(.*)', contingut)
    if m_md:
        return m_md.group(1).strip()
    return path.stem

@router.get("/notes")
async def llistar_notes(q: Optional[str] = None, tag: Optional[str] = None):
    """Retorna la llista de totes les notes disponibles amb metadades."""
    notes = []
    totes_les_tags = set()

    for path in NOTAS_DIR.glob("*"):
        if path.name.startswith((".", "#")) or path.name.endswith("~") or not path.is_file():
            continue
        if path.suffix.lower() not in [".md", ".org"]:
            continue

        try:
            stat = path.stat()
            mtime = stat.st_mtime
            mtime_dt = datetime.fromtimestamp(mtime)
            mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M")

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                capcalera = f.read(1500)

            titol = _extreure_titol_nota(path, capcalera)
            tags = list(extraure_tags_nota(path.name, capcalera))
            for t in tags:
                totes_les_tags.add(t)

            if q:
                q_lower = q.lower()
                if q_lower not in path.name.lower() and q_lower not in titol.lower():
                    continue

            if tag and tag.lower() not in [t.lower() for t in tags]:
                continue

            pdfs = detectar_tots_els_pdfs(path, capcalera)

            notes.append({
                "filename": path.name,
                "path": str(path.resolve()),
                "title": titol,
                "mtime": mtime,
                "mtime_str": mtime_str,
                "tags": tags,
                "is_org": path.suffix.lower() == ".org",
                "is_md": path.suffix.lower() == ".md",
                "size": stat.st_size,
                "has_pdf": len(pdfs) > 0,
                "pdf_count": len(pdfs),
                "is_biblio": path.name.startswith("@"),
            })
        except Exception:
            continue

    notes.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "notes": notes,
        "tags": sorted(list(totes_les_tags)),
        "total": len(notes)
    }

@router.get("/notes/{filename}")
async def llegir_nota(filename: str):
    """Llegeix el contingut complet d'una nota i en detecta PDFs i pàgines."""
    path = NOTAS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        stat = path.stat()
        titol = _extreure_titol_nota(path, content[:1500])
        tags = list(extraure_tags_nota(path.name, content[:1500]))
        trobats_raw = detectar_tots_els_pdfs(path, content)

        pdfs = []
        for pos, ruta_pdf, nom in trobats_raw:
            p_obj = Path(ruta_pdf)
            pdfs.append({
                "pos": pos,
                "path": str(ruta_pdf),
                "name": nom,
                "filename": p_obj.name,
                "exists": p_obj.exists()
            })

        pagines = extraure_pagines_nota(content)

        return {
            "filename": filename,
            "path": str(path.resolve()),
            "title": titol,
            "content": content,
            "mtime": stat.st_mtime,
            "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "tags": tags,
            "pdfs": pdfs,
            "pagines": pagines
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/notes/{filename}")
async def guardar_nota(filename: str, req: GuardarNotaRequest):
    """Guarda els canvis en una nota existent."""
    path = NOTAS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(req.contingut)

        stat = path.stat()
        return {
            "status": "ok",
            "filename": filename,
            "mtime": stat.st_mtime,
            "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/notes/{filename}")
async def esborrar_nota(filename: str):
    """Mou la nota a la paperera de reciclatge."""
    path = NOTAS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    ok = enviar_a_la_papelera(path)
    if not ok:
        raise HTTPException(status_code=500, detail="No s'ha pogut moure la nota a la paperera")
    return {"status": "ok", "message": f"Nota {filename} moguda a la paperera"}

@router.post("/notes")
async def crear_nova_nota(req: NovaNotaRequest):
    """Crea una nova nota Markdown o Denote."""
    titol_clean = req.titol.strip()
    if not titol_clean:
        raise HTTPException(status_code=400, detail="El títol és obligatori")

    ara = datetime.now()
    avui = ara.strftime("%Y-%m-%d")

    # Si comença amb @, és una companion note (@citekey.md)
    if titol_clean.startswith("@"):
        clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '', titol_clean[1:])
        nom_fitxer = f"@{clean_stem}.md"
        path_fitxer = NOTAS_DIR / nom_fitxer
        if path_fitxer.exists():
            return {"status": "exists", "filename": nom_fitxer, "path": str(path_fitxer.resolve())}
        if req.contingut:
            contingut = req.contingut
        else:
            contingut = f"""---
title: "{clean_stem}"
date: {avui}
tags: ["lectura"]
citekey: "@{clean_stem}"
---

# {clean_stem}

## Resum & Idees Clau

- 

## Cites i Anotacions

"""
        path_fitxer.write_text(contingut, encoding="utf-8")
        return {"status": "created", "filename": nom_fitxer, "path": str(path_fitxer.resolve())}

    slug = netejar_text_slug(titol_clean)
    tags_list = [netejar_text_slug(t) for t in req.tags.split() if t.strip()]
    str_tags = "_" + "_".join(tags_list) if tags_list else ""
    id_denote = ara.strftime("%Y%m%dT%H%M%S")
    nom_fitxer = f"{id_denote}--{slug}{str_tags}.md"
    path_fitxer = NOTAS_DIR / nom_fitxer

    contador = 1
    while path_fitxer.exists():
        id_temp = (ara.replace(second=(ara.second + contador) % 60)).strftime("%Y%m%dT%H%M%S")
        nom_fitxer = f"{id_temp}--{slug}{str_tags}.md"
        path_fitxer = NOTAS_DIR / nom_fitxer
        contador += 1

    if req.contingut:
        contingut = req.contingut
    else:
        contingut = f"""---
title: "{titol_clean}"
date: {avui}
tags: {json.dumps(tags_list, ensure_ascii=False)}
---

# {titol_clean}

"""
    path_fitxer.write_text(contingut, encoding="utf-8")
    return {
        "status": "created",
        "filename": nom_fitxer,
        "path": str(path_fitxer.resolve())
    }

@router.get("/notes/{filename}/connections")
async def obtenir_connexions(filename: str):
    """Calcula els retroenllaços (backlinks) i enllaços sortints (outlinks) d'una nota."""
    path_actual = NOTAS_DIR / filename
    if not path_actual.exists():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        with open(path_actual, "r", encoding="utf-8", errors="replace") as f:
            text_actual = f.read()

        id_actual = extraure_identificador_fitxer(filename).lower()
        stem_actual = Path(filename).stem.lower()
        nom_actual_lower = filename.lower()

        backlinks = []
        for p in NOTAS_DIR.glob("*"):
            if p.name == filename or p.name.startswith((".", "#")) or p.name.endswith("~") or not p.is_file():
                continue
            if p.suffix.lower() not in [".md", ".org"]:
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    c = f.read()
                if (id_actual and id_actual in c.lower()) or nom_actual_lower in c.lower() or stem_actual in c.lower():
                    backlinks.append({
                        "filename": p.name,
                        "title": _extreure_titol_nota(p, c[:1000])
                    })
            except Exception:
                continue

        patrons_enllac = [
            r'\[\[(?:denote:)?([^\]|\r\n]+)(?:\|[^\r\n\]]+)?(?:\]\[(.*?)\])?\]\]',
            r'\[([^\]]+)\]\(([^)]+\.(?:md|org))\)',
            r'\[@([a-zA-Z0-9_-]+)\]',
        ]
        enllacos_trobats = set()
        for pat in patrons_enllac:
            for m in re.finditer(pat, text_actual, re.IGNORECASE):
                target = m.group(1) if pat.startswith(r'\[\[') or pat.startswith(r'\[@') else m.group(2)
                target = Path(target).name.lower().strip()
                enllacos_trobats.add(target)
                if not target.startswith('@'):
                    enllacos_trobats.add(f"@{target}")
                else:
                    enllacos_trobats.add(target[1:])

        outlinks = []
        for p in NOTAS_DIR.glob("*"):
            if p.name == filename or p.suffix.lower() not in [".md", ".org"]:
                continue
            p_name_lower = p.name.lower()
            p_id = extraure_identificador_fitxer(p.name).lower()
            p_stem = p.stem.lower()

            if p_name_lower in enllacos_trobats or (p_id and p_id in enllacos_trobats) or p_stem in enllacos_trobats:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    c_target = f.read(1000)
                outlinks.append({
                    "filename": p.name,
                    "title": _extreure_titol_nota(p, c_target)
                })

        backlinks.sort(key=lambda x: x["filename"])
        outlinks.sort(key=lambda x: x["filename"])

        return {
            "filename": filename,
            "backlinks": backlinks,
            "outlinks": outlinks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/notes/{filename}/rescue-pdf")
async def rescatar_pdf_nota(filename: str):
    """
    Intenta recuperar el PDF d'una nota bibliogràfica (@citekey) des de ~/Downloads o fonts obertes.
    """
    path = NOTAS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        is_biblio = path.name.startswith("@")
        citekey = path.stem[1:] if is_biblio else path.stem
        target_pdf = FONTS_DIR / f"{citekey}.pdf"

        if target_pdf.is_file():
            return {
                "status": "ok",
                "message": f"El PDF {target_pdf.name} ja existeix a la biblioteca.",
                "filename": target_pdf.name
            }

        titol = _extreure_titol_nota(path, content[:1500])
        m_doi = re.search(r'doi:\s*["\']?([^"\'\n\r]+)["\']?', content, re.IGNORECASE)
        doi = m_doi.group(1).strip() if m_doi else ""
        m_pmid = re.search(r'pmid:?\s*["\']?(\d+)["\']?', content, re.IGNORECASE)
        pmid = m_pmid.group(1).strip() if m_pmid else (citekey[4:] if citekey.startswith("pmid") and citekey[4:].isdigit() else "")

        # 1. Intentar rescatar des de ~/Downloads
        trobat = check_and_import_from_downloads(target_pdf, title=titol, doi=doi, pmid=pmid)

        # 2. Si no es troba a Downloads i té DOI, provar descarregar via add-paper / Unpaywall
        if not trobat and doi:
            add_paper_cmd = shutil.which("add-paper") or str(Path.home() / ".local" / "bin" / "add-paper")
            proc = subprocess.run([add_paper_cmd, f"https://doi.org/{doi}"], capture_output=True, text=True, timeout=30)
            if target_pdf.is_file():
                trobat = True

        if trobat and target_pdf.is_file():
            try:
                if BIB_FILE_PATH.exists():
                    bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                    if f"{{{citekey}," in bib_text:
                        if "file =" in bib_text:
                            bib_text_nou = re.sub(
                                rf'(@\w+\s*\{{\s*{re.escape(citekey)},[\s\S]*?file\s*=\s*\{{)[^}}]+(\}})',
                                rf'\g<1>{str(target_pdf)}\g<2>',
                                bib_text
                            )
                        else:
                            bib_text_nou = re.sub(
                                rf'(@\w+\s*\{{\s*{re.escape(citekey)},)',
                                rf'\g<1>\n  file = {{{str(target_pdf)}}},',
                                bib_text
                            )
                        BIB_FILE_PATH.write_text(bib_text_nou, encoding="utf-8")
            except Exception as e_bib:
                logger.warning(f"Error actualitzant file a bibtex: {e_bib}")

            return {
                "status": "ok",
                "message": f"PDF rescatat correctament: {target_pdf.name}",
                "filename": target_pdf.name
            }
        else:
            return {
                "status": "not_found",
                "message": "No s'ha trobat cap PDF coincident a ~/Downloads ni en accés obert."
            }
    except Exception as e:
        logger.error(f"Error rescatant PDF per a {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# 2. PDF STREAMING & HIGHLIGHTS
# =====================================================================

@router.get("/pdf/stream")
async def servir_pdf(path: str = Query(..., description="Ruta absoluta o relativa del PDF")):
    """Serveix un fitxer PDF de forma segura per a ser renderitzat per PDF.js al navegador."""
    pdf_path = Path(urllib.parse.unquote(path)).resolve()

    if not pdf_path.exists() or not pdf_path.is_file():
        if (FONTS_DIR / pdf_path.name).exists():
            pdf_path = (FONTS_DIR / pdf_path.name).resolve()
        elif (NOTAS_DIR / pdf_path.name).exists():
            pdf_path = (NOTAS_DIR / pdf_path.name).resolve()
        else:
            raise HTTPException(status_code=404, detail="Fitxer PDF no trobat")

    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="El fitxer sol·licitat no és un PDF")

    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        content_disposition_type="inline",
        filename=pdf_path.name,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@router.get("/pdf/highlights")
async def obtenir_subratllats_pdf(path: str = Query(..., description="Ruta del PDF")):
    """Retorna tots els subratllats guardats per a un PDF concret."""
    pdf_path = str(Path(urllib.parse.unquote(path)).resolve())
    if not HIGHLIGHTS_FILE.exists():
        return {"highlights": []}

    try:
        with open(HIGHLIGHTS_FILE, "r", encoding="utf-8") as f:
            tots = json.load(f)
        return {"highlights": tots.get(pdf_path, [])}
    except Exception:
        return {"highlights": []}

@router.post("/pdf/highlights")
async def desar_subratllat_pdf(req: SaveHighlightRequest):
    """Guarda un nou subratllat persistent associat a un PDF."""
    pdf_path = str(Path(req.pdf_path).resolve())
    tots = {}
    if HIGHLIGHTS_FILE.exists():
        try:
            with open(HIGHLIGHTS_FILE, "r", encoding="utf-8") as f:
                tots = json.load(f)
        except Exception:
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
        with open(HIGHLIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(tots, f, ensure_ascii=False, indent=2)
        return {"status": "ok", "highlight": nou_item}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/pdf/highlights/{highlight_id}")
async def esborrar_subratllat_pdf(highlight_id: str, path: str = Query(...)):
    """Esborra un subratllat concret d'un PDF."""
    pdf_path = str(Path(urllib.parse.unquote(path)).resolve())
    if not HIGHLIGHTS_FILE.exists():
        return {"status": "ok"}

    try:
        with open(HIGHLIGHTS_FILE, "r", encoding="utf-8") as f:
            tots = json.load(f)
        if pdf_path in tots:
            tots[pdf_path] = [h for h in tots[pdf_path] if h.get("id") != highlight_id]
            with open(HIGHLIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(tots, f, ensure_ascii=False, indent=2)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# 3. BIBLIOTECA I FONTS
# =====================================================================

@router.get("/fonts/list")
async def llistar_fonts():
    """Llista els PDFs existents a la carpeta de PDFs de la biblioteca."""
    pdfs = []
    for p in FONTS_DIR.glob("*.pdf"):
        try:
            stat = p.stat()
            pdfs.append({
                "filename": p.name,
                "path": str(p.resolve()),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
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


# =====================================================================
# 4. CERCA ACADÈMICA FEDERADA & IMPORTACIÓ ZOTERLESS
# =====================================================================

@router.get("/academic/search")
async def cerca_academica_federada(
    q: str = Query(..., min_length=2),
    limit: int = 5,
    sources: Optional[str] = Query("all", description="Fonts separades per coma o 'all'.")
):
    """Cerca federada multilingüe a través del motor centralitzat d'academic-spain."""
    mcp_academic_dir = Path.home() / "Projects" / "mcp-server-academic-spain"
    if mcp_academic_dir.exists():
        import glob
        for sp in glob.glob(str(mcp_academic_dir / "venv/lib/python3.*/site-packages")):
            if sp not in sys.path:
                sys.path.insert(0, sp)
        if str(mcp_academic_dir) not in sys.path:
            sys.path.insert(0, str(mcp_academic_dir))

        try:
            from platforms import search_academic
            data = await search_academic(query=q, sources=sources, limit=limit, expand=True)
            results = []
            for paper in data.get("results", []):
                year_val = str(paper.year) if hasattr(paper, 'year') and paper.year else (paper.published_date[:4] if paper.published_date else "")
                results.append({
                    "title": paper.title,
                    "authors": paper.authors,
                    "year": year_val,
                    "date": paper.published_date or "",
                    "doi": paper.doi,
                    "url": paper.url or "",
                    "journal": paper.journal or "",
                    "abstract": paper.abstract,
                    "is_oa": bool(paper.pdf_url),
                    "oa_url": paper.pdf_url or "",
                    "sources": [s.strip() for s in paper.source.split(",")],
                    "extra": getattr(paper, 'extra', {}) or {},
                    "id": paper.doi or paper.url or paper.title
                })
            return {
                "query": q,
                "translations": data.get("translations", {}),
                "total": len(results),
                "results": results
            }
        except Exception as e:
            logger.error(f"Error in academic search: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error executant la cerca acadèmica: {e}")

    raise HTTPException(status_code=500, detail="Motor academic-spain no disponible")

@router.post("/academic/import")
async def importar_article_zoterless(req: AcademicImportRequest):
    """Descarrega el PDF, afegeix l'entrada a biblioteca.bib i crea la nota @citekey.md mitjançant add-paper."""
    add_paper_cmd = shutil.which("add-paper") or str(Path.home() / ".local" / "bin" / "add-paper")

    cmd = [add_paper_cmd]
    if req.vpn:
        cmd.append("--vpn")
    if req.pdf_url:
        cmd.extend(["--pdf-url", req.pdf_url.strip()])
    cmd.append(req.identifier.strip())

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        if proc.returncode == 0:
            return {
                "status": "ok",
                "message": "Article importat correctament a la biblioteca i notes",
                "output": output
            }
        else:
            return {
                "status": "error",
                "message": f"add-paper ha finalitzat amb codi {proc.returncode}",
                "output": output
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excepció executant add-paper: {e}")


# =====================================================================
# 5. PLEC WORKBENCH ACTIONS
# =====================================================================

@router.get("/plec/status")
async def plec_status():
    """Retorna l'estat i recompte de fitxers per a Plec Web."""
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

@router.post("/plec/open-external")
async def plec_open_external(req: PlecOpenExternalRequest):
    """Obre el PDF amb el visor natiu del sistema (Okular / xdg-open)."""
    p = Path(req.pdf)
    if not p.is_file():
        p = FONTS_DIR / req.pdf
    if not p.exists():
        raise HTTPException(status_code=404, detail="PDF no trobat")

    try:
        subprocess.Popen(["okular", str(p.resolve())])
        return {"status": "ok", "message": f"Obert {p.name} amb Okular"}
    except Exception:
        try:
            subprocess.Popen(["xdg-open", str(p.resolve())])
            return {"status": "ok", "message": f"Obert {p.name} amb xdg-open"}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"No s'ha pogut obrir el visor extern: {e2}")

@router.post("/plec/open-note-external")
async def plec_open_note_external(req: PlecOpenNoteExternalRequest):
    """Obre la nota amb l'editor natiu del sistema (Kate / Obsidian / xdg-open)."""
    p = Path(req.filename)
    if not p.is_file():
        p = NOTAS_DIR / req.filename
    if not p.exists():
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

@router.post("/plec/save-pdf")
async def plec_save_pdf(request: Request, filename: str = Query(..., description="Nom del fitxer PDF a desar")):
    """Desa les modificacions i anotacions del PDF directament sobre el fitxer físic original a disc."""
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Contingut binari buit")

    clean_name = Path(urllib.parse.unquote(filename)).name
    target_pdf = FONTS_DIR / clean_name
    if not target_pdf.exists():
        if (NOTAS_DIR / clean_name).exists():
            target_pdf = NOTAS_DIR / clean_name
        else:
            raise HTTPException(status_code=404, detail="Fitxer PDF no trobat a la biblioteca")

    try:
        target_pdf.write_bytes(content)
        return {
            "status": "ok",
            "message": f"PDF {target_pdf.name} desat correctament a disc",
            "size": len(content)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error escrivint PDF a disc: {e}")

@router.get("/plec/extract-highlights")
async def plec_extract_highlights(pdf: str = Query(..., description="Nom o ruta del fitxer PDF")):
    """Extreu tots els subratllats i anotacions d'un PDF mitjançant PyMuPDF."""
    clean_name = Path(urllib.parse.unquote(pdf)).name
    target_pdf = FONTS_DIR / clean_name
    if not target_pdf.exists():
        if (NOTAS_DIR / clean_name).exists():
            target_pdf = NOTAS_DIR / clean_name
        elif Path(pdf).is_file():
            target_pdf = Path(pdf)
        else:
            raise HTTPException(status_code=404, detail="Fitxer PDF no trobat")

    try:
        import fitz
        doc = fitz.open(str(target_pdf))
        highlights = []
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
        doc.close()
        return {
            "status": "ok",
            "pdf": clean_name,
            "total": len(highlights),
            "highlights": highlights
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extreient subratllats: {e}")

@router.post("/plec/create-companion-note")
async def plec_create_companion_note(req: PlecCompanionNoteRequest):
    """Crea una nota associada a un PDF existent si encara no en té."""
    pdf_filename = req.pdf
    p = Path(pdf_filename)
    stem = p.stem
    clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '', stem)

    candidats = [f"@{clean_stem}.md", f"@{clean_stem}.org", f"{clean_stem}.md", f"@{stem}.md", f"{stem}.md"]
    for c in candidats:
        if (NOTAS_DIR / c).exists():
            return {"status": "exists", "note": c, "message": "La nota ja existeix"}

    real_pdf = FONTS_DIR / pdf_filename if not p.is_file() else p
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
    nota_path = NOTAS_DIR / nota_name
    avui = datetime.now().strftime("%Y-%m-%d")

    contingut = f"""---
title: "{titol}"
date: {avui}
tags: ["lectura"]
citekey: "@{clean_stem}"
pdf: "{pdf_filename}"
---

# {titol}

## Metadades
- **Fitxer PDF:** [[{pdf_filename}]]
- **Data d'obertura:** {avui}

## Resum & Idees Clau

- 

## Cites i Anotacions

"""
    nota_path.write_text(contingut, encoding="utf-8")
    return {"status": "created", "note": nota_name, "message": "Nota creada correctament"}

@router.post("/plec/upload-pdf")
async def plec_upload_pdf(request: Request, filename: str = Query(..., description="Nom del fitxer PDF")):
    """Puja un PDF, el desa a FONTS_DIR, n'extrau metadades i crea la seua nota Markdown associada."""
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Contingut buit")

    try:
        import fitz
    except ImportError:
        fitz = None

    raw_name = Path(urllib.parse.unquote(filename)).name
    clean_name = re.sub(r'[^\w\-_\.]', '_', raw_name)
    if not clean_name.lower().endswith(".pdf"):
        clean_name += ".pdf"

    dest_pdf = FONTS_DIR / clean_name
    dest_pdf.write_bytes(content)

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
    nota_path = NOTAS_DIR / nota_name
    avui = datetime.now().strftime("%Y-%m-%d")

    if not nota_path.exists():
        nota_content = f"""---
title: "{titol}"
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
        nota_path.write_text(nota_content, encoding="utf-8")

    return {
        "status": "ok",
        "pdf": clean_name,
        "note": nota_name,
        "title": titol,
        "pages": num_pagines,
        "message": f"PDF {clean_name} ingerit i nota {nota_name} creada"
    }

@router.post("/plec/ingest-url")
async def plec_ingest_url(req: PlecIngestUrlRequest):
    """Descarrega un PDF d'un enllaç URL, el desa a FONTS_DIR i crea la nota associada."""
    import urllib.request
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL buida")

    try:
        parsed = urllib.parse.urlparse(url)
        path_leaf = Path(parsed.path).name
        if not path_leaf or not path_leaf.lower().endswith(".pdf"):
            path_leaf = f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        clean_name = re.sub(r'[^\w\-_\.]', '_', path_leaf)
        dest_pdf = FONTS_DIR / clean_name

        req_headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}
        request_obj = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(request_obj, timeout=30) as response, open(dest_pdf, "wb") as out_file:
            out_file.write(response.read())

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
        nota_path = NOTAS_DIR / nota_name
        avui = datetime.now().strftime("%Y-%m-%d")

        if not nota_path.exists():
            nota_content = f"""---
title: "{titol}"
date: {avui}
tags: ["web", "lectura"]
citekey: "@{clean_stem}"
pdf: "{clean_name}"
url: "{url}"
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
            nota_path.write_text(nota_content, encoding="utf-8")

        return {
            "status": "ok",
            "pdf": clean_name,
            "note": nota_name,
            "title": titol,
            "pages": num_pagines
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error descarregant el PDF: {e}")


# =====================================================================
# 6. COL·LECCIONS VIRTUALS
# =====================================================================

def _carregar_colleccions() -> dict:
    if not COLLECTIONS_FILE.exists():
        return {"collections": []}
    try:
        with open(COLLECTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "collections" not in data:
                return {"collections": []}
            return data
    except Exception:
        return {"collections": []}

def _desar_colleccions(dades: dict):
    COLLECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COLLECTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(dades, f, indent=2, ensure_ascii=False)
    try:
        subprocess.Popen(["kate-sync-project", "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

@router.get("/plec/collections")
@router.get("/collections")
async def get_plec_collections():
    """Retorna l'arbre de col·leccions virtuals de Plec (assegurant la sincronització de PDFs associats a notes)."""
    dades = _carregar_colleccions()
    modificat = False
    for col in dades.get("collections", []):
        if "notes" not in col:
            col["notes"] = []
        if "pdfs" not in col:
            col["pdfs"] = []
        for n in col["notes"]:
            for p in obtenir_pdfs_associats_nota(n):
                if p not in col["pdfs"]:
                    col["pdfs"].append(p)
                    modificat = True
    if modificat:
        _desar_colleccions(dades)
    return dades

@router.post("/plec/collections")
@router.post("/collections")
async def save_plec_collections(req: GuardarColleccionsRequest):
    """Guarda l'estat complet de les col·leccions."""
    data = {"collections": req.collections}
    _desar_colleccions(data)
    return {"status": "ok", "collections": req.collections}

@router.post("/plec/collections/create")
async def create_plec_collection(req: NovaColleccioRequest):
    """Crea una nova col·lecció virtual."""
    dades = _carregar_colleccions()
    import time
    col_id = f"col_{int(time.time() * 1000)}"
    nova = {
        "id": col_id,
        "name": req.name.strip(),
        "parent_id": req.parent_id if req.parent_id else None,
        "icon": req.icon or "fa-folder",
        "notes": [],
        "pdfs": [],
        "created_at": datetime.now().isoformat()
    }
    dades["collections"].append(nova)
    _desar_colleccions(dades)
    return {"status": "ok", "collection": nova}

@router.put("/plec/collections/{col_id}")
async def update_plec_collection(col_id: str, req: ActualitzarColleccioRequest):
    """Actualitza el nom o el pare d'una col·lecció."""
    dades = _carregar_colleccions()
    trobat = False
    for col in dades["collections"]:
        if col.get("id") == col_id:
            if req.name is not None:
                col["name"] = req.name.strip()
            if req.parent_id is not None:
                col["parent_id"] = req.parent_id if req.parent_id != "" else None
            if req.icon is not None:
                col["icon"] = req.icon
            trobat = True
            break
    if not trobat:
        raise HTTPException(status_code=404, detail="Col·lecció no trobada")
    _desar_colleccions(dades)
    return {"status": "ok", "col_id": col_id}

@router.delete("/plec/collections/{col_id}")
async def delete_plec_collection(col_id: str):
    """Elimina una col·lecció virtual sense tocar els fitxers de disc."""
    dades = _carregar_colleccions()
    nova_llista = []
    parent_of_deleted = None
    for col in dades["collections"]:
        if col.get("id") == col_id:
            parent_of_deleted = col.get("parent_id")
        else:
            nova_llista.append(col)

    for col in nova_llista:
        if col.get("parent_id") == col_id:
            col["parent_id"] = parent_of_deleted

    dades["collections"] = nova_llista
    _desar_colleccions(dades)
    return {"status": "ok", "deleted": col_id}

@router.post("/plec/collections/{col_id}/add-item")
async def add_item_to_collection(col_id: str, req: ItemColleccioRequest):
    """Afegeix una nota o PDF a una col·lecció (i sincronitza automàticament els fitxers associats)."""
    dades = _carregar_colleccions()
    col = next((c for c in dades["collections"] if c.get("id") == col_id), None)
    if not col:
        raise HTTPException(status_code=404, detail="Col·lecció no trobada")

    if "notes" not in col:
        col["notes"] = []
    if "pdfs" not in col:
        col["pdfs"] = []

    if req.type == "note":
        if req.filename not in col["notes"]:
            col["notes"].append(req.filename)
        # Sincronitzar PDF(s) associat(s) a la nota
        for p in obtenir_pdfs_associats_nota(req.filename):
            if p not in col["pdfs"]:
                col["pdfs"].append(p)
    else:
        if req.filename not in col["pdfs"]:
            col["pdfs"].append(req.filename)
        # Sincronitzar nota associada al PDF si existeix
        for n in obtenir_notes_associades_pdf(req.filename):
            if n not in col["notes"]:
                col["notes"].append(n)

    _desar_colleccions(dades)
    return {"status": "ok", "collection": col}

@router.post("/plec/collections/{col_id}/remove-item")
async def remove_item_from_collection(col_id: str, req: ItemColleccioRequest):
    """Elimina una nota o PDF d'una col·lecció virtual."""
    dades = _carregar_colleccions()
    col = next((c for c in dades["collections"] if c.get("id") == col_id), None)
    if not col:
        raise HTTPException(status_code=404, detail="Col·lecció no trobada")

    if "notes" not in col:
        col["notes"] = []
    if "pdfs" not in col:
        col["pdfs"] = []

    if req.type == "note":
        if req.filename in col["notes"]:
            col["notes"].remove(req.filename)
            # Eliminar PDFs associats només si cap altra nota de la carpeta els necessita
            associated_pdfs = obtenir_pdfs_associats_nota(req.filename)
            other_pdfs = set()
            for other_n in col.get("notes", []):
                for p in obtenir_pdfs_associats_nota(other_n):
                    other_pdfs.add(p)
            for p in associated_pdfs:
                if p not in other_pdfs and p in col["pdfs"]:
                    col["pdfs"].remove(p)
    else:
        if req.filename in col["pdfs"]:
            col["pdfs"].remove(req.filename)

    _desar_colleccions(dades)
    return {"status": "ok", "collection": col}

@router.post("/plec/collections/set-item-collections")
async def set_item_collections(req: SetItemCollectionsRequest):
    """Assigna una nota o PDF exactament a les col·leccions indicades."""
    dades = _carregar_colleccions()
    target_ids = set(req.collection_ids)

    if req.type == "note":
        associated_pdfs = obtenir_pdfs_associats_nota(req.filename)
        for col in dades["collections"]:
            cid = col.get("id")
            if "notes" not in col:
                col["notes"] = []
            if "pdfs" not in col:
                col["pdfs"] = []

            if cid in target_ids:
                if req.filename not in col["notes"]:
                    col["notes"].append(req.filename)
                for p in associated_pdfs:
                    if p not in col["pdfs"]:
                        col["pdfs"].append(p)
            else:
                if req.filename in col["notes"]:
                    col["notes"].remove(req.filename)
                    # Treure PDFs associats si cap altra nota de la carpeta els requereix
                    other_pdfs = set()
                    for other_n in col.get("notes", []):
                        for p in obtenir_pdfs_associats_nota(other_n):
                            other_pdfs.add(p)
                    for p in associated_pdfs:
                        if p not in other_pdfs and p in col["pdfs"]:
                            col["pdfs"].remove(p)
    else:
        associated_notes = obtenir_notes_associades_pdf(req.filename)
        for col in dades["collections"]:
            cid = col.get("id")
            if "notes" not in col:
                col["notes"] = []
            if "pdfs" not in col:
                col["pdfs"] = []

            if cid in target_ids:
                if req.filename not in col["pdfs"]:
                    col["pdfs"].append(req.filename)
                for n in associated_notes:
                    if n not in col["notes"]:
                        col["notes"].append(n)
            else:
                if req.filename in col["pdfs"]:
                    col["pdfs"].remove(req.filename)

    _desar_colleccions(dades)
    return {
        "status": "ok",
        "filename": req.filename,
        "collections": list(target_ids),
        "collections_data": dades["collections"]
    }

@router.post("/plec/collections/{col_id}/sync-notebooklm")
async def sync_collection_notebooklm(col_id: str):
    """Crea o sincronitza un quadern de NotebookLM per a la col·lecció virtual."""
    dades = _carregar_colleccions()
    col = next((c for c in dades.get("collections", []) if c.get("id") == col_id), None)
    if not col:
        raise HTTPException(status_code=404, detail="Col·lecció no trobada")

    # Assegurar que qualsevol PDF associat a les notes de la col·lecció està inclòs a col["pdfs"]
    if "pdfs" not in col:
        col["pdfs"] = []
    modificat = False
    for n in col.get("notes", []):
        for p in obtenir_pdfs_associats_nota(n):
            if p not in col["pdfs"]:
                col["pdfs"].append(p)
                modificat = True
    if modificat:
        _desar_colleccions(dades)


    col_name = col.get("name", "").strip()
    pdf_list = col.get("pdfs", [])
    notebook_id = col.get("notebook_id")

    async def run_nlm(*args):
        nlm_bin = shutil.which("nlm") or "/home/casimir/.local/bin/nlm"
        proc = await asyncio.create_subprocess_exec(
            nlm_bin, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode == 0, stdout.decode().strip(), stderr.decode().strip()

    # Si no té quadern, crear-lo
    if not notebook_id:
        ok, out, err = await run_nlm("notebook", "create", col_name, "--json")
        if not ok:
            ok, out, err = await run_nlm("create", "notebook", col_name, "--json")
        if ok:
            try:
                data = json.loads(out)
                notebook_id = data.get("id") or data.get("notebook_id")
                if notebook_id:
                    col["notebook_id"] = notebook_id
                    _desar_colleccions(dades)
            except Exception:
                pass

    if not notebook_id:
        raise HTTPException(status_code=500, detail="No s'ha pogut crear o associar el quadern de NotebookLM")

    # Obtenir fonts ja presents
    ok, out, _ = await run_nlm("source", "list", notebook_id, "--json")
    existing_titles = set()
    if ok:
        try:
            src_list = json.loads(out)
            for s in src_list:
                existing_titles.add(s.get("title", ""))
        except Exception:
            pass

    # Pujar els PDFs nous
    afegits = 0
    for pdf_name in pdf_list:
        if pdf_name in existing_titles:
            continue
        p = FONTS_DIR / pdf_name
        if p.exists():
            ok_add, _, _ = await run_nlm("source", "add", notebook_id, "--file", str(p.resolve()))
            if ok_add:
                afegits += 1

    return {
        "status": "ok",
        "notebook_id": notebook_id,
        "message": f"Quadern '{col_name}' sincronitzat ({afegits} fonts noves afegides)"
    }


# =====================================================================
# 7. NOTEBOOKLM IA RAG STUDIO
# =====================================================================

async def _exec_nlm_cli(args: List[str], parse_json: bool = True):
    nlm_bin = shutil.which("nlm") or "/home/casimir/.local/bin/nlm"
    try:
        proc = await asyncio.create_subprocess_exec(
            nlm_bin, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err_msg = stderr_str if stderr_str else stdout_str
            return False, err_msg, None

        if parse_json:
            try:
                raw = stdout_str
                s_obj = raw.find('{')
                s_arr = raw.find('[')
                s_idx = -1
                if s_obj != -1 and (s_arr == -1 or s_obj < s_arr):
                    s_idx = s_obj
                    e_idx = raw.rfind('}')
                elif s_arr != -1:
                    s_idx = s_arr
                    e_idx = raw.rfind(']')
                else:
                    e_idx = -1

                if s_idx != -1 and e_idx != -1 and e_idx >= s_idx:
                    json_str = raw[s_idx:e_idx+1]
                    parsed = json.loads(json_str)
                    return True, stdout_str, parsed
                else:
                    parsed = json.loads(stdout_str)
                    return True, stdout_str, parsed
            except Exception as e:
                return False, f"Error parsejant JSON de nlm ({e}):\n{stdout_str}", None
        return True, stdout_str, stdout_str
    except FileNotFoundError:
        return False, "La comanda 'nlm' (NotebookLM CLI) no està instal·lada o no és al PATH.", None
    except Exception as e:
        return False, str(e), None

@router.get("/nlm/notebooks")
async def nlm_llistar_quaderns():
    """Llista els quaderns disponibles a NotebookLM."""
    ok, text, data = await _exec_nlm_cli(["list", "notebooks", "--json"], parse_json=True)
    if not ok:
        ok, text, data = await _exec_nlm_cli(["notebook", "list", "--json"], parse_json=True)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"notebooks": data}

@router.get("/nlm/sources")
async def nlm_llistar_fonts(notebook_id: str = Query(...)):
    """Llista les fonts documentals d'un quadern concret de NotebookLM."""
    ok, text, data = await _exec_nlm_cli(["source", "list", notebook_id, "--json"], parse_json=True)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"notebook_id": notebook_id, "sources": data}

@router.post("/nlm/query")
async def nlm_consultar(req: NlmQueryRequest):
    """Executa una consulta RAG contra un quadern o font de NotebookLM."""
    args = ["query", "notebook", req.notebook_id, req.query, "--json"]
    if req.conversation_id:
        args.extend(["-c", req.conversation_id])
    if req.source_ids and len(req.source_ids) > 0:
        args.extend(["-s", ",".join(req.source_ids)])
    elif req.source_id:
        args.extend(["-s", req.source_id])

    ok, text, data = await _exec_nlm_cli(args, parse_json=True)
    if not ok:
        raise HTTPException(status_code=500, detail=text)

    sources_map = {}
    ok_src, _, src_data = await _exec_nlm_cli(["source", "list", req.notebook_id, "--json"], parse_json=True)
    if ok_src and isinstance(src_data, list):
        for s in src_data:
            s_id = s.get("id")
            s_title = s.get("title", "")
            citekey = Path(s_title).stem if s_title.endswith(".pdf") else s_title
            sources_map[s_id] = {
                "id": s_id,
                "title": s_title,
                "citekey": citekey
            }

    answer = data.get("answer") if isinstance(data, dict) else text
    conversation_id = data.get("conversation_id") if isinstance(data, dict) else None
    citations = data.get("citations", {}) if isinstance(data, dict) else {}
    references = data.get("references", []) if isinstance(data, dict) else []

    # Resolució de pàgina exacta mitjançant PyMuPDF
    citation_details = {}
    opened_docs = {}
    try:
        import fitz
        for ref in references:
            c_num = str(ref.get("citation_number", ""))
            s_id = ref.get("source_id", "")
            cited_text = ref.get("cited_text", "")
            src_info = sources_map.get(s_id, {})
            title = src_info.get("title", "")
            citekey = src_info.get("citekey", "")

            page_num = 1
            if title:
                target_pdf = None
                for p_cand in [FONTS_DIR / title, NOTAS_DIR / title, FONTS_DIR / f"{title}.pdf", NOTAS_DIR / f"{title}.pdf"]:
                    if p_cand.exists() and p_cand.is_file():
                        target_pdf = p_cand
                        break
                if not target_pdf:
                    target_stem = Path(title).stem.lower()
                    for cand in FONTS_DIR.glob("*.pdf"):
                        if cand.stem.lower() == target_stem:
                            target_pdf = cand
                            break

                if target_pdf:
                    try:
                        doc = opened_docs.get(str(target_pdf))
                        if not doc:
                            doc = fitz.open(str(target_pdf))
                            opened_docs[str(target_pdf)] = doc

                        if cited_text:
                            clean_cite = re.sub(r'[-\s]+', ' ', cited_text).lower().strip()
                            words = [w for w in clean_cite.split() if len(w) > 2]
                            chunks = [' '.join(words[i:i+4]) for i in range(0, min(len(words)-3, 16), 2)]
                            found = False
                            for p_idx in range(len(doc)):
                                p_text = re.sub(r'[-\s]+', ' ', doc[p_idx].get_text()).lower()
                                for chunk in chunks:
                                    if chunk in p_text:
                                        page_num = p_idx + 1
                                        found = True
                                        break
                                if found:
                                    break
                    except Exception as err_fitz:
                        logger.warning(f"Avís fitz cercant pàgina a {title}: {err_fitz}")

            ref["page"] = page_num
            ref["citekey"] = citekey
            ref["pdf"] = title

            if c_num:
                citation_details[c_num] = {
                    "source_id": s_id,
                    "citation_number": c_num,
                    "page": page_num,
                    "citekey": citekey,
                    "pdf": title,
                    "cited_text": cited_text
                }
    except Exception as e_proc:
        logger.warning(f"Avís resolent citacions NLM: {e_proc}")
    finally:
        for doc in opened_docs.values():
            try:
                doc.close()
            except:
                pass

    return {
        "status": "ok",
        "answer": answer,
        "question": req.query,
        "conversation_id": conversation_id,
        "citations": citations,
        "citation_details": citation_details,
        "references": references,
        "sources": sources_map
    }

@router.post("/nlm/sources/add")
async def nlm_afegir_url(req: NlmAddUrlRequest):
    """Afegeix una URL com a nova font a un quadern de NotebookLM."""
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="La URL ha de començar per http:// o https://")

    ok, text, _ = await _exec_nlm_cli(["source", "add", req.notebook_id, "--url", url], parse_json=False)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"status": "ok", "message": text}

@router.post("/nlm/delete")
async def nlm_esborrar_element(req: NlmDeleteRequest):
    """Esborra un quadern o una font de NotebookLM."""
    if req.target_type == "notebook":
        args = ["notebook", "delete", req.item_id, "--yes"]
    elif req.target_type == "source":
        if not req.notebook_id:
            raise HTTPException(status_code=400, detail="Cal el notebook_id per esborrar una font")
        args = ["source", "delete", req.notebook_id, req.item_id, "--yes"]
    else:
        raise HTTPException(status_code=400, detail="Tipus d'element no reconegut")

    ok, text, _ = await _exec_nlm_cli(args, parse_json=False)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"status": "ok", "message": text}

@router.post("/nlm/synthesize")
async def nlm_guardar_sintesi(req: NlmSintesiRequest):
    """Injecta una síntesi estructurada de NotebookLM a la nota especificada."""
    path = NOTAS_DIR / req.fitxer_desti
    if not path.exists():
        raise HTTPException(status_code=404, detail="Nota de destinació no trobada")

    ara_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    bloc = f"""

### 🤖 Síntesi NotebookLM ({ara_str})
**Àmbit / Origen:** {req.origen}
**Pregunta:** {req.pregunta}

> {req.resposta.strip()}
"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(bloc)
        return {"status": "ok", "message": f"Síntesi injectada a {req.fitxer_desti}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
