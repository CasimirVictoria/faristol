"""
Motor RAG Híbrid Local (Docling + BM25 FTS5 + embeddinggemma-2 en NumPy float32) integrat a Faristol.
Proporciona indexació incremental en segon pla, cerca híbrida instantània (~170 ms) i síntesi amb IA local (Ollama),
conservant citacions exactes en format Pandoc [@citekey, p. X] i salt directe a la pàgina del PDF.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from faristol.api.collections import _llegir_colleccions_nolock
from faristol.api.common import logger
from faristol.api.models import LocalRagIndexRequest, LocalRagQueryRequest
from faristol.config import BIB_FILE_PATH, FONTS_DIR, NOTAS_DIR
from faristol.utils import extraure_identificador_fitxer, extraure_tags_nota, obtenir_pdfs_associats_nota

# Incorporar cairnsearch des del repositori local si no és al PYTHONPATH
_CAIRNSEARCH_SRC = Path.home() / "Projects" / "cairnsearch" / "src"
if _CAIRNSEARCH_SRC.exists() and str(_CAIRNSEARCH_SRC) not in sys.path:
    sys.path.insert(0, str(_CAIRNSEARCH_SRC))

from cairnsearch.extractors.pdf_extractor import PDFExtractor
from cairnsearch.extractors.html_extractor import HtmlExtractor
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
    u = u.replace("http://0.0.0.0:", "http://localhost:")
    return u


_DEFAULT_EMBED_MODEL = os.environ.get("FARISTOL_RAG_EMBED_MODEL", "embeddinggemma-2:latest")
_DEFAULT_LLM_MODEL = os.environ.get("FARISTOL_RAG_LLM_MODEL", "gemma4:e2b")
_OLLAMA_URL = _normalize_ollama_url(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))

# Inicialització mandrosa (singleton) dels components RAG
_RAG_INITIALIZED = False
_VECTOR_STORE: Optional[VectorStore] = None
_EMBEDDER: Optional[OllamaEmbedder] = None
_RETRIEVER: Optional[HybridRetriever] = None
_CHUNKER: Optional[DocumentChunker] = None
_PDF_EXTRACTOR: Optional[PDFExtractor] = None
_HTML_EXTRACTOR: Optional[HtmlExtractor] = None

# Estat d'indexació en segon pla
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
    """Genera un identificador enter estable i determinista per a cada fitxer de la biblioteca."""
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:12]
    return int(digest, 16) % 2_000_000_000


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
    """Resol el citekey, el títol descriptiu i les etiquetes associades a un fitxer PDF/HTML."""
    stem = extraure_identificador_fitxer(filename)
    citekey = stem
    title = filename
    tags: List[str] = []

    nota_cand = NOTAS_DIR / f"@{stem}.md"
    if not nota_cand.exists():
        nota_cand = NOTAS_DIR / f"{stem}.md"

    if nota_cand.exists():
        try:
            txt = nota_cand.read_text(encoding="utf-8", errors="replace")
            m_title = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', txt, re.M)
            if m_title and m_title.group(1).strip():
                title = m_title.group(1).strip()
            m_ck = re.search(r'^citekey:\s*["\']?@?([^"\'\n\r]+)["\']?\s*$', txt, re.M)
            if m_ck and m_ck.group(1).strip():
                citekey = m_ck.group(1).strip().lstrip("@")
            tags = extraure_tags_nota(nota_cand.name, txt)
        except Exception:
            pass

    return {
        "filename": filename,
        "citekey": citekey,
        "title": title,
        "tags": tags,
    }


def _obtenir_pdfs_colleccio_recursiu(col_id: str) -> set[str]:
    """Retorna tots els PDFs/HTMLs d'una carpeta i de totes les seues subcarpetes."""
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
        return set()

    pdfs_out: set[str] = set()
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
        for n in cur.get("notes", []) or []:
            for p in obtenir_pdfs_associats_nota(n):
                pdfs_out.add(p)
        for child in by_parent.get(cid, []):
            stack.append(child)

    return pdfs_out


