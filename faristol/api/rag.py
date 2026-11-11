"""
Motor RAG Híbrid Local (Docling + BM25 FTS5 + embeddinggemma-2 en NumPy float32) integrat a Faristol.
Proporciona:
  - Indexació incremental de PDFs, Snapshots HTML (SingleFile) i Notes Markdown/Org.
  - Cua automàtica en segon pla després d'ingestar documents o desar notes.
  - Neteja atòmica de vectors quan s'envia un document o nota a la Paperera.
  - Cerca híbrida instantània (~45–170 ms) i síntesi IA local en streaming SSE (Ollama).
  - Descobriment de connexions semàntiques i resum automàtic amb cites Pandoc [@citekey, p. X].
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from faristol.api.collections import _llegir_colleccions_nolock
from faristol.api.common import _ruta_segura_nota, logger
from faristol.api.models import LocalRagIndexRequest, LocalRagQueryRequest, NoteSummarizeRequest
from faristol.config import BIB_FILE_PATH, FONTS_DIR, NOTAS_DIR
from faristol.utils import extraure_identificador_fitxer, extraure_tags_nota, obtenir_pdfs_associats_nota

# Incorporar cairnsearch des del repositori local si no és al PYTHONPATH
_CAIRNSEARCH_SRC = Path.home() / "Projects" / "cairnsearch" / "src"
if _CAIRNSEARCH_SRC.exists() and str(_CAIRNSEARCH_SRC) not in sys.path:
    sys.path.insert(0, str(_CAIRNSEARCH_SRC))

from cairnsearch.extractors.html_extractor import HtmlExtractor
from cairnsearch.extractors.pdf_extractor import PDFExtractor
from cairnsearch.rag.chunker import DocumentChunker
from cairnsearch.rag.config import EmbeddingProvider, LLMProvider, RAGConfig, set_rag_config
from cairnsearch.rag.embeddings import OllamaEmbedder
from cairnsearch.rag.llm import OllamaLLM
from cairnsearch.rag.retriever import HybridRetriever, RetrievalResult
from cairnsearch.rag.vector_store import VectorStore

router = APIRouter()

# Directori canònic compartit entre cairnsearch, Faristol i el servidor MCP de ModelaSTEAM
_CANONICAL_VEC_DIR = Path(
    os.environ.get(
        "FARISTOL_RAG_VEC_DIR",
        str(Path.home() / ".local" / "share" / "cairnsearch" / "vectors"),
    )
).expanduser()
_CANONICAL_VEC_DB = _CANONICAL_VEC_DIR / "vectors.db"


def _normalize_ollama_url(raw_url: str) -> str:
    u = (raw_url or "http://localhost:11434").strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = f"http://{u}"
    return u.replace("http://0.0.0.0:", "http://localhost:")


_DEFAULT_EMBED_MODEL = os.environ.get("FARISTOL_RAG_EMBED_MODEL", "embeddinggemma-2:latest")
_DEFAULT_LLM_MODEL = os.environ.get("FARISTOL_RAG_LLM_MODEL", "gemma4:e2b")
_OLLAMA_URL = _normalize_ollama_url(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))

_DOC_EXTENSIONS = (".pdf", ".html", ".htm")
_NOTE_EXTENSIONS = (".md", ".org")

# Inicialització mandrosa (singleton) dels components RAG
_RAG_INITIALIZED = False
_VECTOR_STORE: Optional[VectorStore] = None
_EMBEDDER: Optional[OllamaEmbedder] = None
_RETRIEVER: Optional[HybridRetriever] = None
_CHUNKER: Optional[DocumentChunker] = None
_PDF_EXTRACTOR: Optional[PDFExtractor] = None
_HTML_EXTRACTOR: Optional[HtmlExtractor] = None

# Estat i sincronització d'indexació en segon pla
_INDEX_LOCK = threading.Lock()
_PENDING_AUTO_QUEUE: List[str] = []
_INDEXING_STATE: Dict[str, object] = {
    "active": False,
    "current_file": "",
    "done": 0,
    "total": 0,
    "chunks_added": 0,
    "message": "En repòs",
    "last_error": "",
    "started_at": 0.0,
}


def _get_rag_components():
    """Inicialitza i retorna (store, embedder, retriever, chunker, pdf_ext, html_ext)."""
    global _RAG_INITIALIZED, _VECTOR_STORE, _EMBEDDER, _RETRIEVER, _CHUNKER, _PDF_EXTRACTOR, _HTML_EXTRACTOR
    if not _RAG_INITIALIZED:
        _CANONICAL_VEC_DIR.mkdir(parents=True, exist_ok=True)
        set_rag_config(
            RAGConfig(
                strict_local=True,
                embedding_provider=EmbeddingProvider.OLLAMA,
                embedding_model=_DEFAULT_EMBED_MODEL,
                llm_provider=LLMProvider.OLLAMA,
                ollama_model=_DEFAULT_LLM_MODEL,
                ollama_base_url=_OLLAMA_URL,
                hybrid_search=True,
                bm25_weight=0.35,
                vector_weight=0.65,
                vector_store_path=str(_CANONICAL_VEC_DIR),
            )
        )
        _VECTOR_STORE = VectorStore(db_path=_CANONICAL_VEC_DB)
        _EMBEDDER = OllamaEmbedder(model=_DEFAULT_EMBED_MODEL, base_url=_OLLAMA_URL)
        _RETRIEVER = HybridRetriever(vector_store=_VECTOR_STORE, embedder=_EMBEDDER)
        _CHUNKER = DocumentChunker(chunk_size=500, chunk_overlap=50)
        _PDF_EXTRACTOR = PDFExtractor()
        _HTML_EXTRACTOR = HtmlExtractor()
        _RAG_INITIALIZED = True
    return _VECTOR_STORE, _EMBEDDER, _RETRIEVER, _CHUNKER, _PDF_EXTRACTOR, _HTML_EXTRACTOR


def _stable_doc_id(filename: str) -> int:
    """Genera un identificador enter estable i determinista per a cada fitxer (PDF/HTML o Nota)."""
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:12]
    return int(digest, 16) % 2_000_000_000


def _es_fitxer_valid(p: Path) -> bool:
    """Descarta fitxers ocults, temporals, de bloqueig (.lock), còpies .bak o buits."""
    if not p or not p.is_file():
        return False
    name = p.name
    if name.startswith((".", "#", "?")) or name.endswith(("~", ".bak", ".lock", ".tmp")):
        return False
    ext = p.suffix.lower()
    if ext not in _DOC_EXTENSIONS and ext not in _NOTE_EXTENSIONS:
        return False
    try:
        return p.stat().st_size > 20
    except Exception:
        return False


def _resoldre_ruta_fitxer(filename: str) -> Optional[Path]:
    """Resol la ruta física d'un PDF/HTML (a FONTS_DIR) o d'una Nota (a NOTAS_DIR)."""
    clean_name = Path(filename).name
    if not clean_name:
        return None
    ext = Path(clean_name).suffix.lower()
    if ext in _NOTE_EXTENSIONS:
        cand = NOTAS_DIR / clean_name
        return cand if _es_fitxer_valid(cand) else None
    if ext in _DOC_EXTENSIONS:
        cand = FONTS_DIR / clean_name
        return cand if _es_fitxer_valid(cand) else None
    return None


def _comprovar_ollama_models() -> dict:
    """Comprova si Ollama està actiu i quins models hi ha disponibles."""
    try:
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return {
                "online": True,
                "models": models,
                "has_embed": any(_DEFAULT_EMBED_MODEL.split(":")[0] in m for m in models),
                "has_llm": any(_DEFAULT_LLM_MODEL.split(":")[0] in m for m in models),
            }
    except Exception:
        return {"online": False, "models": [], "has_embed": False, "has_llm": False}


def _resoldre_metadata_nota(filename: str) -> dict:
    """Resol citekey, títol descriptiu, tipus i etiquetes per a un document PDF/HTML o Nota."""
    clean_name = Path(filename).name
    ext = Path(clean_name).suffix.lower()
    is_note = ext in _NOTE_EXTENSIONS
    stem = extraure_identificador_fitxer(clean_name)
    citekey = stem
    title = clean_name
    tags: List[str] = []
    associated_pdf: Optional[str] = None

    if is_note:
        nota_cand = NOTAS_DIR / clean_name
        for doc_ext in (".pdf", ".html", ".htm"):
            p_doc = FONTS_DIR / f"{stem}{doc_ext}"
            if p_doc.is_file():
                associated_pdf = p_doc.name
                break
    else:
        nota_cand = NOTAS_DIR / f"@{stem}.md"
        if not nota_cand.exists():
            nota_cand = NOTAS_DIR / f"{stem}.md"

    if nota_cand.exists():
        try:
            txt = nota_cand.read_text(encoding="utf-8", errors="replace")
            m_title = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', txt, re.M)
            if m_title and m_title.group(1).strip():
                title = m_title.group(1).strip()
            else:
                m_h1 = re.search(r'^#\s+(.+)$', txt, re.M)
                if m_h1 and m_h1.group(1).strip():
                    title = m_h1.group(1).strip()
            m_ck = re.search(r'^citekey:\s*["\']?@?([^"\'\n\r]+)["\']?\s*$', txt, re.M)
            if m_ck and m_ck.group(1).strip():
                citekey = m_ck.group(1).strip().lstrip("@")
            tags = extraure_tags_nota(nota_cand.name, txt)
        except Exception:
            pass

    return {
        "filename": clean_name,
        "citekey": citekey,
        "title": title,
        "tags": tags,
        "is_note": is_note,
        "note_filename": nota_cand.name if nota_cand.exists() else None,
        "associated_pdf": associated_pdf,
    }


def _obtenir_items_colleccio_recursiu(col_id: str) -> Tuple[set[str], set[str]]:
    """Retorna (pdfs_set, notes_set) d'una carpeta i de totes les seues subcarpetes."""
    dades = _llegir_colleccions_nolock()
    cols = dades.get("collections", [])
    by_parent: Dict[Optional[str], List[dict]] = {}
    by_id: Dict[str, dict] = {}
    for c in cols:
        cid = c.get("id")
        if cid:
            by_id[cid] = c
        pid = c.get("parent_id") or None
        by_parent.setdefault(pid, []).append(c)

    if col_id not in by_id:
        return set(), set()

    pdfs_out: set[str] = set()
    notes_out: set[str] = set()
    stack = [by_id[col_id]]
    visited = set()
    while stack:
        cur = stack.pop()
        cid = cur.get("id")
        if cid in visited:
            continue
        visited.add(cid)
        for p in cur.get("pdfs", []) or []:
            pdfs_out.add(p)
            stem = extraure_identificador_fitxer(p)
            for cand_n in (f"@{stem}.md", f"{stem}.md", f"{stem}.org"):
                if (NOTAS_DIR / cand_n).is_file():
                    notes_out.add(cand_n)
        for n in cur.get("notes", []) or []:
            notes_out.add(n)
            for p in obtenir_pdfs_associats_nota(n):
                pdfs_out.add(p)
        for child in by_parent.get(cid, []):
            stack.append(child)

    return pdfs_out, notes_out


