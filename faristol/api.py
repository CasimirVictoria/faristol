"""
API FastAPI per a Faristol - Entorn de lectura, anotació i síntesi bibliogràfica.
"""
import os
import sys
import re
import json
import glob
import shutil
import asyncio
import time
import logging
import urllib.parse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from faristol.config import (
    NOTAS_DIR,
    FONTS_DIR,
    BIB_FILE_PATH,
    PAPERERA_DIR,
    COLLECTIONS_FILE,
    HIGHLIGHTS_FILE
)
from faristol.utils import (
    enviar_a_la_papelera,
    netejar_text_slug,
    detectar_tots_els_pdfs,
    extraure_pagines_nota,
    extraure_identificador_fitxer,
    extraure_tags_nota,
    check_and_import_from_downloads,
    is_valid_pdf_content,
    obtenir_pdfs_associats_nota,
    obtenir_notes_associades_pdf
)
from faristol.bibtex import GestorBibtex
from faristol.fsatomic import escriure_atomic, bloqueig, llegir_json_segur
import ipaddress
import socket

logger = logging.getLogger("faristol.api")
router = APIRouter(prefix="/api")

def _ruta_segura_nota(filename: str) -> Path:
    """
    Valida i resol un nom de fitxer per a NOTAS_DIR.
    Rebutja caràcters perillosos, separadors de directori, '..', i enllaços que n'escapen.
    """
    if not filename or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Nom de fitxer no proporcionat")
    
    clean_name = urllib.parse.unquote(filename).strip()
    if "/" in clean_name or "\\" in clean_name or "\x00" in clean_name or clean_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Nom de fitxer no vàlid (separadors o traversal no permesos)")
    
    path_final = (NOTAS_DIR / clean_name).resolve()
    if not path_final.is_relative_to(NOTAS_DIR.resolve()) or path_final.parent != NOTAS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Ruta fora del directori de notes permès")
    
    return path_final

def _document_segur(ref: str) -> Path:
    """
    Valida i resol un fitxer de document (PDF o HTML).
    Permet nom de fitxer o ruta absoluta, però verifica estrictament que
    el fitxer resolt resideixi DINS de FONTS_DIR o NOTAS_DIR.
    """
    if not ref or not isinstance(ref, str):
        raise HTTPException(status_code=400, detail="Ruta de document buida")
    
    clean_ref = urllib.parse.unquote(ref).strip()
    if "\x00" in clean_ref:
        raise HTTPException(status_code=400, detail="Caràcter nul no permès")
    
    p = Path(clean_ref)
    candidats = []
    
    if p.is_absolute():
        candidats.append(p.resolve())
    else:
        parts = p.parts
        if ".." in parts:
            raise HTTPException(status_code=400, detail="Traversal ('..') no permès")
        candidats.append((FONTS_DIR / p.name).resolve())
        candidats.append((NOTAS_DIR / p.name).resolve())
    
    for cand in candidats:
        if cand.is_relative_to(FONTS_DIR.resolve()) or cand.is_relative_to(NOTAS_DIR.resolve()):
            if cand.is_file():
                return cand
    
    raise HTTPException(status_code=404, detail="Document no trobat a la biblioteca o notes")

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

class FaristolCompanionNoteRequest(BaseModel):
    pdf: str

class FaristolOpenExternalRequest(BaseModel):
    pdf: str

class FaristolOpenNoteExternalRequest(BaseModel):
    filename: str

