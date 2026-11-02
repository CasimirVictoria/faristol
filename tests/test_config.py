"""Proves de portabilitat: directoris d'integració configurables i degradació neta.

Faristol empra opcionalment servidors MCP locals (projectes germans). Si no hi
són, ha de degradar amb gràcia en compte de fallar.
"""
import os
import subprocess
import sys
from pathlib import Path

from faristol.api import search


def test_mcp_open_data_absent_retorna_none(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "MCP_OPEN_DATA_DIR", tmp_path / "inexistent")
    monkeypatch.setattr(search, "_MCP_OPEN_DATA_MOD", None)
    assert search._get_mcp_open_data() is None


def test_mcp_real_estate_absent_retorna_none(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "MCP_REAL_ESTATE_DIR", tmp_path / "inexistent")
    monkeypatch.setattr(search, "_MCP_REAL_ESTATE_MOD", None)
    assert search._get_mcp_real_estate() is None


def test_dirs_mcp_configurables_per_entorn():
    """Les rutes dels projectes germans es poden sobreescriure per entorn."""
    repo = Path(__file__).resolve().parents[1]
    entorn = dict(os.environ, FARISTOL_MCP_OPEN_DATA_DIR="/tmp/mcp-custom")
    resultat = subprocess.run(
        [sys.executable, "-c", "from faristol import config; print(config.MCP_OPEN_DATA_DIR)"],
        cwd=repo,
        env=entorn,
        capture_output=True,
        text=True,
        check=True,
    )
    assert resultat.stdout.strip() == "/tmp/mcp-custom"