def _llistar_fonts_scope(
    collection_id: Optional[str] = None,
    tag: Optional[str] = None,
    include_notes: bool = True,
) -> List[dict]:
    """Llista documents (.pdf, .html) i notes (.md, .org) dins l'abast indicat."""
    store, _, _, _, _, _ = _get_rag_components()
    indexed_map = store.get_indexed_files_map()

    allowed_pdfs: Optional[set[str]] = None
    allowed_notes: Optional[set[str]] = None
    if collection_id and collection_id not in ("all", "null", ""):
        if collection_id == "unfiled":
            dades = _llegir_colleccions_nolock()
            all_filed_pdfs: set[str] = set()
            all_filed_notes: set[str] = set()
            for c in dades.get("collections", []):
                for p in c.get("pdfs", []) or []:
                    all_filed_pdfs.add(p)
                for n in c.get("notes", []) or []:
                    all_filed_notes.add(n)
                    for p in obtenir_pdfs_associats_nota(n):
                        all_filed_pdfs.add(p)
            allowed_pdfs = {
                p.name
                for ext in _DOC_EXTENSIONS
                for p in FONTS_DIR.glob(f"*{ext}")
                if _es_fitxer_valid(p) and p.name not in all_filed_pdfs
            }
            allowed_notes = {
                p.name
                for ext in _NOTE_EXTENSIONS
                for p in NOTAS_DIR.glob(f"*{ext}")
                if _es_fitxer_valid(p) and p.name not in all_filed_notes
            }
        else:
            allowed_pdfs, allowed_notes = _obtenir_items_colleccio_recursiu(collection_id)

    sources = []

    def _afegir_fitxer(p: Path, kind: str):
        fname = p.name
        meta = _resoldre_metadata_nota(fname)
        if tag and tag.strip():
            t_clean = tag.strip().lstrip("#").lower()
            if not any(t.lower() == t_clean for t in meta["tags"]):
                return
        try:
            st = p.stat()
            size = st.st_size
            mtime = st.st_mtime
        except Exception:
            size = 0
            mtime = 0.0

        idx_info = indexed_map.get(fname)
        is_indexed = bool(idx_info and idx_info.get("chunk_count", 0) > 0)
        chunk_count = idx_info.get("chunk_count", 0) if idx_info else 0

        sources.append(
            {
                "id": fname,
                "filename": fname,
                "title": meta["title"],
                "citekey": meta["citekey"],
                "tags": meta["tags"],
                "type": kind,  # "pdf" | "html" | "note"
                "size": size,
                "mtime": mtime,
                "indexed": is_indexed,
                "chunk_count": chunk_count,
            }
        )

    for ext in _DOC_EXTENSIONS:
        for p in FONTS_DIR.glob(f"*{ext}"):
            if not _es_fitxer_valid(p):
                continue
            if allowed_pdfs is not None and p.name not in allowed_pdfs:
                continue
            kind = "html" if p.suffix.lower() in (".html", ".htm") else "pdf"
            _afegir_fitxer(p, kind)

    if include_notes:
        for ext in _NOTE_EXTENSIONS:
            for p in NOTAS_DIR.glob(f"*{ext}"):
                if not _es_fitxer_valid(p):
                    continue
                if allowed_notes is not None and p.name not in allowed_notes:
                    continue
                _afegir_fitxer(p, "note")

    sources.sort(key=lambda s: (not s["indexed"], s["type"] == "note", s["filename"].lower()))
    return sources