class FaristolIngestUrlRequest(BaseModel):
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
            has_pdf_doc = any(Path(p[1]).suffix.lower() == ".pdf" for p in pdfs)
            has_html_doc = any(Path(p[1]).suffix.lower() in [".html", ".htm"] for p in pdfs)
            doc_format = "pdf" if has_pdf_doc else ("html" if has_html_doc else "")

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
                "has_pdf_doc": has_pdf_doc,
                "has_html_doc": has_html_doc,
                "doc_format": doc_format,
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
    path = _ruta_segura_nota(filename)
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
    """Guarda els canvis en una nota existent de forma atòmica i protegida contra concurrència."""
    path = _ruta_segura_nota(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        with bloqueig(path):
            escriure_atomic(path, req.contingut)

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
    """
    Mou la nota i els seus documents associats (PDF/HTML) a la subcarpeta Paperera de la biblioteca,
    i arxiva la seua entrada a biblioteca.bib.
    """
    path = _ruta_segura_nota(filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada o no és un fitxer")

    trash_notes_dir = PAPERERA_DIR / "notes"
    trash_docs_dir = PAPERERA_DIR / "documents"
    trash_notes_dir.mkdir(parents=True, exist_ok=True)
    trash_docs_dir.mkdir(parents=True, exist_ok=True)

    # 1. Detectar documents associats abans de moure
    associated_docs = []
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        trobats = detectar_tots_els_pdfs(path, content)
        for _, p, _ in trobats:
            if p.is_file() and p.name not in associated_docs:
                associated_docs.append(p.name)
    except Exception as e:
        logger.warning(f"Error detectant documents associats abans de moure a la paperera: {e}")

    # Comprovació canònica addicional (@citekey.md -> citekey.html / citekey.pdf)
    stem = path.stem[1:] if path.name.startswith("@") else path.stem
    for ext in [".html", ".pdf"]:
        c_doc = FONTS_DIR / f"{stem}{ext}"
        if c_doc.is_file() and c_doc.name not in associated_docs:
            associated_docs.append(c_doc.name)

    # 2. Moure la nota a Paperera/notes/
    dest_note = trash_notes_dir / path.name
    try:
        shutil.move(str(path), str(dest_note))
    except Exception as e:
        logger.error(f"Error movent nota a la paperera: {e}")
        raise HTTPException(status_code=500, detail=f"No s'ha pogut moure la nota a la paperera: {e}")

    # 3. Moure els documents associats a Paperera/documents/
    for doc_name in associated_docs:
        src_doc = FONTS_DIR / doc_name
        if src_doc.is_file():
            dest_doc = trash_docs_dir / doc_name
            try:
                shutil.move(str(src_doc), str(dest_doc))
            except Exception as e_doc:
                logger.warning(f"Error movent document associat {doc_name} a Paperera: {e_doc}")

    # 4. Comentar o arxivar l'entrada a biblioteca.bib si és una cita bibliogràfica
    if path.name.startswith("@") and BIB_FILE_PATH.exists():
        citekey = path.stem[1:]
        try:
            with bloqueig(BIB_FILE_PATH):
                bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                patro_ent = re.compile(rf'(@\w+\s*\{{\s*{re.escape(citekey)}\s*,[\s\S]*?\n\s*\}})(?=\s*@|\s*\Z)', re.MULTILINE)
                m_ent = patro_ent.search(bib_text)
                if m_ent:
                    ent_original = m_ent.group(1)
                    # Comentar cada línia amb % TRASH:
                    ent_comentada = "\n".join(f"% TRASH: {line}" for line in ent_original.splitlines())
                    bib_nou = bib_text[:m_ent.start()] + ent_comentada + bib_text[m_ent.end():]
                    escriure_atomic(BIB_FILE_PATH, bib_nou, backup=True)
        except Exception as e_bib:
            logger.warning(f"Error comentant entrada BibTeX a biblioteca.bib: {e_bib}")

    return {"status": "ok", "message": f"Nota {filename} i documents associats moguts a la paperera"}

@router.get("/trash/items")
async def llistar_paperera():
    """Llista tots els elements continguts a la Paperera de la biblioteca."""
    trash_notes_dir = PAPERERA_DIR / "notes"
    trash_docs_dir = PAPERERA_DIR / "documents"
    trash_notes_dir.mkdir(parents=True, exist_ok=True)
    trash_docs_dir.mkdir(parents=True, exist_ok=True)

    items = []
    # Notes a la paperera
    for p in trash_notes_dir.iterdir():
        if p.is_file() and p.suffix.lower() in [".md", ".org"]:
            try:
                stat = p.stat()
                capcalera = p.read_text(encoding="utf-8", errors="ignore")[:1500]
                titol = _extreure_titol_nota(p, capcalera)
                
                # Detectar documents a la paperera
                stem = p.stem[1:] if p.name.startswith("@") else p.stem
                docs = []
                for ext in [".pdf", ".html"]:
                    cand = trash_docs_dir / f"{stem}{ext}"
                    if cand.is_file():
                        docs.append(cand.name)

                items.append({
                    "filename": p.name,
                    "title": titol,
                    "mtime": stat.st_mtime,
                    "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_biblio": p.name.startswith("@"),
                    "documents": docs,
                    "type": "note"
                })
            except Exception:
                continue

    # Documents orfes a la paperera (que no tinguen nota)
    notes_stems = {it["filename"].replace(".md", "").replace(".org", "").lstrip("@") for it in items}
    for d in trash_docs_dir.iterdir():
        if d.is_file() and d.stem not in notes_stems:
            try:
                stat = d.stat()
                items.append({
                    "filename": d.name,
                    "title": d.name,
                    "mtime": stat.st_mtime,
                    "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_biblio": False,
                    "documents": [d.name],
                    "type": "doc"
                })
            except Exception:
                continue

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items, "total": len(items)}

@router.post("/trash/restore/{filename}")
async def restaurar_de_paperera(filename: str):
    """Restaura un element de la paperera tornant-lo a les carpetes actives de la biblioteca."""
    trash_notes_dir = PAPERERA_DIR / "notes"
    trash_docs_dir = PAPERERA_DIR / "documents"
    
    src_note = trash_notes_dir / filename
    src_doc = trash_docs_dir / filename

    if src_note.is_file():
        dest_note = NOTAS_DIR / filename
        shutil.move(str(src_note), str(dest_note))
        
        # Restaurar documents associats
        stem = src_note.stem[1:] if src_note.name.startswith("@") else src_note.stem
        for ext in [".pdf", ".html"]:
            cand = trash_docs_dir / f"{stem}{ext}"
            if cand.is_file():
                shutil.move(str(cand), str(FONTS_DIR / cand.name))

        # Descomentar a biblioteca.bib si correspon
        if src_note.name.startswith("@") and BIB_FILE_PATH.exists():
            citekey = src_note.stem[1:]
            try:
                with bloqueig(BIB_FILE_PATH):
                    bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                    patro_com = re.compile(rf'((?:% TRASH: [^\n]*\n?)+)', re.MULTILINE)
                    
                    def restore_match(m):
                        block = m.group(1)
                        if f"{{{citekey}," in block:
                            lines = block.splitlines()
                            restored = "\n".join(l.replace("% TRASH: ", "", 1) if l.startswith("% TRASH: ") else l for l in lines)
                            return restored + "\n"
                        return block

                    bib_nou = patro_com.sub(restore_match, bib_text)
                    if bib_nou != bib_text:
                        escriure_atomic(BIB_FILE_PATH, bib_nou, backup=True)
            except Exception as e_bib:
                logger.warning(f"Error restaurant entrada BibTeX: {e_bib}")

        return {"status": "ok", "message": f"{filename} restaurat correctament"}

    elif src_doc.is_file():
        dest_doc = FONTS_DIR / filename
        shutil.move(str(src_doc), str(dest_doc))
        return {"status": "ok", "message": f"{filename} restaurat correctament"}

    raise HTTPException(status_code=404, detail="Fitxer no trobat a la paperera")

@router.delete("/trash/permanent/{filename}")
async def eliminar_permanent_paperera(filename: str):
    """Elimina definitivament un fitxer i els seus adjunts de la Paperera."""
    trash_notes_dir = PAPERERA_DIR / "notes"
    trash_docs_dir = PAPERERA_DIR / "documents"

    src_note = trash_notes_dir / filename
    src_doc = trash_docs_dir / filename

    eliminat = False
    if src_note.is_file():
        stem = src_note.stem[1:] if src_note.name.startswith("@") else src_note.stem
        for ext in [".pdf", ".html"]:
            cand = trash_docs_dir / f"{stem}{ext}"
            if cand.is_file():
                cand.unlink(missing_ok=True)
        src_note.unlink(missing_ok=True)
        eliminat = True

        # Netejar completament el bloc comentat a biblioteca.bib si existia
        if src_note.name.startswith("@") and BIB_FILE_PATH.exists():
            citekey = src_note.stem[1:]
            try:
                with bloqueig(BIB_FILE_PATH):
                    bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                    patro_com = re.compile(rf'\n*(?:% TRASH: [^\n]*\n?)+', re.MULTILINE)
                    def remove_trash_block(m):
                        block = m.group(0)
                        if f"{{{citekey}," in block:
                            return ""
                        return block
                    bib_nou = patro_com.sub(remove_trash_block, bib_text)
                    if bib_nou != bib_text:
                        escriure_atomic(BIB_FILE_PATH, bib_nou, backup=True)
            except Exception as e_bib:
                logger.warning(f"Error netejant entrada esborrada a bibtex: {e_bib}")

    elif src_doc.is_file():
        src_doc.unlink(missing_ok=True)
        eliminat = True

    if not eliminat:
        raise HTTPException(status_code=404, detail="Fitxer no trobat a la paperera")

    return {"status": "ok", "message": f"{filename} eliminat definitivament"}

@router.post("/trash/empty")
async def buidar_paperera():
    """Buida completament la Paperera de la biblioteca."""
    trash_notes_dir = PAPERERA_DIR / "notes"
    trash_docs_dir = PAPERERA_DIR / "documents"
    
    comptador = 0
    for p in trash_notes_dir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)
            comptador += 1
    for p in trash_docs_dir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)
            comptador += 1

    # Netejar tots els comentaris TRASH de biblioteca.bib
    if BIB_FILE_PATH.exists():
        try:
            with bloqueig(BIB_FILE_PATH):
                bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                bib_nou = re.sub(r'\n*(?:% TRASH: [^\n]*\n?)+', '\n', bib_text)
                if bib_nou != bib_text:
                    escriure_atomic(BIB_FILE_PATH, bib_nou, backup=True)
        except Exception as e_bib:
            logger.warning(f"Error netejant entrades TRASH a bibtex: {e_bib}")

    return {"status": "ok", "message": f"S'han suprimit {comptador} fitxers de la paperera"}

