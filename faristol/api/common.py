"""Utilitats compartides de l'API (logger i validació de rutes)."""

import logging
import urllib.parse
from pathlib import Path

from fastapi import HTTPException

from faristol.config import FONTS_DIR, NOTAS_DIR

logger = logging.getLogger("faristol.api")

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