def _extraure_text_nota_estructurat(file_path: Path, meta_nota: dict) -> str:
    """Converteix una nota Markdown/Org en text paginat i jeràrquic per a DocumentChunker."""
    raw = file_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return ""

    # Eliminar frontmatter YAML perquè no contamine el vector semàntic (ja tenim title/tags en metadata)
    body = re.sub(r"^---\s*\n[\s\S]*?\n---\s*\n?", "", raw, count=1).strip()
    if not body:
        body = raw

    titol = meta_nota.get("title") or file_path.stem
    return f"[Page 1] [Context: Nota Personal: {titol}]\n# Nota Personal: {titol}\n{body}"


def _indexar_fitxers_sync(filenames: List[str], force: bool = False) -> dict:
    """Executa la indexació amb Docling / HTML / Markdown + embeddinggemma-2 en un fil de treball."""
    with _INDEX_LOCK:
        store, embedder, _, chunker, pdf_ext, html_ext = _get_rag_components()
        indexed_map = store.get_indexed_files_map()

        to_process: List[Path] = []
        vistos = set()
        for fname in filenames:
            clean_name = Path(fname).name
            if not clean_name or clean_name in vistos:
                continue
            vistos.add(clean_name)
            p = _resoldre_ruta_fitxer(clean_name)
            if not p:
                continue
            if not force and clean_name in indexed_map:
                info = indexed_map[clean_name]
                st = p.stat()
                if info.get("chunk_count", 0) > 0 and abs(st.st_mtime - float(info.get("mtime", 0))) < 2.0:
                    continue
            to_process.append(p)

        _INDEXING_STATE["active"] = True
        _INDEXING_STATE["done"] = 0
        _INDEXING_STATE["total"] = len(to_process)
        _INDEXING_STATE["chunks_added"] = 0
        _INDEXING_STATE["last_error"] = ""
        _INDEXING_STATE["started_at"] = time.time()

        if not to_process:
            _INDEXING_STATE["active"] = False
            _INDEXING_STATE["current_file"] = ""
            _INDEXING_STATE["message"] = "Tots els documents seleccionats ja estan indexats i al dia."
            return {"indexed_files": 0, "chunks_added": 0, "message": _INDEXING_STATE["message"]}

        files_ok = 0
        total_chunks_added = 0

        try:
            for idx, file_path in enumerate(to_process, start=1):
                fname = file_path.name
                _INDEXING_STATE["current_file"] = fname
                _INDEXING_STATE["message"] = f"Indexant ({idx}/{len(to_process)}): {fname}..."

                try:
                    ext = file_path.suffix.lower()
                    meta_nota = _resoldre_metadata_nota(fname)
                    extraction_method = "docling+hybrid-tables"

                    if ext in _NOTE_EXTENSIONS:
                        text_out = _extraure_text_nota_estructurat(file_path, meta_nota)
                        extraction_method = "markdown-note"
                        success = bool(text_out.strip())
                        err_msg = "Nota buida"
                    elif ext in (".html", ".htm"):
                        res = html_ext.extract(file_path)
                        success = res.success
                        text_out = res.text or ""
                        err_msg = res.error or ""
                        extraction_method = res.extraction_method or "html"
                        if success and text_out and not text_out.lstrip().startswith("[Page "):
                            text_out = f"[Page 1] [Context: {meta_nota['title']}]\n{text_out}"
                    else:
                        res = pdf_ext.extract(file_path)
                        success = res.success
                        text_out = res.text or ""
                        err_msg = res.error or ""
                        extraction_method = res.extraction_method or "docling+hybrid-tables"

                    if not success or not text_out.strip():
                        logger.warning(f"No s'ha pogut extraure text de {fname}: {err_msg}")
                        _INDEXING_STATE["done"] = idx
                        continue

                    doc_id = _stable_doc_id(fname)
                    store.delete_by_doc_id(doc_id)

                    chunks = chunker.chunk_document(
                        doc_id=doc_id,
                        file_path=str(file_path.resolve()),
                        filename=fname,
                        content=text_out,
                        metadata={
                            "citekey": meta_nota["citekey"],
                            "doc_title": meta_nota["title"],
                            "source_kind": "note" if ext in _NOTE_EXTENSIONS else ("html" if ext in (".html", ".htm") else "pdf"),
                        },
                    )

                    if chunks:
                        embeddings = embedder.embed_batch([c.content for c in chunks])
                        added = store.add_chunks(chunks, embeddings)
                        st = file_path.stat()
                        store.record_indexed_file(
                            file_path=str(file_path.resolve()),
                            filename=fname,
                            doc_id=doc_id,
                            mtime=st.st_mtime,
                            size=st.st_size,
                            chunk_count=added,
                            extraction_method=extraction_method,
                        )
                        total_chunks_added += added
                        _INDEXING_STATE["chunks_added"] = total_chunks_added
                        files_ok += 1
                except Exception as exc_file:
                    logger.error(f"Error indexant {fname}: {exc_file}")
                    _INDEXING_STATE["last_error"] = f"{fname}: {exc_file}"

                _INDEXING_STATE["done"] = idx

            _INDEXING_STATE["message"] = (
                f"Indexació completada: {files_ok} documents ({total_chunks_added} fragments)."
            )
        finally:
            _INDEXING_STATE["active"] = False
            _INDEXING_STATE["current_file"] = ""

    # Si s'han encuat nous fitxers mentre indexàvem, processar-los ara
    extra_batch: List[str] = []
    if _PENDING_AUTO_QUEUE:
        extra_batch = list(dict.fromkeys(_PENDING_AUTO_QUEUE))
        _PENDING_AUTO_QUEUE.clear()
    if extra_batch:
        _indexar_fitxers_sync(extra_batch, force=False)

    return {
        "indexed_files": files_ok,
        "chunks_added": total_chunks_added,
        "message": _INDEXING_STATE["message"],
    }