@router.post("/trash/move-doc/{filename}")
async def moure_document_paperera(filename: str):
    """Mou un document orfe (PDF/HTML) a la Paperera de la biblioteca."""
    src = FONTS_DIR / filename
    if not src.is_file():
        raise HTTPException(status_code=404, detail="Document no trobat")

    trash_docs_dir = PAPERERA_DIR / "documents"
    trash_docs_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_docs_dir / filename
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error movent document a la paperera: {e}")

    return {"status": "ok", "message": f"Document {filename} mogut a la paperera"}

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
        with bloqueig(path_fitxer):
            escriure_atomic(path_fitxer, contingut)
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
title: {json.dumps(titol_clean, ensure_ascii=False)}
date: {avui}
tags: {json.dumps(tags_list, ensure_ascii=False)}
---

# {titol_clean}

"""
    with bloqueig(path_fitxer):
        escriure_atomic(path_fitxer, contingut)
    return {
        "status": "created",
        "filename": nom_fitxer,
        "path": str(path_fitxer.resolve())
    }

@router.get("/notes/{filename}/connections")
async def obtenir_connexions(filename: str):
    """Calcula els retroenllaços (backlinks) i enllaços sortints (outlinks) d'una nota."""
    path_actual = _ruta_segura_nota(filename)
    if not path_actual.exists() or not path_actual.is_file():
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
    S'executa en un fil de treball per no bloquejar l'event loop d'Uvicorn.
    """
    path = _ruta_segura_nota(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    def _rescatar_sync() -> dict:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        is_biblio = path.name.startswith("@")
        citekey = path.stem[1:] if is_biblio else path.stem
        target_pdf = FONTS_DIR / f"{citekey}.pdf"

        titol = _extreure_titol_nota(path, content[:1500])
        m_doi = re.search(r'doi:\s*["\']?([^"\'\n\r]+)["\']?', content, re.IGNORECASE)
        doi = m_doi.group(1).strip() if m_doi else ""
        m_pmid = re.search(r'pmid:?\s*["\']?(\d+)["\']?', content, re.IGNORECASE)
        pmid = m_pmid.group(1).strip() if m_pmid else (citekey[4:] if citekey.startswith("pmid") and citekey[4:].isdigit() else "")
        m_abstract = re.search(r'#+\s*(?:Resum|Abstract)[\r\n]+([\s\S]*?)(?=\n#+|$)', content, re.IGNORECASE)
        abstract = m_abstract.group(1).strip() if m_abstract else ""

        if target_pdf.is_file():
            is_valid, reason = is_valid_pdf_content(target_pdf, title=titol, abstract=abstract, doi=doi)
            if is_valid:
                return {
                    "status": "ok",
                    "message": f"El PDF {target_pdf.name} ja existeix a la biblioteca i és vàlid.",
                    "filename": target_pdf.name
                }
            else:
                logger.warning(f"El PDF existent {target_pdf.name} no és vàlid ({reason}). Eliminant el fitxer incomplet...")
                target_pdf.unlink(missing_ok=True)

        # 1. Intentar rescatar des de ~/Downloads
        trobat = check_and_import_from_downloads(target_pdf, title=titol, abstract=abstract, doi=doi, pmid=pmid)

        # 2. Si no es troba a Downloads i té DOI, provar descarregar via add-paper / Unpaywall
        if not trobat and doi and not doi.startswith("-"):
            add_paper_cmd = shutil.which("add-paper") or str(Path.home() / ".local" / "bin" / "add-paper")
            subprocess.run([add_paper_cmd, f"https://doi.org/{doi}"], capture_output=True, text=True, timeout=30)
            if target_pdf.is_file():
                is_val, _ = is_valid_pdf_content(target_pdf, title=titol, abstract=abstract, doi=doi)
                if is_val:
                    trobat = True
                else:
                    target_pdf.unlink(missing_ok=True)

        if trobat and target_pdf.is_file():
            try:
                if BIB_FILE_PATH.exists():
                    with bloqueig(BIB_FILE_PATH):
                        bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                        patro_especific = re.compile(rf'(@\w+\s*\{{\s*{re.escape(citekey)}\s*,[\s\S]*?\n\}})(?=@|\Z)', re.MULTILINE)
                        m_ent = patro_especific.search(bib_text)
                        if m_ent:
                            entrada_antiga = m_ent.group(1)
                            if re.search(r'file\s*=\s*\{[^}]*\}', entrada_antiga):
                                entrada_nova = re.sub(r'file\s*=\s*\{[^}]*\}', f'file = {{{str(target_pdf)}}}', entrada_antiga)
                            else:
                                entrada_nova = re.sub(rf'(@\w+\s*\{{\s*{re.escape(citekey)}\s*,)', rf'\g<1>\n  file = {{{str(target_pdf)}}},', entrada_antiga)
                            bib_text_nou = bib_text[:m_ent.start()] + entrada_nova + bib_text[m_ent.end():]
                            escriure_atomic(BIB_FILE_PATH, bib_text_nou, backup=True)
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

    try:
        return await asyncio.to_thread(_rescatar_sync)
    except Exception as e:
        logger.error(f"Error rescatant PDF per a {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# 2. PDF STREAMING & HIGHLIGHTS
# =====================================================================

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


# =====================================================================
# 3. BIBLIOTECA I FONTS
# =====================================================================

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


# =====================================================================
# 4. CERCA ACADÈMICA FEDERADA & IMPORTACIÓ ZOTERLESS + MCP LOCAL
# =====================================================================

_MCP_OPEN_DATA_MOD = None
_MCP_REAL_ESTATE_MOD = None

def _get_mcp_open_data():
    global _MCP_OPEN_DATA_MOD
    if _MCP_OPEN_DATA_MOD is not None:
        return _MCP_OPEN_DATA_MOD
    open_dir = Path.home() / "Projects" / "mcp-server-open-data-spain"
    if open_dir.exists():
        import glob
        for sp in glob.glob(str(open_dir / ".venv/lib/python3.*/site-packages")):
            if sp not in sys.path:
                sys.path.insert(0, sp)
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("open_data_srv", str(open_dir / "server.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _MCP_OPEN_DATA_MOD = mod
            return mod
        except Exception as e:
            logger.warning(f"Error importing open-data-spain server: {e}")
    return None

def _get_mcp_real_estate():
    global _MCP_REAL_ESTATE_MOD
    if _MCP_REAL_ESTATE_MOD is not None:
        return _MCP_REAL_ESTATE_MOD
    real_dir = Path.home() / "Projects" / "mcp-server-real-estate-spain"
    if real_dir.exists():
        import glob
        for sp in glob.glob(str(real_dir / ".venv/lib/python3.*/site-packages")):
            if sp not in sys.path:
                sys.path.insert(0, sp)
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("real_estate_srv", str(real_dir / "server.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _MCP_REAL_ESTATE_MOD = mod
            return mod
        except Exception as e:
            logger.warning(f"Error importing real-estate-spain server: {e}")
    return None

def _query_open_data_spain(q: str, limit: int = 5, specific: Optional[str] = None) -> list:
    mod = _get_mcp_open_data()
    if not mod:
        return []
    res_list = []
    
    # 1. BOE
    if not specific or specific in ["boe", "legislacio"]:
        try:
            b_raw = mod.search_boe(query=q)
            b_data = json.loads(b_raw)
            for it in b_data.get("disposiciones", [])[:limit]:
                pdf_u = it.get("url_pdf", "")
                res_list.append({
                    "title": it.get("titulo", "Disposició BOE"),
                    "authors": [it.get("departamento", "BOE")],
                    "year": b_data.get("data_consultada", "")[:4],
                    "date": b_data.get("data_consultada", ""),
                    "doi": "",
                    "url": it.get("url_html", "") or pdf_u,
                    "journal": f"BOE ({it.get('seccion', 'Oficial')})",
                    "abstract": f"Identificador: {it.get('identificador', '')}. {it.get('titulo', '')}",
                    "is_oa": bool(pdf_u),
                    "oa_url": pdf_u,
                    "sources": ["BOE", "OpenData"],
                    "extra": {"boe_id": it.get("identificador", ""), "pdf_url": pdf_u},
                    "id": it.get("identificador") or pdf_u or it.get("titulo", "")
                })
        except Exception as e:
            logger.warning(f"Error querying BOE in OpenData: {e}")

    # 2. Europarl
    if specific == "europarl":
        try:
            ep_raw = mod.search_debates_europarl(limit=limit)
            ep_data = json.loads(ep_raw)
            for sp in ep_data.get("intervencions_recents", [])[:limit]:
                sp_id = sp.get("id", "")
                desc = sp.get("descripcio", "Intervenció al Ple del Parlament Europeu")
                dt = sp.get("data", "")
                tip = sp.get("tipus", "Debat Plenari")
                res_list.append({
                    "title": desc,
                    "authors": ["Parlament Europeu"],
                    "year": dt[:4] if dt else "",
                    "date": dt,
                    "doi": "",
                    "url": sp.get("enllac_oficial", ""),
                    "journal": f"Europarl ({tip})",
                    "abstract": f"Registre Oficial del Parlament Europeu ({tip}). Data: {dt}. Id activitat: {sp_id}",
                    "is_oa": False,
                    "oa_url": "",
                    "sources": ["Europarl", "OpenData"],
                    "extra": {"europarl_id": sp_id, "tipus": tip},
                    "id": sp.get("enllac_oficial") or f"europarl_{sp_id}"
                })
        except Exception as e:
            logger.warning(f"Error querying Europarl: {e}")

    # 3. datos.gob.es
    if not specific or specific == "opendata":
        try:
            d_raw = mod.search_datos_gob_es(query=q, max_results=limit)
            d_data = json.loads(d_raw)
            for it in d_data.get("datasets", [])[:limit]:
                res_list.append({
                    "title": it.get("titol", "Conjunt de Dades"),
                    "authors": [it.get("organisme", "datos.gob.es")],
                    "year": it.get("data_publicacio", "")[:4],
                    "date": it.get("data_publicacio", ""),
                    "doi": "",
                    "url": it.get("enllac_dades", ""),
                    "journal": "datos.gob.es (Catàleg Nacional de Dades)",
                    "abstract": it.get("descripcio", ""),
                    "is_oa": False,
                    "oa_url": "",
                    "sources": ["datos.gob.es", "OpenData"],
                    "extra": {"organisme": it.get("organisme", "")},
                    "id": it.get("enllac_dades") or f"opendata_{it.get('titol', '')[:30]}"
                })
        except Exception as e:
            logger.warning(f"Error querying datos.gob.es: {e}")

    # 4. INE
    if not specific or specific == "opendata":
        try:
            ine_raw = mod.search_ine_statistics(query=q, max_results=limit)
            ine_data = json.loads(ine_raw)
            for it in ine_data.get("operacions", [])[:limit]:
                res_list.append({
                    "title": it.get("operacion", "Operació Estadística"),
                    "authors": ["Instituto Nacional de Estadística (INE)"],
                    "year": "2026",
                    "date": "",
                    "doi": "",
                    "url": it.get("link", ""),
                    "journal": f"INE Estadístiques ({it.get('codigo', 'INE')})",
                    "abstract": f"Operació estadística oficial de l'INE: {it.get('operacion', '')}. Codi: {it.get('codigo', '')}",
                    "is_oa": False,
                    "oa_url": "",
                    "sources": ["INE", "OpenData"],
                    "extra": {"codigo_ine": it.get("codigo", "")},
                    "id": it.get("link") or f"ine_{it.get('codigo', '')}"
                })
        except Exception as e:
            logger.warning(f"Error querying INE: {e}")

    # 5. BDNS (Subvencions)
    if not specific or specific == "opendata":
        try:
            b_raw = mod.search_subvenciones_bdns(query=q)
            b_data = json.loads(b_raw)
            conv_url = b_data.get("portal_convocatories", "")
            res_list.append({
                "title": f"Convocatòries i ajudes públiques (BDNS): '{q}'",
                "authors": ["Ministerio de Hacienda - BDNS"],
                "year": "2026",
                "date": "",
                "doi": "",
                "url": conv_url,
                "journal": "BDNS (Base de Datos Nacional de Subvenciones)",
                "abstract": b_data.get("consell_cerca", "Cercador oficial de subvencions, ajudes públiques i fons NextGen PRTR."),
                "is_oa": False,
                "oa_url": "",
                "sources": ["BDNS", "OpenData"],
                "extra": {"bdns_query": q},
                "id": conv_url or f"bdns_{q}"
            })
        except Exception as e:
            logger.warning(f"Error querying BDNS: {e}")

    return res_list

def _query_real_estate_spain(q: str) -> list:
    mod = _get_mcp_real_estate()
    if not mod:
        return []
    res_list = []
    q_clean = q.strip().upper().replace(" ", "").replace("-", "")
    
    # 1. Catastro parcel lookup (if >= 14 chars alphanumeric)
    if len(q_clean) >= 14 and all(c.isalnum() for c in q_clean):
        try:
            c_raw = mod.query_catastro(q_clean)
            c_data = json.loads(c_raw)
            adr = c_data.get("adreca") or "Sense carrer especificat"
            mun = c_data.get("municipi") or ""
            prov = c_data.get("provincia") or ""
            gps = c_data.get("coordenades_gps", {})
            lat = gps.get("latitud")
            lon = gps.get("longitud")
            tip = c_data.get("tipus_immoble", "Immoble")

            res_list.append({
                "title": f"Immoble Cadastral {q_clean}: {adr}",
                "authors": [f"{mun}, {prov} ({tip})"],
                "year": "2026",
                "date": "",
                "doi": "",
                "url": c_data.get("enllac_seu_electronica", ""),
                "journal": f"Sede Electrónica del Catastro ({tip})",
                "abstract": f"Referència Cadastral: {q_clean}\nAdreça: {adr}, {mun} ({prov})\nCoordenades GPS WGS84: Latitud {lat}, Longitud {lon}\nEnllaç al visor cartogràfic oficial del Cadastre.",
                "is_oa": False,
                "oa_url": "",
                "sources": ["Catastro"],
                "extra": {
                    "referencia_catastral": q_clean,
                    "latitud": lat,
                    "longitud": lon,
                    "visor_cartografic": c_data.get("visor_cartografic", "")
                },
                "id": f"catastro_{q_clean}"
            })
        except Exception as e:
            logger.warning(f"Error querying Catastro in real-estate: {e}")

    # 2. Safor real estate market benchmark
    try:
        m_raw = mod.analyze_real_estate_market(q)
        m_data = json.loads(m_raw)
        if "municipio" in m_data:
            mun_name = m_data.get("municipio", q)
            res_list.append({
                "title": f"Mercat Immobiliari: {mun_name}",
                "authors": [m_data.get("comarca", "La Safor")],
                "year": "2026",
                "date": "",
                "doi": "",
                "url": "",
                "journal": f"Venda: {m_data.get('precio_venta_m2')} €/m² | Lloguer: {m_data.get('precio_alquiler_m2')} €/m²",
                "abstract": f"Zona: {mun_name}\nPreu mitjà vivenda (80m²): {m_data.get('precio_medio_vivienda_80m2')} € | Lloguer mitjà (80m²): {m_data.get('alquiler_medio_80m2')} €/mes\nRendibilitat bruta anual: {m_data.get('rentabilidad_bruta_anual')}%\nTendència anual: venda {m_data.get('tendencia_anual_venta')}, lloguer {m_data.get('tendencia_anual_alquiler')}\n{m_data.get('perfil', '')}",
                "is_oa": False,
                "oa_url": "",
                "sources": ["RealEstateSafor", "Catastro"],
                "extra": m_data,
                "id": f"realestate_{q.lower().strip().replace(' ', '_')}"
            })
    except Exception as e:
        logger.warning(f"Error querying Safor Real Estate: {e}")

    return res_list


@router.get("/academic/search")
async def cerca_academica_federada(
    q: str = Query(..., min_length=2),
    limit: int = 5,
    sources: Optional[str] = Query("all", description="Fonts separades per coma o 'all'.")
):
    """Cerca federada multilingüe a través d'academic-spain, open-data-spain i real-estate-spain."""
    s_clean = (sources or "all").lower().strip()

    # Route specific MCP servers directly in worker thread
    if s_clean in ["opendata", "datosgob", "ine", "bdns"]:
        results = await asyncio.to_thread(_query_open_data_spain, q, limit, "opendata")
        return {"query": q, "translations": {}, "total": len(results), "results": results}

    if s_clean == "europarl":
        results = await asyncio.to_thread(_query_open_data_spain, q, limit, "europarl")
        return {"query": q, "translations": {}, "total": len(results), "results": results}

    if s_clean in ["catastro", "immobiliari", "realestate"]:
        results = await asyncio.to_thread(_query_real_estate_spain, q)
        return {"query": q, "translations": {}, "total": len(results), "results": results}

    # Query academic-spain
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

            # If searching BOE, augment with real-time BOE sumario from open-data-spain
            if s_clean in ["boe", "legislacio"]:
                boe_extra = await asyncio.to_thread(_query_open_data_spain, q, limit, "boe")
                existing_titles = {r["title"].lower() for r in results}
                for bx in boe_extra:
                    if bx["title"].lower() not in existing_titles:
                        results.insert(0, bx)

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
    """Descarrega el PDF, afegeix l'entrada a biblioteca.bib i crea la nota @citekey.md mitjançant add-paper o crea la nota directa de dades."""
    ident = req.identifier.strip()
    if not ident or ident.startswith("-"):
        raise HTTPException(status_code=400, detail="Identificador d'article no vàlid")

    # Direct note generation for Catastro, Real Estate or OpenData resources without PDF
    if not req.pdf_url and (ident.startswith("catastro_") or ident.startswith("realestate_") or ident.startswith("opendata_") or ident.startswith("ine_") or ident.startswith("bdns_") or ident.startswith("europarl_")):
        clean_key = re.sub(r'[^a-zA-Z0-9_]', '_', ident)
        note_file = _ruta_segura_nota(f"@{clean_key}.md")

        title = ident
        extra_content = ""
        tags = ["notes"]

        if ident.startswith("catastro_"):
            rc = ident.replace("catastro_", "").upper()
            title = f"Immoble Cadastral {rc}"
            tags = ["catastro", "immobles", "patrimoni"]
            r_mod = await asyncio.to_thread(_get_mcp_real_estate)
            if r_mod:
                try:
                    raw_cat = await asyncio.to_thread(r_mod.query_catastro, rc)
                    c_info = json.loads(raw_cat)
                    adr = c_info.get("adreca") or ""
                    mun = c_info.get("municipi") or ""
                    prov = c_info.get("provincia") or ""
                    gps = c_info.get("coordenades_gps", {})
                    lat = gps.get("latitud")
                    lon = gps.get("longitud")
                    extra_content = f"""- **Referència Cadastral:** `{rc}`
- **Adreça Oficial:** {adr}
- **Municipi / Província:** {mun} ({prov})
- **Tipus d'Immoble:** {c_info.get('tipus_immoble', 'Urbana')}
- **Coordenades GPS WGS84:** Latitud `{lat}`, Longitud `{lon}`
- **Seu Electrònica del Cadastre:** [{rc}]({c_info.get('enllac_seu_electronica', '')})
- **Visor Cartogràfic Oficial:** [Obrir visor de la parcel·la]({c_info.get('visor_cartografic', '')})

## Observacions de l'immoble
"""
                except Exception as e:
                    logger.warning(f"Error fetching catastro details: {e}")

        elif ident.startswith("realestate_"):
            zona = ident.replace("realestate_", "")
            title = f"Mercat Immobiliari: {zona.title()} (La Safor)"
            tags = ["immobiliari", "la_safor", "mercat", "preus"]
            r_mod = await asyncio.to_thread(_get_mcp_real_estate)
            if r_mod:
                try:
                    raw_mkt = await asyncio.to_thread(r_mod.analyze_real_estate_market, zona)
                    m_info = json.loads(raw_mkt)
                    extra_content = f"""- **Zona:** {m_info.get('municipio', zona)}
- **Preu Venda:** {m_info.get('precio_venta_m2')} €/m²
- **Preu Lloguer:** {m_info.get('precio_alquiler_m2')} €/m²/mes
- **Vivenda Tipus 80 m²:** {m_info.get('precio_medio_vivienda_80m2')} € (lloguer: {m_info.get('alquiler_medio_80m2')} €/mes)
- **Rendibilitat Bruta Anual:** {m_info.get('rentabilidad_bruta_anual')}%
- **Tendència:** Venda {m_info.get('tendencia_anual_venta')} | Lloguer {m_info.get('tendencia_anual_alquiler')}

### Perfil de la Zona
{m_info.get('perfil', '')}
"""
                except Exception as e:
                    logger.warning(f"Error fetching real estate details: {e}")

        content = f"""---
title: {json.dumps(title, ensure_ascii=False)}
date: "{datetime.now().strftime('%Y-%m-%d')}"
tags: {json.dumps(tags, ensure_ascii=False)}
citekey: "@{clean_key}"
---

# {title}

{extra_content}
"""
        with bloqueig(note_file):
            escriure_atomic(note_file, content)
        return {
            "status": "ok",
            "message": f"Nota creada correctament: @{clean_key}",
            "output": f"Nota creada: @{clean_key}"
        }

    add_paper_cmd = shutil.which("add-paper") or str(Path.home() / ".local" / "bin" / "add-paper")
    cmd = [add_paper_cmd]
    if req.vpn:
        cmd.append("--vpn")
    if req.pdf_url:
        pdf_u_clean = req.pdf_url.strip()
        if pdf_u_clean.startswith("-"):
            raise HTTPException(status_code=400, detail="URL de PDF no vàlida")
        cmd.extend(["--pdf-url", pdf_u_clean])
    cmd.append(ident)

    try:
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=60)
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
# 5. FARISTOL WORKBENCH ACTIONS
# =====================================================================

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


