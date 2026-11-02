"""NotebookLM (RAG) i sincronització de col·leccions."""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from faristol.api.common import _ruta_segura_nota, logger
from faristol.api.models import NlmAddUrlRequest, NlmDeleteRequest, NlmQueryRequest, NlmSintesiRequest
from faristol.config import FONTS_DIR, NOTAS_DIR
from faristol.fsatomic import bloqueig, escriure_atomic

router = APIRouter()

def _validar_id_nlm(val: str, camp: str = "id") -> str:
    net = (val or "").strip()
    if not net or net.startswith("-") or "\x00" in net:
        raise HTTPException(status_code=400, detail=f"Paràmetre '{camp}' no vàlid")
    return net

def _nlm_bin() -> Optional[str]:
    """Resol l'executable de la CLI de NotebookLM ('nlm').

    Ordre de prioritat: la variable d'entorn FARISTOL_NLM_BIN, el PATH del
    sistema i, finalment, la ubicació habitual d'instal·lació per usuari
    (~/.local/bin/nlm). Així no cal fixar cap ruta personal al codi.
    """
    override = os.environ.get("FARISTOL_NLM_BIN", "").strip()
    if override:
        return override
    trobat = shutil.which("nlm")
    if trobat:
        return trobat
    candidat = Path.home() / ".local" / "bin" / "nlm"
    if candidat.is_file():
        return str(candidat)
    return None

async def _exec_nlm_cli(args: List[str], parse_json: bool = True, timeout: float = 90.0):
    nlm_bin = _nlm_bin()
    if not nlm_bin:
        return False, "La comanda 'nlm' (NotebookLM CLI) no està instal·lada o no és al PATH.", None
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