def encuar_indexacio_fons(filenames: List[str]) -> None:
    """
    Encola silenciosament documents nous/modificats per indexar-los en segon pla.
    No s'executa automàticament durant tests unitaris (`PYTEST_CURRENT_TEST`).
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("FARISTOL_RAG_AUTOINDEX", "1") == "0":
        return
    clean = [Path(f).name for f in filenames if f]
    if not clean:
        return

    if _INDEXING_STATE.get("active"):
        for f in clean:
            if f not in _PENDING_AUTO_QUEUE:
                _PENDING_AUTO_QUEUE.append(f)
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_indexar_fitxers_sync, clean, False))
    except RuntimeError:
        t = threading.Thread(target=_indexar_fitxers_sync, args=(clean, False), daemon=True)
        t.start()


def eliminar_documents_del_rag(filenames: List[str]) -> int:
    """Elimina de `vectors.db` els fragments i empremtes dels documents enviats a la Paperera."""
    if not filenames:
        return 0
    try:
        store, _, _, _, _, _ = _get_rag_components()
        total_deleted = 0
        for fname in filenames:
            clean = Path(fname).name
            if not clean:
                continue
            total_deleted += store.delete_by_doc_id(_stable_doc_id(clean))
            total_deleted += store.delete_by_filename(clean)
        return total_deleted
    except Exception as exc:
        logger.warning(f"No s'han pogut netejar fragments del RAG per a {filenames}: {exc}")
        return 0


def _netejar_fragment_per_ressaltat(chunk_text: str) -> str:
    """Extreu una frase neta del chunk (sense etiquetes [Page X] ni [Context: ...]) perquè PDF.js la ressalti."""
    lines = []
    for ln in (chunk_text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("[Page ", "[Context:", "[Table ", "[Table Facts", "| ---", "|---")):
            continue
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^[-*•]\s*(\*\*\d+\.\d+\*\*\s*)?", "", s)
        s = s.replace("|", " ").strip()
        if len(s) >= 25:
            lines.append(s)
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:220] if joined else (chunk_text or "")[:180]


def _formatar_resposta_evidencies(chunks: List[RetrievalResult]) -> str:
    """Construeix una resposta estructurada immediata (~50-170ms) amb insígnies [1], [2] convertibles a Pandoc."""
    parts = [
        f"S'han recuperat **{len(chunks)} fragments d'alta precisió** mitjançant cerca híbrida (**BM25 FTS5 + embeddinggemma-2**):"
    ]
    for i, ch in enumerate(chunks, start=1):
        meta = ch.metadata or {}
        ctx = meta.get("context", "")
        score_str = f"{ch.score:.2f}"
        body_lines = []
        for ln in (ch.content or "").splitlines():
            s = ln.strip()
            if s.startswith(("[Page ", "[Context:")):
                continue
            body_lines.append(s)
        clean_body = "\n".join(body_lines).strip()
        if len(clean_body) > 520:
            clean_body = clean_body[:520].rsplit(" ", 1)[0] + "..."

        header_ctx = f"**{ctx}** " if ctx else f"**{ch.filename}** "
        parts.append(f"### {i}. {header_ctx}[{i}] *(score: `{score_str}`)*\n{clean_body}")

    return "\n\n".join(parts)


def _construir_prompts_sintesi(query: str, chunks: List[RetrievalResult]) -> tuple[str, str]:
    ctx_blocks = []
    for i, ch in enumerate(chunks, start=1):
        meta = ch.metadata or {}
        page = meta.get("page", 1)
        ctx = meta.get("context", "")
        kind = meta.get("source_kind", "pdf")
        label = "Nota Personal" if kind == "note" else "Document"
        ctx_blocks.append(
            f"FONT [{i}] — {label}: {ch.filename} (Pàgina {page}) | Secció: {ctx}\n{ch.content}"
        )

    context_str = "\n\n---\n\n".join(ctx_blocks)
    system_prompt = (
        "Ets l'assistent d'investigació local de Faristol. Respon en el mateix idioma de la pregunta "
        "(català/valencià, castellà o anglès) basant-te EXCLUSIVAMENT en els fragments proporcionats.\n"
        "REGLA D'OR DE CITACIÓ: Cita sempre les afirmacions i dades utilitzant el número de font entre "
        "claudàtors com [1], [2], [1, 3] (que Faristol convertirà automàticament a format Pandoc [@citekey, p. X]). "
        "Quan hi haja dades de '[Table Facts]', copia els valors numèrics exactes sense confondre columnes. "
        "Omet dades administratives secundàries (com ara beques de finançament, codis de projecte o rols d'autoria) "
        "tret que l'usuari ho pregunte explícitament."
    )
    user_prompt = f"FRAGMENTS RECUPERATS:\n{context_str}\n\nPREGUNTA: {query}\n\nRESPOSTA FONAMENTADA AMB CITES [1], [2]:"
    return system_prompt, user_prompt


def _sintetitzar_amb_ollama(query: str, chunks: List[RetrievalResult], model_name: Optional[str] = None) -> str:
    """Genera una síntesi fonamentada amb cites [1], [2], ... usant el model local d'Ollama."""
    llm = OllamaLLM(model=model_name or _DEFAULT_LLM_MODEL, base_url=_OLLAMA_URL)
    system_prompt, user_prompt = _construir_prompts_sintesi(query, chunks)
    return llm.generate(user_prompt, system=system_prompt)


