import re
import shutil
import subprocess
import urllib.parse
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional

def enviar_a_la_papelera(ruta: Path) -> bool:
    """Mou un fitxer a la paperera de KDE Plasma / Freedesktop o utilitza directori de paperera local."""
    if not ruta.exists():
        return False

    for cmd in [
        ["kioclient", "move", str(ruta.resolve()), "trash:/"],
        ["kioclient5", "move", str(ruta.resolve()), "trash:/"],
        ["gio", "trash", str(ruta.resolve())],
        ["trash-put", str(ruta.resolve())]
    ]:
        try:
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0:
                return True
        except FileNotFoundError:
            continue

    try:
        trash_dir = Path.home() / ".local" / "share" / "Trash"
        files_dir = trash_dir / "files"
        info_dir = trash_dir / "info"
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)

        dest_file = files_dir / ruta.name
        contador = 1
        while dest_file.exists():
            dest_file = files_dir / f"{ruta.stem}_{contador}{ruta.suffix}"
            contador += 1

        info_file = info_dir / f"{dest_file.name}.trashinfo"
        now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        with open(info_file, "w", encoding="utf-8") as f:
            f.write(f"[Trash Info]\nPath={ruta.resolve()}\nDeletionDate={now_str}\n")

        shutil.move(str(ruta.resolve()), str(dest_file))
        return True
    except Exception as e:
        print(f"Error movent a la paperera: {e}")
        return False

def netejar_text_slug(text: str) -> str:
    """Converteix qualsevol text en un slug net per a noms de fitxer."""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)

def detectar_tots_els_pdfs(fichero_nota: Path, text: str) -> List[Tuple[int, Path, str]]:
    """
    Detecta tots els PDFs extraient el text descriptiu de l'enllaç si existeix.
    Retorna: [(posicio_inici, ruta_absoluta, nom_o_titol_descriptiu), ...]
    """
    patrons = [
        # 1. Markdown amb etiqueta descriptiva: [Títol descriptiu](file:///ruta.pdf)
        (r'\[([^\]\n\r]+)\]\(([^)\n\r]+?[.]pdf)\)', True),
        # 2. Org-mode amb descripció: [[ruta.pdf][Títol descriptiu]]
        (r'\[\[([^\]\n\r]+?[.]pdf)\]\[(.*?)\]\]', False),
        # 3. Org-mode simple: [[ruta.pdf]]
        (r'\[\[([^\]\n\r]+?[.]pdf)\]\]', None),
        # 4. Propietat Org-noter
        (r':NOTER_DOCUMENT:\s*["\']?([^"\n\r]+?[.]pdf)["\']?', None),
        # 5. Ruta directa solta
        (r'(?:file://)?(/[^\n\r"\'<>]+?[.]pdf)', None),
    ]
    
    trobats = []
    rutes_vistes = set()

    # 0. Resolució 1:1 canònica per a notes bibliogràfiques (@citekey.md <-> citekey.pdf)
    if fichero_nota:
        stem = ""
        if fichero_nota.name.startswith("@"):
            stem = fichero_nota.stem[1:]
        else:
            m_cite = re.search(r'^citekey:\s*["\']?@?([^"\'\n\r]+)["\']?', text, re.M)
            if m_cite:
                stem = m_cite.group(1).strip()
        
        if stem:
            from .config import FONTS_DIR
            pdf_canonic = FONTS_DIR / f"{stem}.pdf"
            if pdf_canonic.is_file() and str(pdf_canonic.resolve()) not in rutes_vistes:
                rutes_vistes.add(str(pdf_canonic.resolve()))
                trobats.append((0, pdf_canonic.resolve(), pdf_canonic.name))

    for pat, tipus in patrons:
        for m in re.finditer(pat, text, re.IGNORECASE):
            etiqueta = ""
            if tipus is True:
                etiqueta = m.group(1).strip()
                candidata = m.group(2).strip().strip("'\"<>")
            elif tipus is False:
                candidata = m.group(1).strip().strip("'\"<>")
                etiqueta = m.group(2).strip()
            else:
                candidata = m.group(1).strip().strip("'\"<>")

            if candidata.startswith("file://"):
                candidata = candidata[7:]
            elif candidata.startswith("file:"):
                candidata = candidata[5:]
            if candidata.startswith("//"):
                candidata = "/" + candidata.lstrip("/")
            
            candidata = urllib.parse.unquote(candidata)
            
            p = Path(candidata)
            if not p.is_absolute() and fichero_nota:
                p = (fichero_nota.parent / p).resolve()
            
            if p.exists() and p.is_file() and p.suffix.lower() == ".pdf":
                if str(p) not in rutes_vistes:
                    rutes_vistes.add(str(p))
                    nom_mostrar = etiqueta if etiqueta else p.name
                    trobats.append((m.start(), p, nom_mostrar))

    trobats.sort(key=lambda x: x[0])
    return trobats

