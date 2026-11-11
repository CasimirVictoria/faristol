"""Endpoints de notes, paperera, connexions i subratllats."""

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException

from faristol.api.common import _ruta_segura_nota, logger
from faristol.api.models import GuardarNotaRequest, NovaNotaRequest
from faristol.bibtex import GestorBibtex
from faristol.config import BIB_FILE_PATH, FONTS_DIR, NOTAS_DIR, PAPERERA_DIR
from faristol.fsatomic import bloqueig, escriure_atomic
from faristol.utils import check_and_import_from_downloads, detectar_tots_els_pdfs, extraure_identificador_fitxer, extraure_pagines_nota, extraure_tags_nota, is_valid_pdf_content, netejar_text_slug

router = APIRouter()

# Memòria cau en RAM per mtime de notes: evita re-llegir N fitxers en llistar o calcular backlinks
_NOTES_CACHE: Dict[str, dict] = {}
_LAST_NOTAS_DIR_KEY: Optional[str] = None


def _get_note_cached(path: Path) -> dict:
    """Retorna metadades, text complet i enllaços d'una nota, rellegint només si ha canviat l'mtime."""
    st = path.stat()
    key = path.name
    cached = _NOTES_CACHE.get(key)
    if cached and cached["mtime"] == st.st_mtime and cached["size"] == st.st_size:
        return cached

    content = path.read_text(encoding="utf-8", errors="replace")
    titol = _extreure_titol_nota(path, content[:1500])
    tags = list(extraure_tags_nota(path.name, content))
    pdfs = detectar_tots_els_pdfs(path, content)
    has_pdf_doc = any(Path(p[1]).suffix.lower() == ".pdf" for p in pdfs)
    has_html_doc = any(Path(p[1]).suffix.lower() in [".html", ".htm"] for p in pdfs)
    doc_format = "pdf" if has_pdf_doc else ("html" if has_html_doc else "")

    patrons_enllac = [
        r'\[\[(?:denote:)?([^\]|\r\n]+)(?:\|[^\r\n\]]+)?(?:\]\[(.*?)\])?\]\]',
        r'\[([^\]]+)\]\(([^)]+\.(?:md|org))\)',
        r'\[@([a-zA-Z0-9_-]+)\]',
    ]
    enllacos_trobats = set()
    for pat in patrons_enllac:
        for m in re.finditer(pat, content, re.IGNORECASE):
            target = m.group(1) if pat.startswith(r'\[\[') or pat.startswith(r'\[@') else m.group(2)
            target = Path(target).name.lower().strip()
            enllacos_trobats.add(target)
            if not target.startswith('@'):
                enllacos_trobats.add(f"@{target}")
            else:
                enllacos_trobats.add(target[1:])

    entry = {
        "filename": path.name,
        "path": str(path.resolve()),
        "title": titol,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "mtime_str": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "tags": tags,
        "is_org": path.suffix.lower() == ".org",
        "is_md": path.suffix.lower() == ".md",
        "has_pdf": len(pdfs) > 0,
        "pdf_count": len(pdfs),
        "has_pdf_doc": has_pdf_doc,
        "has_html_doc": has_html_doc,
        "doc_format": doc_format,
        "is_biblio": path.name.startswith("@"),
        "content_lower": content.lower(),
        "outlink_targets": enllacos_trobats,
    }
    _NOTES_CACHE[key] = entry
    return entry


