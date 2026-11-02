"""Col·leccions virtuals."""

import json
import subprocess
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException

from faristol.api.common import _document_segur
from faristol.api.models import ActualitzarColleccioRequest, GuardarColleccionsRequest, ItemColleccioRequest, NovaColleccioRequest, SetItemCollectionsRequest
from faristol.api.nlm import _exec_nlm_cli
from faristol.config import COLLECTIONS_FILE
from faristol.fsatomic import bloqueig, escriure_atomic, llegir_json_segur
from faristol.utils import obtenir_notes_associades_pdf, obtenir_pdfs_associats_nota

router = APIRouter()

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