# =====================================================================
# 6. COL·LECCIONS VIRTUALS
# =====================================================================

def _llegir_colleccions_nolock() -> dict:
    data = llegir_json_segur(COLLECTIONS_FILE, {"collections": []})
    if not isinstance(data, dict) or "collections" not in data:
        return {"collections": []}
    return data

def _escriure_colleccions_nolock(dades: dict):
    escriure_atomic(COLLECTIONS_FILE, json.dumps(dades, indent=2, ensure_ascii=False))

def _notificar_kate_sync():
    try:
        subprocess.Popen(["kate-sync-project", "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _carregar_colleccions() -> dict:
    with bloqueig(COLLECTIONS_FILE):
        return _llegir_colleccions_nolock()

def _desar_colleccions(dades: dict):
    with bloqueig(COLLECTIONS_FILE):
        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()

@router.get("/collections")
@router.get("/faristol/collections")
async def obtenir_colleccions():
    """Retorna l'arbre de col·leccions virtuals de Faristol (assegurant la sincronització de PDFs associats a notes)."""
    modificat = False
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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
            _escriure_colleccions_nolock(dades)
    if modificat:
        _notificar_kate_sync()
    return dades

@router.post("/collections")
@router.post("/faristol/collections")
async def guardar_colleccions(req: GuardarColleccionsRequest):
    """Guarda l'estat complet de les col·leccions."""
    data = {"collections": req.collections}
    _desar_colleccions(data)
    return {"status": "ok", "collections": req.collections}

@router.post("/collections/create")
@router.post("/faristol/collections/create")
async def crear_colleccio(req: NovaColleccioRequest):
    """Crea una nova col·lecció virtual de forma atòmica."""
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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
        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {"status": "ok", "collection": nova}

@router.put("/collections/{col_id}")
@router.put("/faristol/collections/{col_id}")
async def actualitzar_colleccio(col_id: str, req: ActualitzarColleccioRequest):
    """Actualitza el nom o el pare d'una col·lecció de forma atòmica."""
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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
        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {"status": "ok", "col_id": col_id}

@router.delete("/collections/{col_id}")
@router.delete("/faristol/collections/{col_id}")
async def eliminar_colleccio(col_id: str):
    """Elimina una col·lecció virtual sense tocar els fitxers de disc."""
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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
        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {"status": "ok", "deleted": col_id}

@router.post("/collections/{col_id}/add-item")
@router.post("/faristol/collections/{col_id}/add-item")
async def afegir_item_colleccio(col_id: str, req: ItemColleccioRequest):
    """Afegeix una nota o PDF a una col·lecció (i sincronitza automàticament els fitxers associats)."""
    assoc_pdfs = obtenir_pdfs_associats_nota(req.filename) if req.type == "note" else []
    assoc_notes = obtenir_notes_associades_pdf(req.filename) if req.type != "note" else []

    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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
            for p in assoc_pdfs:
                if p not in col["pdfs"]:
                    col["pdfs"].append(p)
        else:
            if req.filename not in col["pdfs"]:
                col["pdfs"].append(req.filename)
            for n in assoc_notes:
                if n not in col["notes"]:
                    col["notes"].append(n)

        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {"status": "ok", "collection": col}

@router.post("/collections/{col_id}/remove-item")
@router.post("/faristol/collections/{col_id}/remove-item")
async def treure_item_colleccio(col_id: str, req: ItemColleccioRequest):
    """Elimina una nota o PDF d'una col·lecció virtual."""
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
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

        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {"status": "ok", "collection": col}

@router.post("/collections/set-item-collections")
@router.post("/faristol/collections/set-item-collections")
async def assignar_colleccions_item(req: SetItemCollectionsRequest):
    """Assigna una nota o PDF exactament a les col·leccions indicades."""
    target_ids = set(req.collection_ids)
    associated_pdfs = obtenir_pdfs_associats_nota(req.filename) if req.type == "note" else []
    associated_notes = obtenir_notes_associades_pdf(req.filename) if req.type != "note" else []

    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
        if req.type == "note":
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
                        other_pdfs = set()
                        for other_n in col.get("notes", []):
                            for p in obtenir_pdfs_associats_nota(other_n):
                                other_pdfs.add(p)
                        for p in associated_pdfs:
                            if p not in other_pdfs and p in col["pdfs"]:
                                col["pdfs"].remove(p)
        else:
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

        _escriure_colleccions_nolock(dades)
    _notificar_kate_sync()
    return {
        "status": "ok",
        "filename": req.filename,
        "collections": list(target_ids),
        "collections_data": dades["collections"]
    }

@router.post("/collections/{col_id}/sync-notebooklm")
@router.post("/faristol/collections/{col_id}/sync-notebooklm")
async def sincronitzar_colleccio_notebooklm(col_id: str):
    """Crea o sincronitza un quadern de NotebookLM per a la col·lecció virtual amb validació estricta de rutes."""
    modificat = False
    with bloqueig(COLLECTIONS_FILE):
        dades = _llegir_colleccions_nolock()
        col = next((c for c in dades.get("collections", []) if c.get("id") == col_id), None)
        if not col:
            raise HTTPException(status_code=404, detail="Col·lecció no trobada")

        if "pdfs" not in col:
            col["pdfs"] = []
        for n in col.get("notes", []):
            for p in obtenir_pdfs_associats_nota(n):
                if p not in col["pdfs"]:
                    col["pdfs"].append(p)
                    modificat = True
        if modificat:
            _escriure_colleccions_nolock(dades)

        col_name = col.get("name", "").strip()
        pdf_list = list(col.get("pdfs", []))
        notebook_id = col.get("notebook_id")

    if modificat:
        _notificar_kate_sync()

    # Si no té quadern, crear-lo
    if not notebook_id:
        ok, out, data = await _exec_nlm_cli(["notebook", "create", col_name, "--json"], parse_json=True)
        if not ok:
            ok, out, data = await _exec_nlm_cli(["create", "notebook", col_name, "--json"], parse_json=True)
        if ok and isinstance(data, dict):
            notebook_id = data.get("id") or data.get("notebook_id")
            if notebook_id:
                with bloqueig(COLLECTIONS_FILE):
                    fresh_dades = _llegir_colleccions_nolock()
                    for c_item in fresh_dades.get("collections", []):
                        if c_item.get("id") == col_id:
                            c_item["notebook_id"] = notebook_id
                            break
                    _escriure_colleccions_nolock(fresh_dades)
                _notificar_kate_sync()

    if not notebook_id or str(notebook_id).startswith("-"):
        raise HTTPException(status_code=500, detail="No s'ha pogut crear o associar el quadern de NotebookLM")

    # Obtenir fonts ja presents
    ok, _, src_list = await _exec_nlm_cli(["source", "list", str(notebook_id), "--json"], parse_json=True)
    existing_titles = set()
    if ok and isinstance(src_list, list):
        for s in src_list:
            if isinstance(s, dict):
                existing_titles.add(s.get("title", ""))

    # Pujar els PDFs nous validant estrictament cada ruta amb _document_segur
    afegits = 0
    for pdf_name in pdf_list:
        if pdf_name in existing_titles:
            continue
        try:
            p = _document_segur(pdf_name)
        except HTTPException:
            continue
        if p.is_file() and p.suffix.lower() == ".pdf":
            ok_add, _, _ = await _exec_nlm_cli(["source", "add", str(notebook_id), "--file", str(p)], parse_json=False)
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

def _validar_id_nlm(val: str, camp: str = "id") -> str:
    net = (val or "").strip()
    if not net or net.startswith("-") or "\x00" in net:
        raise HTTPException(status_code=400, detail=f"Paràmetre '{camp}' no vàlid")
    return net

async def _exec_nlm_cli(args: List[str], parse_json: bool = True, timeout: float = 90.0):
    nlm_bin = shutil.which("nlm") or "/home/casimir/.local/bin/nlm"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            nlm_bin, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return False, f"Temps d'espera esgotat ({int(timeout)}s) executant 'nlm'.", None

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
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise
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
    nb_id = _validar_id_nlm(notebook_id, "notebook_id")
    ok, text, data = await _exec_nlm_cli(["source", "list", nb_id, "--json"], parse_json=True)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"notebook_id": nb_id, "sources": data}

@router.post("/nlm/query")
async def nlm_consultar(req: NlmQueryRequest):
    """Executa una consulta RAG contra un quadern o font de NotebookLM."""
    nb_id = _validar_id_nlm(req.notebook_id, "notebook_id")
    query_clean = (req.query or "").strip()
    if not query_clean or query_clean.startswith("-"):
        raise HTTPException(status_code=400, detail="La consulta no pot estar buida ni començar per '-'")

    args = ["query", "notebook", nb_id, query_clean, "--json"]
    if req.conversation_id:
        args.extend(["-c", _validar_id_nlm(req.conversation_id, "conversation_id")])
    if req.source_ids and len(req.source_ids) > 0:
        clean_sids = [_validar_id_nlm(s, "source_id") for s in req.source_ids]
        args.extend(["-s", ",".join(clean_sids)])
    elif req.source_id:
        args.extend(["-s", _validar_id_nlm(req.source_id, "source_id")])

    ok, text, data = await _exec_nlm_cli(args, parse_json=True, timeout=120.0)
    if not ok:
        raise HTTPException(status_code=500, detail=text)

    sources_map = {}
    ok_src, _, src_data = await _exec_nlm_cli(["source", "list", nb_id, "--json"], parse_json=True)
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

    def _resoldre_pagines_sync() -> dict:
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
                    clean_title_leaf = Path(title).name
                    for p_cand in [FONTS_DIR / clean_title_leaf, NOTAS_DIR / clean_title_leaf, FONTS_DIR / f"{clean_title_leaf}.pdf", NOTAS_DIR / f"{clean_title_leaf}.pdf"]:
                        if p_cand.exists() and p_cand.is_file():
                            target_pdf = p_cand
                            break
                    if not target_pdf:
                        target_stem = Path(clean_title_leaf).stem.lower()
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
                except Exception:
                    pass
        return citation_details

    citation_details = await asyncio.to_thread(_resoldre_pagines_sync)

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
    nb_id = _validar_id_nlm(req.notebook_id, "notebook_id")
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="La URL ha de començar per http:// o https://")

    ok, text, _ = await _exec_nlm_cli(["source", "add", nb_id, "--url", url], parse_json=False)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"status": "ok", "message": text}