def _llistar_fonts_scope(
    collection_id: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[dict]:
    """Llista tots els documents (.pdf i .html) dins l'abast indicat amb el seu estat d'indexació."""
    store, _, _, _, _, _ = _get_rag_components()
    indexed_map = store.get_indexed_files_map()

    allowed_by_col: Optional[set[str]] = None
    if collection_id and collection_id not in ("all", "null", ""):
        if collection_id == "unfiled":
            dades = _llegir_colleccions_nolock()
            all_filed: set[str] = set()
            for c in dades.get("collections", []):
                for p in c.get("pdfs", []) or []:
                    all_filed.add(p)
                for n in c.get("notes", []) or []:
                    for p in obtenir_pdfs_associats_nota(n):
                        all_filed.add(p)
            allowed_by_col = {
                p.name
                for ext in ("*.pdf", "*.html")
                for p in FONTS_DIR.glob(ext)
                if p.name not in all_filed
            }
        else:
            allowed_by_col = _obtenir_pdfs_colleccio_recursiu(collection_id)

    sources = []
    for ext in ("*.pdf", "*.html"):
        for p in FONTS_DIR.glob(ext):
            fname = p.name
            if allowed_by_col is not None and fname not in allowed_by_col:
                continue
            meta = _resoldre_metadata_nota(fname)
            if tag and tag.strip():
                t_clean = tag.strip().lstrip("#").lower()
                if not any(t.lower() == t_clean for t in meta["tags"]):
                    continue

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
                    "type": "html" if p.suffix.lower() == ".html" else "pdf",
                    "size": size,
                    "mtime": mtime,
                    "indexed": is_indexed,
                    "chunk_count": chunk_count,
                }
            )

    sources.sort(key=lambda s: (not s["indexed"], s["filename"].lower()))
    return sources


