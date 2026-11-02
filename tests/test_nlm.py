"""Proves de la resolució de l'executable de NotebookLM (sense dependre'n)."""
import asyncio

from faristol.api import nlm


def test_nlm_bin_usa_la_variable_dentorn(monkeypatch, tmp_path):
    override = tmp_path / "nlm"
    override.write_text("")
    monkeypatch.setenv("FARISTOL_NLM_BIN", str(override))
    assert nlm._nlm_bin() == str(override)


def test_nlm_bin_des_del_path(monkeypatch):
    monkeypatch.delenv("FARISTOL_NLM_BIN", raising=False)
    monkeypatch.setattr(nlm.shutil, "which", lambda _: "/usr/bin/nlm")
    assert nlm._nlm_bin() == "/usr/bin/nlm"


def test_nlm_bin_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("FARISTOL_NLM_BIN", raising=False)
    monkeypatch.setattr(nlm.shutil, "which", lambda _: None)
    monkeypatch.setattr(nlm.Path, "home", classmethod(lambda cls: tmp_path))
    assert nlm._nlm_bin() is None


def test_exec_nlm_cli_sense_bin_dona_error_net(monkeypatch):
    monkeypatch.setattr(nlm, "_nlm_bin", lambda: None)
    ok, missatge, data = asyncio.run(nlm._exec_nlm_cli(["list"]))
    assert ok is False
    assert data is None
    assert "nlm" in missatge