def extraure_pagines_nota(contingut: str) -> List[int]:
    """Extreu totes les referències a pàgines d'un fitxer de notes."""
    pagines = set()
    for m in re.finditer(r':NOTER_PAGE:\s*(\d+)', contingut, re.IGNORECASE):
        pagines.add(int(m.group(1)))
    for m in re.finditer(r'(?:pàgina|p\.|pag\.|pàg\.)\s*(\d+)', contingut, re.IGNORECASE):
        pagines.add(int(m.group(1)))
    for m in re.finditer(r'\[@[\w\-]+,\s*p\.\s*(\d+)\]', contingut, re.IGNORECASE):
        pagines.add(int(m.group(1)))
    return sorted(list(pagines))

def extraure_identificador_fitxer(filename: str) -> str:
    """Extreu l'identificador canònic d'un fitxer de notes o PDF."""
    stem = Path(filename).stem
    if stem.startswith("@"):
        stem = stem[1:]
    return stem

def extraure_tags_nota(contingut_o_nom: str, contingut: Optional[str] = None) -> List[str]:
    """Extreu etiquetes de la capçalera YAML (tags: [...]) o del text (#tag)."""
    text_a_analitzar = contingut if contingut is not None else contingut_o_nom
    tags = set()
    # YAML tags
    m_yaml = re.search(r'^---\s*\n([\s\S]*?)\n---', text_a_analitzar)
    if m_yaml:
        yaml_text = m_yaml.group(1)
        m_tags = re.search(r'tags:\s*\[(.*?)\]', yaml_text)
        if m_tags:
            for t in m_tags.group(1).split(','):
                t = t.strip().strip("'\"#")
                if t:
                    tags.add(t)
        m_tags_list = re.findall(r'tags:\s*\n((?:\s*-\s*.+\n?)+)', yaml_text)
        for block in m_tags_list:
            for line in block.splitlines():
                t = re.sub(r'^\s*-\s*', '', line).strip().strip("'\"#")
                if t:
                    tags.add(t)
    
    # Inline #tags
    if contingut:
        for m in re.finditer(r'(?<!\w)#([a-zA-Z0-9_\-]+)', contingut):
            t = m.group(1)
            if not t.isdigit() and len(t) > 1:
                tags.add(t)
            
    return sorted(list(tags))

def check_and_import_from_downloads(target_path: Path, title: str = "", doi: str = "", pmid: str = "") -> bool:
    """
    Comprova si l'usuari ha descarregat el PDF manualment a ~/Downloads (p. ex. quan l'editorial
    utilitza Cloudflare o protecció antibot com MDPI, ScienceDirect, etc.).
    """
    downloads_dir = Path.home() / "Downloads"
    if not downloads_dir.is_dir():
        return False

    import time
    now = time.time()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    def _strip_accents(text: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))

    doi_clean = re.sub(r"[^a-zA-Z0-9]", "", doi.split("/")[-1] if "/" in doi else doi).lower()
    clean_full_title = re.sub(r"[^a-z0-9]", "", _strip_accents(title).lower()) if title else ""
    main_title = re.split(r"[:\-\?]", title)[0].strip() if title else ""
    clean_main_title = re.sub(r"[^a-z0-9]", "", _strip_accents(main_title).lower()) if main_title else ""

    for p in downloads_dir.glob("*.pdf"):
        try:
            mtime = p.stat().st_mtime
            if now - mtime > 28800:  # 8 hores
                continue

            fname_clean = re.sub(r"[^a-zA-Z0-9]", "", _strip_accents(p.name).lower())

            # 1. Coincidència de nom amb DOI o PMID
            if (pmid and str(pmid) in fname_clean) or (doi_clean and len(doi_clean) > 4 and doi_clean in fname_clean):
                shutil.copy2(p, target_path)
                return True

            # 2. Coincidència de nom de fitxer amb el títol (ex: Dialnet-CreativityAndArtificialIntelligence...)
            if len(clean_main_title) >= 12 and clean_main_title[:20] in fname_clean:
                shutil.copy2(p, target_path)
                return True

            # 3. Coincidència estricta de frase al contingut de la primera pàgina
            if len(clean_main_title) >= 12:
                res = subprocess.run(["pdftotext", "-l", "1", str(p), "-"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0:
                    clean_pdf = re.sub(r"[^a-z0-9]", "", _strip_accents(res.stdout).lower())
                    if (clean_full_title and len(clean_full_title) >= 15 and clean_full_title in clean_pdf) or \
                       (clean_main_title in clean_pdf):
                        shutil.copy2(p, target_path)
                        return True
        except Exception:
            continue

    return False