def _preparar_metadades_consulta(req: LocalRagQueryRequest) -> tuple[list[RetrievalResult], dict, float]:
    t0 = time.perf_counter()
    _, _, retriever, _, _, _ = _get_rag_components()

    allowed_files: Optional[List[str]] = None
    if req.filenames and len(req.filenames) > 0:
        allowed_files = [Path(f).name for f in req.filenames if f]
    elif req.collection_id and req.collection_id not in ("all", "null", ""):
        scope_sources = _llistar_fonts_scope(req.collection_id, None, include_notes=req.include_notes)
        allowed_files = [s["filename"] for s in scope_sources]
    elif not req.include_notes:
        scope_sources = _llistar_fonts_scope(None, None, include_notes=False)
        allowed_files = [s["filename"] for s in scope_sources]

    results = retriever.retrieve(
        req.query.strip(),
        top_k=max(1, min(req.top_k or 6, 15)),
        file_paths=allowed_files,
    )
    took_retrieval_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    citations: Dict[str, str] = {}
    citation_details: Dict[str, dict] = {}
    references: List[dict] = []
    sources_map: Dict[str, dict] = {}
    citekey_cache: Dict[str, dict] = {}

    for idx, r in enumerate(results, start=1):
        c_num = str(idx)
        fname = r.filename
        if fname not in citekey_cache:
            citekey_cache[fname] = _resoldre_metadata_nota(fname)
        doc_meta = citekey_cache[fname]
        citekey = doc_meta["citekey"]
        source_id = fname

        r_meta = r.metadata or {}
        source_kind = r_meta.get("source_kind") or ("note" if doc_meta.get("is_note") else "pdf")

        sources_map[source_id] = {
            "id": source_id,
            "title": doc_meta.get("title") or fname,
            "citekey": citekey,
            "source_kind": source_kind,
            "note_filename": doc_meta.get("note_filename"),
        }

        page_num = int(r_meta.get("page") or 1)
        ctx_str = str(r_meta.get("context") or "")
        cited_snippet = _netejar_fragment_per_ressaltat(r.content)

        citations[c_num] = source_id
        detail_obj = {
            "source_id": source_id,
            "citation_number": c_num,
            "page": page_num,
            "citekey": citekey,
            "pdf": doc_meta.get("associated_pdf") if source_kind == "note" and doc_meta.get("associated_pdf") else fname,
            "note_filename": fname if source_kind == "note" else doc_meta.get("note_filename"),
            "source_kind": source_kind,
            "cited_text": cited_snippet,
            "context": ctx_str,
            "score": round(float(r.score), 3),
            "source_type": r.source,
        }
        citation_details[c_num] = detail_obj
        references.append(detail_obj)

    meta_payload = {
        "citations": citations,
        "citation_details": citation_details,
        "references": references,
        "sources": sources_map,
    }
    return results, meta_payload, took_retrieval_ms


