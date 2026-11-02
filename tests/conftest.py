"""Configuració comuna dels tests.

Aïlla totes les dades en un directori temporal: cal fixar l'entorn ABANS
d'importar qualsevol mòdul de ``faristol``, perquè ``faristol.config`` i
``faristol.auth`` resolen la configuració en el moment d'importar-se.
"""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="faristol-tests-"))
os.environ["FARISTOL_DOCS_DIR"] = str(_TMP_ROOT / "documents")
os.environ["FARISTOL_CONFIG_DIR"] = str(_TMP_ROOT / "config")
# Força la llista d'amfitrions per defecte (127.0.0.1, localhost, testserver).
os.environ.pop("FARISTOL_ALLOWED_HOSTS", None)

# Credencials d'autenticació per a les proves.
USUARI_TEST = "test"
CONTRASENYA_TEST = "contrasenya-test"
os.environ["FARISTOL_AUTH_USER"] = USUARI_TEST
os.environ["FARISTOL_AUTH_PASSWORD"] = CONTRASENYA_TEST
os.environ["FARISTOL_SESSION_SECRET"] = "secret-de-prova"
os.environ.pop("FARISTOL_SESSION_DAYS", None)
os.environ.pop("FARISTOL_COOKIE_SECURE", None)


@pytest.fixture()
def anon_client():
    """Client de proves sense cap sessió iniciada."""
    from fastapi.testclient import TestClient
    from faristol.server import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client(anon_client):
    """Client de proves amb la sessió ja iniciada."""
    resposta = anon_client.post(
        "/login",
        data={"username": USUARI_TEST, "password": CONTRASENYA_TEST},
    )
    assert resposta.status_code in (200, 303), resposta.status_code
    return anon_client


@pytest.fixture(autouse=True)
def _entorn_net():
    """Buidja les dades temporals abans de cada test."""
    from faristol import config

    for d in (config.NOTAS_DIR, config.FONTS_DIR, config.PAPERERA_DIR, config.CONFIG_DIR):
        if d.exists():
            shutil.rmtree(d)
    config.NOTAS_DIR.mkdir(parents=True, exist_ok=True)
    config.FONTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.PAPERERA_DIR / "notes").mkdir(parents=True, exist_ok=True)
    (config.PAPERERA_DIR / "documents").mkdir(parents=True, exist_ok=True)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    yield
