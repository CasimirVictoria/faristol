"""Cicle de vida dels subratllats persistents associats a un PDF."""
from faristol import config


def _crear_pdf(nom: str):
    fitxer = config.FONTS_DIR / nom
    fitxer.write_bytes(b"%PDF-1.4\n% dummy")
    return nom


def test_cicle_de_vida_dun_subratllat(client):
    nom = _crear_pdf("article.pdf")

    # Inicialment no n'hi ha cap
    resposta = client.get("/api/pdf/highlights", params={"path": nom})
    assert resposta.status_code == 200
    assert resposta.json()["highlights"] == []

    # Desar-ne un
    resposta = client.post(
        "/api/pdf/highlights",
        json={
            "pdf_path": nom,
            "page": 3,
            "text": "Fragment subratllat",
            "rects": [[10, 10, 100, 20]],
        },
    )
    assert resposta.status_code == 200
    highlight_id = resposta.json()["highlight"]["id"]

    # Recuperar-lo
    resposta = client.get("/api/pdf/highlights", params={"path": nom})
    assert resposta.status_code == 200
    subratllats = resposta.json()["highlights"]
    assert len(subratllats) == 1
    assert subratllats[0]["text"] == "Fragment subratllat"
    assert subratllats[0]["page"] == 3

    # Esborrar-lo
    resposta = client.delete(
        f"/api/pdf/highlights/{highlight_id}", params={"path": nom}
    )
    assert resposta.status_code == 200
    resposta = client.get("/api/pdf/highlights", params={"path": nom})
    assert resposta.json()["highlights"] == []


def test_subratllats_de_pdf_inexistent_dona_404(client):
    resposta = client.get("/api/pdf/highlights", params={"path": "no-existeix.pdf"})
    assert resposta.status_code == 404
