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
    for m in re.finditer(r'(?<!\w)#([a-zA-Z0-9_\-]+)', contingut):
        t = m.group(1)
        if not t.isdigit() and len(t) > 1:
            tags.add(t)
            
    return sorted(list(tags))