@router.get("/rag/status")
async def rag_obtenir_estat():
    """Retorna l'estat del motor RAG Híbrid Local, d'Ollama i de l'índex vectorial/BM25."""
    def _sync_status():
        store, _, _, _, _, _ = _get_rag_components()
        stats = store.get_stats()
        indexed_map = store.get_indexed_files_map()
        ollama_info = _comprovar_ollama_models()
        total_docs = sum(1 for ext in _DOC_EXTENSIONS for p in FONTS_DIR.glob(f"*{ext}") if _es_fitxer_valid(p))
        total_notes = sum(1 for ext in _NOTE_EXTENSIONS for p in NOTAS_DIR.glob(f"*{ext}") if _es_fitxer_valid(p))
        return {
            "status": "ok",
            "ollama": ollama_info,
            "embedding_model": _DEFAULT_EMBED_MODEL,
            "llm_model": _DEFAULT_LLM_MODEL,
            "vector_store": stats,
            "total_library_files": total_docs + total_notes,
            "total_docs": total_docs,
            "total_notes": total_notes,
            "indexed_files_count": len(indexed_map),
            "indexing": dict(_INDEXING_STATE),
        }

    return await asyncio.to_thread(_sync_status)


@router.get("/rag/sources")
async def rag_llistar_fonts(
    collection_id: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    include_notes: bool = Query(True),
):
    """Llista els documents i notes disponibles per a l'abast actual i el seu estat d'indexació."""
    sources = await asyncio.to_thread(_llistar_fonts_scope, collection_id, tag, include_notes)
    indexed_count = sum(1 for s in sources if s["indexed"])
    total_chunks = sum(s["chunk_count"] for s in sources)
    return {
        "status": "ok",
        "collection_id": collection_id,
        "tag": tag,
        "sources": sources,
        "total": len(sources),
        "indexed_count": indexed_count,
        "total_chunks": total_chunks,
    }


@router.post("/rag/index")
async def rag_indexar_documents(req: LocalRagIndexRequest):
    """Inicia la indexació incremental (Docling + Notes + BM25 + embeddinggemma-2) en segon pla."""
    if _INDEXING_STATE.get("active"):
        return {
            "status": "already_running",
            "indexing": dict(_INDEXING_STATE),
        }

    if req.filenames:
        target_filenames = [Path(f).name for f in req.filenames if f]
    else:
        sources = await asyncio.to_thread(_llistar_fonts_scope, req.collection_id, None, req.include_notes)
        target_filenames = [s["filename"] for s in sources]

    if not target_filenames:
        return {"status": "empty", "message": "Cap document trobat per indexar en aquest abast."}

    asyncio.create_task(asyncio.to_thread(_indexar_fitxers_sync, target_filenames, req.force))

    return {
        "status": "started",
        "queued_files": len(target_filenames),
        "indexing": dict(_INDEXING_STATE),
    }


