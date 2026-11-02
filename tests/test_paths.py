"""Proves de la validació estricta de rutes (anti path traversal)."""
import pytest
from fastapi import HTTPException

from faristol import config
from faristol.api.common import _document_segur, _ruta_segura_nota


# --- _ruta_segura_nota ------------------------------------------------------

@pytest.mark.parametrize(
    "dolent",
    ["", "..", "../secret.md", "sub/dir.md", "a\\b.md", "x\x00y"],
)
def test_ruta_nota_rebutja_noms_invalids(dolent):
    with pytest.raises(HTTPException) as exc:
        _ruta_segura_nota(dolent)
    assert exc.value.status_code == 400


def test_ruta_nota_accepta_nom_simple():
    path = _ruta_segura_nota("nota.md")
    assert path.name == "nota.md"
    assert path.parent == config.NOTAS_DIR.resolve()


def test_ruta_nota_desescodifica_percentatge():
    path = _ruta_segura_nota("amb%20espai.md")
    assert path.name == "amb espai.md"


# --- _document_segur --------------------------------------------------------

def test_document_segur_troba_a_fonts():
    fitxer = config.FONTS_DIR / "article.pdf"
    fitxer.write_bytes(b"%PDF-1.4")
    assert _document_segur("article.pdf") == fitxer.resolve()


def test_document_segur_troba_a_notes():
    fitxer = config.NOTAS_DIR / "snapshot.html"
    fitxer.write_text("<html></html>")
    assert _document_segur("snapshot.html") == fitxer.resolve()


def test_document_segur_rebutja_traversal():
    with pytest.raises(HTTPException) as exc:
        _document_segur("../fora.pdf")
    assert exc.value.status_code == 400


def test_document_segur_no_trobat():
    with pytest.raises(HTTPException) as exc:
        _document_segur("inexistent.pdf")
    assert exc.value.status_code == 404


def test_document_segur_ruta_absoluta_fora_de_la_biblioteca():
    with pytest.raises(HTTPException) as exc:
        _document_segur("/etc/passwd")
    assert exc.value.status_code == 404