def _indexar_fitxers_sync(filenames: List[str], force: bool = False) -> dict:
    """Executa la indexació amb Docling + embeddinggemma-2 en un fil de treball."""
    store, embedder, _, chunker, pdf_ext, html_ext = _get_rag_components()
    indexed_map = store.get_indexed_files_map()

    to_process: List[Path] = []
    for fname in filenames:
        clean_name = Path(fname).name
        p = FONTS_DIR / clean_name
        if not p.exists() or not p.is_file():
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
                is_html = file_path.suffix.lower() == ".html"
                if is_html:
                    res = html_ext.extract(file_path)
                    if res.success and res.text and not res.text.lstrip().startswith("[Page "):
                        res.text = f"[Page 1] [Context: {fname}]\n{res.text}"
                else:
                    res = pdf_ext.extract(file_path)

                if not res.success or not (res.text or "").strip():
                    logger.warning(f"No s'ha pogut extraure text de {fname}: {res.error}")
                    _INDEXING_STATE["done"] = idx
                    continue

                doc_id = _stable_doc_id(fname)
                store.delete_by_doc_id(doc_id)

                meta_nota = _resoldre_metadata_nota(fname)
                chunks = chunker.chunk_document(
                    doc_id=doc_id,
                    file_path=str(file_path.resolve()),
                    filename=fname,
                    content=res.text,
                    metadata={"citekey": meta_nota["citekey"], "doc_title": meta_nota["title"]},
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
                        extraction_method=res.extraction_method,
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

    return {
        "indexed_files": files_ok,
        "chunks_added": total_chunks_added,
        "message": _INDEXING_STATE["message"],
    }


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


def _formatar_resposta_evidencies(query: str, chunks: List[RetrievalResult], citekey_map: Dict[str, dict]) -> str:
    """Construeix una resposta estructurada immediata (~170ms) amb insígnies [1], [2] convertibles a Pandoc."""
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


def _sintetitzar_amb_ollama(query: str, chunks: List[RetrievalResult], model_name: Optional[str] = None) -> str:
    """Genera una síntesi fonamentada amb cites [1], [2], ... usant el model local d'Ollama."""
    llm = OllamaLLM(model=model_name or _DEFAULT_LLM_MODEL, base_url=_OLLAMA_URL)
    ctx_blocks = []
    for i, ch in enumerate(chunks, start=1):
        meta = ch.metadata or {}
        page = meta.get("page", 1)
        ctx = meta.get("context", "")
        ctx_blocks.append(
            f"FONT [{i}] — Document: {ch.filename} (Pàgina {page}) | Secció: {ctx}\n{ch.content}"
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
    return llm.generate(user_prompt, system=system_prompt)


@router.get("/rag/status")
async def rag_obtenir_estat():
    """Retorna l'estat del motor RAG Híbrid Local, d'Ollama i de l'índex vectorial/BM25."""
    def _sync_status():
        store, _, _, _, _, _ = _get_rag_components()
        stats = store.get_stats()
        indexed_map = store.get_indexed_files_map()
        ollama_info = _comprovar_ollama_models()
        total_lib_files = sum(1 for ext in ("*.pdf", "*.html") for _ in FONTS_DIR.glob(ext))
        return {
            "status": "ok",
            "ollama": ollama_info,
            "embedding_model": _DEFAULT_EMBED_MODEL,
            "llm_model": _DEFAULT_LLM_MODEL,
            "vector_store": stats,
            "total_library_files": total_lib_files,
            "indexed_files_count": len(indexed_map),
            "indexing": dict(_INDEXING_STATE),
        }

    return await asyncio.to_thread(_sync_status)


@router.get("/rag/sources")
async def rag_llistar_fonts(
    collection_id: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
):
    """Llista els documents disponibles per a l'abast actual i indica quins ja estan indexats."""
    sources = await asyncio.to_thread(_llistar_fonts_scope, collection_id, tag)
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
    """Inicia la indexació incremental (Docling + BM25 + embeddinggemma-2) en segon pla."""
    if _INDEXING_STATE.get("active"):
        return {
            "status": "already_running",
            "indexing": dict(_INDEXING_STATE),
        }

    target_filenames: List[str] = []
    if req.filenames:
        target_filenames = [Path(f).name for f in req.filenames if f]
    else:
        sources = await asyncio.to_thread(_llistar_fonts_scope, req.collection_id, None)
        target_filenames = [s["filename"] for s in sources]

    if not target_filenames:
        return {"status": "empty", "message": "Cap document trobat per indexar en aquest abast."}

    # Llançar en segon pla perquè la UI pugui consultar /api/rag/status sense bloquejar-se
    asyncio.create_task(asyncio.to_thread(_indexar_fitxers_sync, target_filenames, req.force))

    return {
        "status": "started",
        "queued_files": len(target_filenames),
        "indexing": dict(_INDEXING_STATE),
    }


@router.post("/rag/query")
async def rag_consultar(req: LocalRagQueryRequest):
    """Executa una consulta al RAG Híbrid Local (BM25 FTS5 + embeddinggemma-2) i retorna cites Pandoc exactes."""
    query_clean = (req.query or "").strip()
    if not query_clean:
        raise HTTPException(status_code=400, detail="La consulta no pot estar buida")

    def _executar_consulta_sync():
        t0 = time.perf_counter()
        store, _, retriever, _, _, _ = _get_rag_components()

        allowed_files: Optional[List[str]] = None
        if req.filenames and len(req.filenames) > 0:
            allowed_files = [Path(f).name for f in req.filenames if f]
        elif req.collection_id and req.collection_id not in ("all", "null", ""):
            scope_sources = _llistar_fonts_scope(req.collection_id, None)
            allowed_files = [s["filename"] for s in scope_sources]

        results = retriever.retrieve(
            query_clean,
            top_k=max(1, min(req.top_k or 6, 15)),
            file_paths=allowed_files,
        )

        took_retrieval_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        if not results:
            return {
                "status": "ok",
                "mode": req.mode,
                "question": query_clean,
                "took_ms": took_retrieval_ms,
                "answer": (
                    "No s'han trobat fragments indexats per als documents seleccionats. "
                    "Fes clic al botó **«⚡ Indexar»** a la part superior per indexar aquesta carpeta amb Docling i `embeddinggemma-2`."
                ),
                "citations": {},
                "citation_details": {},
                "references": [],
                "sources": {},
            }

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

            sources_map[source_id] = {
                "id": source_id,
                "title": fname,
                "citekey": citekey,
            }

            r_meta = r.metadata or {}
            page_num = int(r_meta.get("page") or 1)
            ctx_str = str(r_meta.get("context") or "")
            cited_snippet = _netejar_fragment_per_ressaltat(r.content)

            citations[c_num] = source_id
            detail_obj = {
                "source_id": source_id,
                "citation_number": c_num,
                "page": page_num,
                "citekey": citekey,
                "pdf": fname,
                "cited_text": cited_snippet,
                "context": ctx_str,
                "score": round(float(r.score), 3),
                "source_type": r.source,
            }
            citation_details[c_num] = detail_obj
            references.append(detail_obj)

        if req.mode == "synthesis":
            try:
                answer = _sintetitzar_amb_ollama(query_clean, results, model_name=req.llm_model)
            except Exception as exc_llm:
                logger.warning(f"Avís: fallada en síntesi Ollama ({exc_llm}), mostrant evidències directes.")
                answer = (
                    f"⚠️ *No s'ha pogut generar la síntesi amb `{req.llm_model or _DEFAULT_LLM_MODEL}` ({exc_llm}). "
                    f"Mostrant evidències directes:*\n\n"
                    + _formatar_resposta_evidencies(query_clean, results, citekey_cache)
                )
        else:
            answer = _formatar_resposta_evidencies(query_clean, results, citekey_cache)

        total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "status": "ok",
            "mode": req.mode,
            "question": query_clean,
            "took_ms": total_ms,
            "retrieval_ms": took_retrieval_ms,
            "answer": answer,
            "citations": citations,
            "citation_details": citation_details,
            "references": references,
            "sources": sources_map,
        }

    return await asyncio.to_thread(_executar_consulta_sync)