def _sincronitzar_cache_notes() -> list[dict]:
    """Assegura que _NOTES_CACHE reflecteix els fitxers presents a NOTAS_DIR."""
    global _LAST_NOTAS_DIR_KEY
    cur_key = str(NOTAS_DIR.resolve())
    if _LAST_NOTAS_DIR_KEY != cur_key:
        _NOTES_CACHE.clear()
        _LAST_NOTAS_DIR_KEY = cur_key

    actual_names = set()
    items = []
    for path in NOTAS_DIR.glob("*"):
        if path.name.startswith((".", "#")) or path.name.endswith("~") or not path.is_file():
            continue
        if path.suffix.lower() not in [".md", ".org"]:
            continue
        try:
            actual_names.add(path.name)
            items.append(_get_note_cached(path))
        except Exception:
            continue

    for stale in [k for k in _NOTES_CACHE if k not in actual_names]:
        _NOTES_CACHE.pop(stale, None)
    return items

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
    """Retorna la llista de totes les notes disponibles (amb cerca full-text i enriquiment BibTeX des de memòria cau)."""
    cached_items = _sincronitzar_cache_notes()

    bib_by_key: Dict[str, dict] = {}
    if BIB_FILE_PATH.exists():
        for ref in GestorBibtex.carregar_referencies(BIB_FILE_PATH):
            k = (ref.get("key") or "").strip().lower()
            if k:
                bib_by_key[k] = ref

    notes = []
    totes_les_tags = set()
    q_terms = [t.lower().strip() for t in (q or "").split() if t.strip()]

    for entry in cached_items:
        for t in entry["tags"]:
            totes_les_tags.add(t)

        if tag and tag.lower() not in [t.lower() for t in entry["tags"]]:
            continue

        stem_low = extraure_identificador_fitxer(entry["filename"]).lower()
        bib = bib_by_key.get(stem_low, {})
        author = bib.get("author", "")
        year = bib.get("year", "")
        journal = bib.get("journal", "")

        if q_terms:
            tags_str = " ".join(entry["tags"]).lower()
            searchable_head = f"{entry['filename']} {entry['title']} {author} {year} {journal} {tags_str}".lower()
            body_low = entry["content_lower"]
            if not all((term in searchable_head or term in body_low) for term in q_terms):
                continue

        notes.append({
            "filename": entry["filename"],
            "path": entry["path"],
            "title": entry["title"],
            "mtime": entry["mtime"],
            "mtime_str": entry["mtime_str"],
            "tags": entry["tags"],
            "is_org": entry["is_org"],
            "is_md": entry["is_md"],
            "size": entry["size"],
            "has_pdf": entry["has_pdf"],
            "pdf_count": entry["pdf_count"],
            "has_pdf_doc": entry["has_pdf_doc"],
            "has_html_doc": entry["has_html_doc"],
            "doc_format": entry["doc_format"],
            "is_biblio": entry["is_biblio"],
            "author": author,
            "year": year,
            "journal": journal,
        })

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

        _NOTES_CACHE.pop(filename, None)
        try:
            from faristol.api.rag import encuar_indexacio_fons
            encuar_indexacio_fons([filename])
        except Exception:
            pass

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

    _NOTES_CACHE.pop(path.name, None)
    try:
        from faristol.api.rag import eliminar_documents_del_rag
        eliminar_documents_del_rag([path.name] + associated_docs)
    except Exception:
        pass

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
        restored_files = [filename]
        
        # Restaurar documents associats
        stem = src_note.stem[1:] if src_note.name.startswith("@") else src_note.stem
        for ext in [".pdf", ".html"]:
            cand = trash_docs_dir / f"{stem}{ext}"
            if cand.is_file():
                shutil.move(str(cand), str(FONTS_DIR / cand.name))
                restored_files.append(cand.name)

        _NOTES_CACHE.pop(filename, None)
        try:
            from faristol.api.rag import encuar_indexacio_fons
            encuar_indexacio_fons(restored_files)
        except Exception:
            pass

        # Descomentar a biblioteca.bib si correspon
        if src_note.name.startswith("@") and BIB_FILE_PATH.exists():
            citekey = src_note.stem[1:]
            try:
                with bloqueig(BIB_FILE_PATH):
                    bib_text = BIB_FILE_PATH.read_text(encoding="utf-8")
                    patro_com = re.compile(r'((?:% TRASH: [^\n]*\n?)+)', re.MULTILINE)
                    
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
        try:
            from faristol.api.rag import encuar_indexacio_fons
            encuar_indexacio_fons([filename])
        except Exception:
            pass
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
                    patro_com = re.compile(r'\n*(?:% TRASH: [^\n]*\n?)+', re.MULTILINE)
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
    """Mou un document (PDF/HTML) i la seua nota associada a la Paperera de la biblioteca."""
    src = FONTS_DIR / filename
    if not src.is_file():
        raise HTTPException(status_code=404, detail="Document no trobat")

    stem = src.stem
    cand_notes = [f"@{stem}.md", f"{stem}.md"]
    note_found = None
    for cand in cand_notes:
        if (NOTAS_DIR / cand).is_file():
            note_found = cand
            break

    # Si té nota associada, deleguem en esborrar_nota perquè mogui nota, docs i comenti bibtex
    if note_found:
        return await esborrar_nota(note_found)

    trash_docs_dir = PAPERERA_DIR / "documents"
    trash_docs_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_docs_dir / filename
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error movent document a la paperera: {e}")

    try:
        from faristol.api.rag import eliminar_documents_del_rag
        eliminar_documents_del_rag([filename])
    except Exception:
        pass

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
    """Calcula els retroenllaços (backlinks) i enllaços sortints (outlinks) d'una nota usant memòria cau en RAM."""
    path_actual = _ruta_segura_nota(filename)
    if not path_actual.exists() or not path_actual.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    try:
        cached_items = _sincronitzar_cache_notes()
        actual_entry = _get_note_cached(path_actual)

        id_actual = extraure_identificador_fitxer(filename).lower()
        stem_actual = Path(filename).stem.lower()
        nom_actual_lower = filename.lower()
        enllacos_trobats = actual_entry["outlink_targets"]

        backlinks = []
        outlinks = []

        for entry in cached_items:
            p_name = entry["filename"]
            if p_name == filename:
                continue

            c_low = entry["content_lower"]
            if (id_actual and id_actual in c_low) or nom_actual_lower in c_low or stem_actual in c_low:
                backlinks.append({
                    "filename": p_name,
                    "title": entry["title"],
                })

            p_name_lower = p_name.lower()
            p_id = extraure_identificador_fitxer(p_name).lower()
            p_stem = Path(p_name).stem.lower()
            if p_name_lower in enllacos_trobats or (p_id and p_id in enllacos_trobats) or p_stem in enllacos_trobats:
                outlinks.append({
                    "filename": p_name,
                    "title": entry["title"],
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


