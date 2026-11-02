"""Configuració comuna dels tests.

Aïlla totes les dades en un directori temporal: cal fixar l'entorn ABANS
d'importar qualsevol mòdul de ``faristol``, perquè ``faristol.config`` resol
les rutes en el moment d'importar-se.
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


@pytest.fixture(scope="session")
def client():
    """Client de proves de l'aplicació FastAPI completa."""
    from fastapi.testclient import TestClient
    from faristol.server import app

    with TestClient(app) as c:
        yield c


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
