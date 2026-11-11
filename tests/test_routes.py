"""La superfície de l'API ha de romandre estable.

Esta prova bloqueja el conjunt exacte de rutes (mètode + camí) davall de
``/api``. Si s'afig, es renombra o s'elimina un endpoint, esta llista cal
actualitzar-la conscientment.
"""
from faristol.server import app

RUTES_ESPERADES = {
    ("DELETE", "/collections/{col_id}"),
    ("DELETE", "/faristol/collections/{col_id}"),
    ("DELETE", "/notes/{filename}"),
    ("DELETE", "/pdf/highlights/{highlight_id}"),
    ("DELETE", "/trash/permanent/{filename}"),
    ("GET", "/academic/search"),
    ("GET", "/bibtex/references"),
    ("GET", "/collections"),
    ("GET", "/faristol/collections"),
    ("GET", "/faristol/extract-highlights"),
    ("GET", "/faristol/status"),
    ("GET", "/fonts/list"),
    ("GET", "/library/events"),
    ("GET", "/library/status"),
    ("GET", "/nlm/notebooks"),
    ("GET", "/nlm/sources"),
    ("GET", "/notes"),
    ("GET", "/notes/{filename}"),
    ("GET", "/notes/{filename}/connections"),
    ("GET", "/pdf/highlights"),
    ("GET", "/pdf/stream"),
    ("GET", "/rag/sources"),
    ("GET", "/rag/status"),
    ("GET", "/session"),
    ("GET", "/trash/items"),
    ("POST", "/academic/import"),
    ("POST", "/collections"),
    ("POST", "/collections/create"),
    ("POST", "/collections/set-item-collections"),
    ("POST", "/collections/{col_id}/add-item"),
    ("POST", "/collections/{col_id}/remove-item"),
    ("POST", "/collections/{col_id}/sync-notebooklm"),
    ("POST", "/faristol/collections"),
    ("POST", "/faristol/collections/create"),
    ("POST", "/faristol/collections/set-item-collections"),
    ("POST", "/faristol/collections/{col_id}/add-item"),
    ("POST", "/faristol/collections/{col_id}/remove-item"),
    ("POST", "/faristol/collections/{col_id}/sync-notebooklm"),
    ("POST", "/faristol/create-companion-note"),
    ("POST", "/faristol/ingest-url"),
    ("POST", "/faristol/open-external"),
    ("POST", "/faristol/open-note-external"),
    ("POST", "/faristol/save-pdf"),
    ("POST", "/faristol/upload-pdf"),
    ("POST", "/library/notify-change"),
    ("POST", "/library/open-document"),
    ("POST", "/nlm/delete"),
    ("POST", "/nlm/query"),
    ("POST", "/nlm/sources/add"),
    ("POST", "/nlm/synthesize"),
    ("POST", "/notes"),
    ("POST", "/notes/{filename}/rescue-pdf"),
    ("POST", "/pdf/highlights"),
    ("POST", "/rag/index"),
    ("POST", "/rag/query"),
    ("POST", "/trash/empty"),
    ("POST", "/trash/move-doc/{filename}"),
    ("POST", "/trash/restore/{filename}"),
    ("PUT", "/collections/{col_id}"),
    ("PUT", "/faristol/collections/{col_id}"),
    ("PUT", "/notes/{filename}"),
}


def _rutes_actuals() -> set[tuple[str, str]]:
    rutes = set()
    for path, operacions in app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        for metode in operacions:
            rutes.add((metode.upper(), path[len("/api"):]))
    return rutes


def test_la_superficie_de_rutes_no_canvia():
    assert _rutes_actuals() == RUTES_ESPERADES


def test_endpoints_de_lectura_responen_200(client):
    for path in (
        "/",
        "/api/notes",
        "/api/fonts/list",
        "/api/collections",
        "/api/trash/items",
        "/api/library/status",
        "/api/bibtex/references",
    ):
        resposta = client.get(path)
        assert resposta.status_code == 200, f"{path} -> {resposta.status_code}"
