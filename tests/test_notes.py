"""Cicle de vida complet d'una nota a través de l'API."""


def test_cicle_de_vida_duna_nota(client):
    # Crear
    resposta = client.post(
        "/api/notes",
        json={"titol": "Prova de Test", "tags": "test suite"},
    )
    assert resposta.status_code == 200
    filename = resposta.json()["filename"]
    assert filename.endswith(".md")

    # Llistar
    resposta = client.get("/api/notes")
    assert resposta.status_code == 200
    noms = [n["filename"] for n in resposta.json()["notes"]]
    assert filename in noms

    # Llegir
    resposta = client.get(f"/api/notes/{filename}")
    assert resposta.status_code == 200
    assert "Prova de Test" in resposta.json()["content"]

    # Actualitzar
    resposta = client.put(f"/api/notes/{filename}", json={"contingut": "# Nou\n\nHola"})
    assert resposta.status_code == 200
    resposta = client.get(f"/api/notes/{filename}")
    assert resposta.json()["content"] == "# Nou\n\nHola"

    # Esborrar (a la paperera)
    resposta = client.delete(f"/api/notes/{filename}")
    assert resposta.status_code == 200
    assert client.get(f"/api/notes/{filename}").status_code == 404


def test_crear_nota_sense_titol_falla(client):
    resposta = client.post("/api/notes", json={"titol": "   "})
    assert resposta.status_code == 400


def test_llegir_nota_inexistent_dona_404(client):
    assert client.get("/api/notes/no-existeix.md").status_code == 404
