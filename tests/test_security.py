"""Proves de les defenses de seguretat del servidor.

Cobreixen: capçaleres de seguretat, llista blanca d'amfitrions (anti
DNS-rebinding) i la protecció CSRF.
"""
import faristol.server as server


# --- Capçaleres de seguretat ------------------------------------------------

def test_capcaleres_de_seguretat_en_resposta_ok(client):
    resposta = client.get("/")
    assert resposta.status_code == 200
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_capcaleres_de_seguretat_en_resposta_derror(client):
    resposta = client.get("/", headers={"Host": "evil.example.com"})
    assert resposta.status_code == 400
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["X-Frame-Options"] == "SAMEORIGIN"


# --- Llista blanca d'amfitrions ---------------------------------------------

def test_amfitrions_locals_permesos(client):
    for host in ("127.0.0.1", "localhost", "testserver"):
        assert client.get("/", headers={"Host": host}).status_code == 200


def test_amfitrio_desconegut_bloquejat(client):
    assert client.get("/", headers={"Host": "attacker.com"}).status_code == 400


def test_host_permes_admet_comodins(monkeypatch):
    monkeypatch.setattr(server, "HOSTS_PERMESOS", ["exemple.com", "*.ts.net"])
    assert server._host_permès("exemple.com")
    assert server._host_permès("EXEMPLE.COM")  # insensible a majúscules
    assert server._host_permès("node.ts.net")
    assert server._host_permès("ts.net")  # coincidència amb el domini base
    assert not server._host_permès("exemple.com.evil.com")
    assert not server._host_permès("evil.com")
    assert not server._host_permès("")


# --- Protecció CSRF ---------------------------------------------------------

def test_csrf_bloqueja_peticio_cross_site(client):
    resposta = client.post(
        "/api/notes",
        headers={"Origin": "http://evil.com", "Sec-Fetch-Site": "cross-site"},
        json={"titol": "No ha de crear-se"},
    )
    assert resposta.status_code == 403


def test_csrf_bloqueja_origen_null(client):
    resposta = client.post(
        "/api/notes",
        headers={"Origin": "null"},
        json={"titol": "No ha de crear-se"},
    )
    assert resposta.status_code == 403


def test_csrf_bloqueja_origen_desconegut(client):
    resposta = client.post(
        "/api/notes",
        headers={"Origin": "http://evil.com", "Sec-Fetch-Site": "same-origin"},
        json={"titol": "No ha de crear-se"},
    )
    assert resposta.status_code == 403


def test_csrf_accepta_mateix_origen(client):
    resposta = client.post(
        "/api/notes",
        headers={"Origin": "http://testserver"},
        json={"titol": "Nota legítima"},
    )
    assert resposta.status_code == 200
