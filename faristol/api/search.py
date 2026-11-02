"""Cerca acadèmica federada, importació Zotero-less i MCP local."""

import asyncio
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from faristol.api.common import _ruta_segura_nota, logger
from faristol.api.models import AcademicImportRequest
from faristol.config import (
    MCP_ACADEMIC_DIR,
    MCP_OPEN_DATA_DIR,
    MCP_REAL_ESTATE_DIR,
)
from faristol.fsatomic import bloqueig, escriure_atomic

router = APIRouter()

_MCP_OPEN_DATA_MOD = None
_MCP_REAL_ESTATE_MOD = None

def _get_mcp_open_data():
    global _MCP_OPEN_DATA_MOD
    if _MCP_OPEN_DATA_MOD is not None:
        return _MCP_OPEN_DATA_MOD
    open_dir = MCP_OPEN_DATA_DIR
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
    real_dir = MCP_REAL_ESTATE_DIR
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
    mcp_academic_dir = MCP_ACADEMIC_DIR
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


