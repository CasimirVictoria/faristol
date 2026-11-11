"""Proves per al motor RAG Local, cerca full-text, neteja de paperera i exportació Quarto/Pandoc."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_cerca_fulltext_i_enriquiment_bibtex(client):
    from faristol import config

    config.BIB_FILE_PATH.write_text(
        '@article{garciaTest2025,\n'
        '  title = {Creativitat Computacional},\n'
        '  author = {García, María and López, Joan},\n'
        '  year = {2025},\n'
        '  journal = {Revista STEAM}\n'
        '}\n',
        encoding="utf-8",
    )
    (config.NOTAS_DIR / "@garciaTest2025.md").write_text(
        '---\ntitle: "Creativitat Computacional"\ncitekey: "@garciaTest2025"\ntags: ["steam"]\n---\n\n'
        '# Idees\nAquesta nota tracta sobre retroalimentació formativa amb làpices al centre.\n',
        encoding="utf-8",
    )

    # Cerca per text dins del cos de la nota
    r_body = client.get("/api/notes", params={"q": "làpices"})
    assert r_body.status_code == 200
    notes = r_body.json()["notes"]
    assert len(notes) == 1
    assert notes[0]["author"] == "García, María and López, Joan"
    assert notes[0]["year"] == "2025"

    # Cerca per nom d'autor procedent de biblioteca.bib
    r_author = client.get("/api/notes", params={"q": "García"})
    assert r_author.status_code == 200
    assert len(r_author.json()["notes"]) == 1


def test_rag_notes_index_consulta_i_neteja_paperera(client, tmp_path, monkeypatch):
    from faristol import config
    from faristol.api import rag

    vec_dir = tmp_path / "vec_test"
    vec_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rag, "_CANONICAL_VEC_DIR", vec_dir)
    monkeypatch.setattr(rag, "_CANONICAL_VEC_DB", vec_dir / "vectors.db")
    monkeypatch.setattr(rag, "_RAG_INITIALIZED", False)

    nota_path = config.NOTAS_DIR / "@prova2025.md"
    nota_path.write_text(
        '---\ntitle: "Modelització física"\ncitekey: "@prova2025"\ntags: ["fisica"]\n---\n\n'
        '## Conclusions\nEl ritme de canvi explica la velocitat sense derivades formals.\n',
        encoding="utf-8",
    )
    html_path = config.FONTS_DIR / "prova2025.html"
    html_path.write_text("<html><head><title>Prova HTML</title></head><body><p>Contingut HTML de prova</p></body></html>", encoding="utf-8")

    class FakeEmbedder:
        def embed(self, text: str):
            return [1.0, 0.0, 0.0, 0.0]

        def embed_batch(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    _, _, _, _, _, _ = rag._get_rag_components()
    monkeypatch.setattr(rag, "_EMBEDDER", FakeEmbedder())
    rag._RETRIEVER.embedder = FakeEmbedder()

    res_idx = rag._indexar_fitxers_sync(["@prova2025.md", "prova2025.html"], force=True)
    assert res_idx["indexed_files"] == 2
    assert res_idx["chunks_added"] >= 2

    # Comprovar /api/rag/sources inclou nota i html
    r_src = client.get("/api/rag/sources")
    assert r_src.status_code == 200
    src_data = r_src.json()
    assert src_data["indexed_count"] == 2
    types = {s["filename"]: s["type"] for s in src_data["sources"]}
    assert types["@prova2025.md"] == "note"
    assert types["prova2025.html"] == "html"

    # Comprovar consulta /api/rag/query
    r_q = client.post("/api/rag/query", json={"query": "ritme de canvi velocitat", "mode": "evidences"})
    assert r_q.status_code == 200
    q_data = r_q.json()
    assert len(q_data["references"]) >= 1
    assert any(ref["citekey"] == "prova2025" for ref in q_data["references"])

    # Comprovar /api/rag/stream en mode evidències
    r_st = client.post("/api/rag/stream", json={"query": "ritme de canvi", "mode": "evidences"})
    assert r_st.status_code == 200
    assert "event: metadata" in r_st.text
    assert "event: done" in r_st.text

    # En enviar a la paperera, els chunks han de netejar-se automàticament de vectors.db
    r_del = client.delete("/api/notes/@prova2025.md")
    assert r_del.status_code == 200
    r_status = client.get("/api/rag/status")
    assert r_status.json()["vector_store"]["total_chunks"] == 0


def test_exportacio_quarto_html_i_pdf(client):
    from faristol import config

    config.BIB_FILE_PATH.write_text(
        '@article{casimir2026,\n'
        '  title = {Laboratoris de Caixa Blanca},\n'
        '  author = {Victoria, Casimiro},\n'
        '  year = {2026},\n'
        '  journal = {STEAM Valencia}\n'
        '}\n',
        encoding="utf-8",
    )

    md_sample = (
        "# Síntesi de Prova\n\n"
        "Aquesta afirmació cita l'article [@casimir2026, p. 3] i inclou matemàtiques $v = v_0 + a t$.\n"
    )

    # 1. Quarto .qmd
    r_qmd = client.post("/api/export/document", json={
        "filename": "prova_quarto",
        "title": "Síntesi de Prova",
        "content": md_sample,
        "format": "qmd",
    })
    assert r_qmd.status_code == 200
    assert "pdf-engine: xelatex" in r_qmd.text
    assert "bibliography:" in r_qmd.text

    # 2. HTML compilat amb Pandoc + citeproc
    r_html = client.post("/api/export/document", json={
        "filename": "prova_quarto",
        "title": "Síntesi de Prova",
        "content": md_sample,
        "format": "html",
    })
    assert r_html.status_code == 200
    assert "Victoria" in r_html.text

    # 3. PDF compilat amb Pandoc + XeLaTeX
    r_pdf = client.post("/api/export/document", json={
        "filename": "prova_quarto",
        "title": "Síntesi de Prova",
        "content": md_sample,
        "format": "pdf",
    })
    assert r_pdf.status_code == 200
    assert r_pdf.content.startswith(b"%PDF")
