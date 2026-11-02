"""Proves de l'autenticació per sessió amb cookies HttpOnly."""

import os

from faristol import auth

USUARI = os.environ["FARISTOL_AUTH_USER"]
CONTRASENYA = os.environ["FARISTOL_AUTH_PASSWORD"]


def test_sense_cookie_lapi_retorna_401(anon_client):
    for path in ("/api/notes", "/api/collections", "/api/fonts/list", "/api/library/status"):
        assert anon_client.get(path).status_code == 401, path


def test_sense_cookie_html_redirigeix_al_login(anon_client):
    for path in ("/", "/faristol"):
        resposta = anon_client.get(path, follow_redirects=False)
        assert resposta.status_code == 303, path
        assert resposta.headers["location"] == "/login"


def test_arxius_estatics_son_publics(anon_client):
    assert anon_client.get("/static/login.html").status_code == 200


def test_login_get_mostra_formulari(anon_client):
    resposta = anon_client.get("/login")
    assert resposta.status_code == 200
    assert 'action="/login"' in resposta.text
    assert 'name="password"' in resposta.text


def test_credencials_incorrectes_no_donen_sessio(anon_client):
    resposta = anon_client.post(
        "/login",
        data={"username": USUARI, "password": "malament"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login?error=1"
    assert auth.COOKIE_NAME not in anon_client.cookies
    assert anon_client.get("/api/notes").status_code == 401


def test_login_correcte_establix_cookie_httponly_de_30_dies(anon_client):
    resposta = anon_client.post(
        "/login",
        data={"username": USUARI, "password": CONTRASENYA},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/"

    set_cookie = resposta.headers["set-cookie"].lower()
    assert auth.COOKIE_NAME in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert f"max-age={auth.MAX_AGE}" in set_cookie
    assert auth.MAX_AGE == 30 * 86400  # 30 dies
    # Per defecte NO és Secure (l'accés habitual és HTTP dins la LAN/Tailscale).
    assert "secure" not in set_cookie

    # Amb la cookie ja es pot accedir a les dades
    assert anon_client.get("/api/notes").status_code == 200


def test_logout_esborra_la_sessio(anon_client):
    anon_client.post("/login", data={"username": USUARI, "password": CONTRASENYA})
    assert anon_client.get("/api/notes").status_code == 200

    anon_client.post("/logout")
    assert anon_client.get("/api/notes").status_code == 401


def test_token_manipulat_es_rebutjat(anon_client):
    anon_client.cookies.set(auth.COOKIE_NAME, "aixo.no.es.un.token")
    assert anon_client.get("/api/notes").status_code == 401


def test_token_caducat_es_rebutjat(anon_client):
    anon_client.cookies.set(auth.COOKIE_NAME, auth.crear_token(USUARI, max_age=-10))
    assert anon_client.get("/api/notes").status_code == 401


def test_estat_de_sessio_autenticat(client):
    resposta = client.get("/api/session")
    assert resposta.status_code == 200
    assert resposta.json()["authenticated"] is True
    assert resposta.json()["user"] == USUARI