@router.post("/rag/query")
async def rag_consultar(req: LocalRagQueryRequest):
    """Executa una consulta al RAG Híbrid Local i retorna evidències o síntesi IA amb cites Pandoc."""
    query_clean = (req.query or "").strip()
    if not query_clean:
        raise HTTPException(status_code=400, detail="La consulta no pot estar buida")

    def _executar_consulta_sync():
        t0 = time.perf_counter()
        results, meta_payload, took_retrieval_ms = _preparar_metadades_consulta(req)

        if not results:
            return {
                "status": "ok",
                "mode": req.mode,
                "question": query_clean,
                "took_ms": took_retrieval_ms,
                "retrieval_ms": took_retrieval_ms,
                "answer": (
                    "No s'han trobat fragments indexats per als documents seleccionats. "
                    "Fes clic al botó **«⚡ Indexar»** a la part superior per indexar aquesta carpeta amb Docling i `embeddinggemma-2`."
                ),
                **meta_payload,
            }

        if req.mode == "synthesis":
            try:
                answer = _sintetitzar_amb_ollama(query_clean, results, model_name=req.llm_model)
            except Exception as exc_llm:
                logger.warning(f"Avís: fallada en síntesi Ollama ({exc_llm}), mostrant evidències directes.")
                answer = (
                    f"⚠️ *No s'ha pogut generar la síntesi amb `{req.llm_model or _DEFAULT_LLM_MODEL}` ({exc_llm}). "
                    f"Mostrant evidències directes:*\n\n"
                    + _formatar_resposta_evidencies(results)
                )
        else:
            answer = _formatar_resposta_evidencies(results)

        total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "status": "ok",
            "mode": req.mode,
            "question": query_clean,
            "took_ms": total_ms,
            "retrieval_ms": took_retrieval_ms,
            "answer": answer,
            **meta_payload,
        }

    return await asyncio.to_thread(_executar_consulta_sync)


@router.post("/rag/stream")
async def rag_consultar_stream(req: LocalRagQueryRequest):
    """Streaming SSE per a Síntesi IA en temps real: emet `metadata`, `token`s i `done`."""
    query_clean = (req.query or "").strip()
    if not query_clean:
        raise HTTPException(status_code=400, detail="La consulta no pot estar buida")

    t0 = time.perf_counter()
    results, meta_payload, took_retrieval_ms = await asyncio.to_thread(_preparar_metadades_consulta, req)

    async def _event_generator():
        if not results:
            empty_msg = (
                "No s'han trobat fragments indexats per als documents seleccionats. "
                "Fes clic al botó **«⚡ Indexar»** per indexar aquesta carpeta amb Docling i `embeddinggemma-2`."
            )
            yield f"event: metadata\ndata: {json.dumps({'retrieval_ms': took_retrieval_ms, **meta_payload}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'answer': empty_msg, 'took_ms': took_retrieval_ms}, ensure_ascii=False)}\n\n"
            return

        yield f"event: metadata\ndata: {json.dumps({'retrieval_ms': took_retrieval_ms, **meta_payload}, ensure_ascii=False)}\n\n"

        if req.mode != "synthesis":
            ev_answer = _formatar_resposta_evidencies(results)
            yield f"event: done\ndata: {json.dumps({'answer': ev_answer, 'took_ms': took_retrieval_ms}, ensure_ascii=False)}\n\n"
            return

        system_prompt, user_prompt = _construir_prompts_sintesi(query_clean, results)
        llm = OllamaLLM(model=req.llm_model or _DEFAULT_LLM_MODEL, base_url=_OLLAMA_URL)

        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _producer():
            try:
                for token in llm.generate_stream(user_prompt, system=system_prompt):
                    loop.call_soon_threadsafe(q.put_nowait, ("token", token))
                loop.call_soon_threadsafe(q.put_nowait, ("eof", None))
            except Exception as exc:
                loop.call_soon_threadsafe(q.put_nowait, ("err", str(exc)))

        threading.Thread(target=_producer, daemon=True).start()

        acc: List[str] = []
        while True:
            kind, val = await q.get()
            if kind == "token":
                acc.append(val)
                yield f"event: token\ndata: {json.dumps({'delta': val}, ensure_ascii=False)}\n\n"
            elif kind == "err":
                fallback = (
                    f"⚠️ *No s'ha pogut completar el streaming amb Ollama ({val}). Mostrant evidències directes:*\n\n"
                    + _formatar_resposta_evidencies(results)
                )
                total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                yield f"event: done\ndata: {json.dumps({'answer': fallback, 'took_ms': total_ms, 'error': val}, ensure_ascii=False)}\n\n"
                return
            elif kind == "eof":
                final_answer = "".join(acc)
                total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                yield f"event: done\ndata: {json.dumps({'answer': final_answer, 'took_ms': total_ms}, ensure_ascii=False)}\n\n"
                return

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/notes/{filename}/semantic")
async def obtenir_connexions_semantiques(filename: str, top_k: int = Query(5, ge=1, le=12)):
    """Retorna documents o notes semànticament afins calculats sobre la matriu NumPy float32 (<2 ms)."""
    _ruta_segura_nota(filename)

    def _calc_semantic():
        store, embedder, _, _, _, _ = _get_rag_components()
        store._ensure_matrix_cache()
        if store._cached_matrix is None or len(store._cached_rows) == 0:
            return {"filename": filename, "related": []}

        stem = extraure_identificador_fitxer(filename)
        exclude_stems = {stem.lower()}
        exclude_names = {
            filename.lower(),
            f"{stem}.pdf".lower(),
            f"{stem}.html".lower(),
            f"{stem}.htm".lower(),
            f"@{stem}.md".lower(),
            f"{stem}.md".lower(),
        }

        # Intentar obtenir el vector mitjà del fitxer o del seu PDF associat
        target_vecs: List[np.ndarray] = []
        for idx, row in enumerate(store._cached_rows):
            r_fname = (row[3] or "").lower()
            if r_fname in exclude_names:
                target_vecs.append(store._cached_matrix[idx])

        if target_vecs:
            query_vec = np.mean(np.vstack(target_vecs), axis=0)
            norm = float(np.linalg.norm(query_vec))
            if norm > 0:
                query_vec = query_vec / norm
        else:
            # Si encara no està indexat, fer embedding ràpid dels primers 1500 chars de la nota
            nota_path = NOTAS_DIR / Path(filename).name
            if not nota_path.is_file():
                return {"filename": filename, "related": []}
            snippet = nota_path.read_text(encoding="utf-8", errors="replace")[:1500].strip()
            if len(snippet) < 40:
                return {"filename": filename, "related": []}
            try:
                q_emb = np.asarray(embedder.embed(snippet), dtype=np.float32)
                norm = float(np.linalg.norm(q_emb))
                if norm == 0:
                    return {"filename": filename, "related": []}
                query_vec = q_emb / norm
            except Exception:
                return {"filename": filename, "related": []}

        if query_vec.shape[0] != store._cached_matrix.shape[1]:
            return {"filename": filename, "related": []}

        sims = store._cached_matrix @ query_vec
        # Agrupar per document mantenint el màxim + mitjana dels 2 millors chunks
        per_doc: Dict[str, List[float]] = {}
        for idx, score in enumerate(sims.tolist()):
            fname = store._cached_rows[idx][3]
            if not fname:
                continue
            f_low = fname.lower()
            f_stem = extraure_identificador_fitxer(fname).lower()
            if f_low in exclude_names or f_stem in exclude_stems:
                continue
            per_doc.setdefault(fname, []).append(float(score))

        scored_docs = []
        for fname, scores in per_doc.items():
            scores.sort(reverse=True)
            agg = scores[0] if len(scores) == 1 else (0.7 * scores[0] + 0.3 * scores[1])
            if agg < 0.32:
                continue
            meta = _resoldre_metadata_nota(fname)
            scored_docs.append(
                {
                    "filename": fname,
                    "title": meta["title"],
                    "citekey": meta["citekey"],
                    "is_note": meta["is_note"],
                    "note_filename": meta["note_filename"],
                    "similarity": round(agg, 3),
                }
            )

        scored_docs.sort(key=lambda x: x["similarity"], reverse=True)
        return {"filename": filename, "related": scored_docs[:top_k]}

    return await asyncio.to_thread(_calc_semantic)


