"""
API FastAPI per a Faristol - Entorn de lectura, anotació i síntesi bibliogràfica.

L'API s'organitza en subrouters per domini (notes, PDF, biblioteca, cerca,
banc de treball, col·leccions i NotebookLM) que es munten davall d'un router
principal amb el prefix ``/api``.
"""
from fastapi import APIRouter

from faristol.api import collections, export, library, nlm, notes, pdf, rag, search, workbench

router = APIRouter(prefix="/api")

# L'orde de muntatge conserva l'orde original de les rutes.
for _subrouter in (notes, pdf, library, search, workbench, collections, nlm, rag, export):
    router.include_router(_subrouter.router)

__all__ = ["router"]
