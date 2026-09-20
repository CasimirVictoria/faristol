"""
Mòdul de gestió i cerca sobre la base de dades BibTeX mestra per a Plec.
Compatible amb Better BibTeX (Zotero / Zoterless).
"""
import re
from pathlib import Path
from typing import List, Dict, Optional

class GestorBibtex:
    _cache_referencies: Optional[List[Dict[str, str]]] = None
    _ultim_mtime: float = 0.0

    @classmethod
    def carregar_referencies(cls, ruta_bib: Path) -> List[Dict[str, str]]:
        if not ruta_bib or not ruta_bib.exists():
            return []

        try:
            mtime = ruta_bib.stat().st_mtime
        except Exception:
            return []

        if cls._cache_referencies is not None and mtime == cls._ultim_mtime:
            return cls._cache_referencies

        referencies = []
        try:
            contingut = ruta_bib.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        # Expressió regular per identificar entrades BibTeX: @tipus{clau, camps...}
        patro_entrada = re.compile(r'@(\w+)\s*\{\s*([^,]+),([\s\S]*?)\n\}\s*(?=@|\Z)', re.MULTILINE)

        for match in patro_entrada.finditer(contingut):
            tipus = match.group(1).lower()
            clau = match.group(2).strip()
            cossos = match.group(3)

            ref = {
                "key": clau,
                "type": tipus,
                "title": "",
                "author": "",
                "year": "",
                "journal": "",
                "file": "",
                "url": "",
                "abstract": ""
            }

            # Extracció de camps
            for camp_match in re.finditer(r'([a-zA-Z_-]+)\s*=\s*(?:\{([\s\S]*?)\}|"([\s\S]*?)"|(\d+))(?:\s*,|\s*$)', cossos):
                nom_camp = camp_match.group(1).lower()
                valor = camp_match.group(2) or camp_match.group(3) or camp_match.group(4) or ""
                valor = re.sub(r'\s+', ' ', valor).strip()
                valor_net = valor.replace('{', '').replace('}', '')
                ref[nom_camp] = valor_net

            # Netejar caràcters d'accents LaTeX bàsics com {\'o} -> ó
            for f in ["title", "author", "journal"]:
                if f in ref:
                    v = ref[f]
                    v = re.sub(r"\\['`^\"~]?([a-zA-Z])", r"\1", v)
                    ref[f] = v

            referencies.append(ref)

        cls._cache_referencies = referencies
        cls._ultim_mtime = mtime
        return referencies

    @classmethod
    def cercar(cls, ruta_bib: Path, query: str) -> List[Dict[str, str]]:
        refs = cls.carregar_referencies(ruta_bib)
        if not query or not query.strip():
            return refs

        termes = [t.lower().strip() for t in query.split() if t.strip()]
        resultats = []

        for ref in refs:
            text_total = f"{ref['key']} {ref['title']} {ref['author']} {ref['year']} {ref['journal']}".lower()
            if all(t in text_total for t in termes):
                resultats.append(ref)

        return resultats