@router.post("/notes/{filename}/summarize")
async def resumir_nota_amb_rag(filename: str, req: NoteSummarizeRequest):
    """Genera automàticament un Resum i Idees Clau amb cites Pandoc a partir del PDF/document indexat."""
    nota_path = _ruta_segura_nota(filename)
    if not nota_path.is_file():
        raise HTTPException(status_code=404, detail="Nota no trobada")

    def _summarize_sync():
        stem = extraure_identificador_fitxer(filename)
        store, _, retriever, _, pdf_ext, html_ext = _get_rag_components()

        doc_fname: Optional[str] = None
        for ext in _DOC_EXTENSIONS:
            cand = FONTS_DIR / f"{stem}{ext}"
            if cand.is_file():
                doc_fname = cand.name
                break

        chunks_text: List[str] = []
        if doc_fname:
            # Cercar primers chunks / seccions clau si ja està indexat
            indexed = store.get_indexed_files_map()
            if doc_fname in indexed:
                results = retriever.retrieve(
                    "abstract introduction objective methodology results findings conclusions",
                    top_k=7,
                    file_paths=[doc_fname],
                )
                for i, r in enumerate(results, start=1):
                    p = (r.metadata or {}).get("page", 1)
                    chunks_text.append(f"[Fragment {i} - @{stem}, p. {p}]\n{r.content}")
            else:
                # Extracció ràpida de les primeres pàgines si encara no s'ha indexat
                p_obj = FONTS_DIR / doc_fname
                if p_obj.suffix.lower() == ".pdf":
                    res = pdf_ext.extract(p_obj, page_range=(1, 6))
                else:
                    res = html_ext.extract(p_obj)
                if res.success and res.text:
                    chunks_text.append(res.text[:6000])

        if not chunks_text:
            nota_txt = nota_path.read_text(encoding="utf-8", errors="replace")
            chunks_text.append(nota_txt[:5000])

        context = "\n\n---\n\n".join(chunks_text)
        llm = OllamaLLM(model=req.model or _DEFAULT_LLM_MODEL, base_url=_OLLAMA_URL)
        system = (
            "Ets un assistent acadèmic de Faristol. Redacta en català/valencià un resum rigorós "
            "i entre 4 i 6 idees clau en format Markdown net basant-te EXCLUSIVAMENT en el text proporcionat. "
            f"Conserva cites exactes en format [@{stem}, p. X] quan la pàgina estiga indicada."
        )
        prompt = (
            f"TEXT DEL DOCUMENT ({doc_fname or filename}):\n{context}\n\n"
            "Genera exactament aquestes dues seccions Markdown:\n"
            "## Resum / Abstract\n(un paràgraf clar i complet)\n\n"
            "## Idees clau\n- ...\n"
        )
        summary_md = llm.generate(prompt, system=system).strip()
        return {
            "status": "ok",
            "filename": filename,
            "source_doc": doc_fname,
            "markdown": summary_md,
        }

    try:
        return await asyncio.to_thread(_summarize_sync)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No s'ha pogut generar el resum: {exc}")