@router.post("/nlm/delete")
async def nlm_esborrar_element(req: NlmDeleteRequest):
    """Esborra un quadern o una font de NotebookLM."""
    item_id = _validar_id_nlm(req.item_id, "item_id")
    if req.target_type == "notebook":
        args = ["notebook", "delete", item_id, "--yes"]
    elif req.target_type == "source":
        if not req.notebook_id:
            raise HTTPException(status_code=400, detail="Cal el notebook_id per esborrar una font")
        nb_id = _validar_id_nlm(req.notebook_id, "notebook_id")
        args = ["source", "delete", nb_id, item_id, "--yes"]
    else:
        raise HTTPException(status_code=400, detail="Tipus d'element no reconegut")

    ok, text, _ = await _exec_nlm_cli(args, parse_json=False)
    if not ok:
        raise HTTPException(status_code=500, detail=text)
    return {"status": "ok", "message": text}

@router.post("/nlm/synthesize")
async def nlm_guardar_sintesi(req: NlmSintesiRequest):
    """Injecta una síntesi estructurada de NotebookLM a la nota especificada de forma atòmica i amb comprovació estricta de ruta."""
    path = _ruta_segura_nota(req.fitxer_desti)
    if path.suffix.lower() not in (".md", ".org"):
        raise HTTPException(status_code=400, detail="Només es permet afegir síntesi a fitxers de notes .md o .org")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Nota de destinació no trobada")

    ara_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    bloc = f"""

### 🤖 Síntesi NotebookLM ({ara_str})
**Àmbit / Origen:** {req.origen}
**Pregunta:** {req.pregunta}

> {req.resposta.strip()}
"""
    try:
        with bloqueig(path):
            contingut_actual = path.read_text(encoding="utf-8", errors="replace")
            escriure_atomic(path, contingut_actual + bloc)
        return {"status": "ok", "message": f"Síntesi injectada a {path.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
