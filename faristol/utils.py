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

def is_valid_pdf_content(pdf_path: Path, title: str = "", abstract: str = "", doi: str = "") -> Tuple[bool, str]:
    """
    Comprova si un fitxer PDF té contingut acadèmic autèntic i no és una pàgina de bloqueig antibot
    (com ara Cloudflare, "JavaScript is disabled in your browser", error 403/404 o paywall).
    Opcionalment valida la concordança de paraules clau amb el títol, abstract o DOI.
    """
    if not pdf_path.is_file():
        return False, "El fitxer no existeix."
    
    size = pdf_path.stat().st_size
    if size < 1000:
        return False, f"El fitxer és massa petit ({size} bytes)."
    
    # Comprovar capçalera PDF
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(10)
        if not header.startswith(b"%PDF"):
            return False, "La capçalera del fitxer no correspon a un document PDF vàlid."
    except Exception as e:
        return False, f"Error llegint el fitxer: {e}"

    # Extreure text de les 2 primeres pàgines amb pdftotext
    try:
        res = subprocess.run(["pdftotext", "-l", "2", str(pdf_path), "-"], capture_output=True, text=True, timeout=3)
        raw_text = res.stdout if res.returncode == 0 else ""
    except Exception:
        raw_text = ""

    text_lower = raw_text.lower()

    # 1. Detecció de marcadors de bloqueig / antibot / paywall
    block_markers = [
        "javascript is disabled in your browser",
        "please enable javascript to proceed",
        "a required part of this site couldn't load",
        "attention required! | cloudflare",
        "just a moment...",
        "cf-browser-verification",
        "access denied",
        "403 forbidden",
        "subscription required",
        "purchase this article",
        "institutional login",
        "an error occurred while processing your request",
    ]
    for marker in block_markers:
        if marker in text_lower:
            return False, f"Detectat bloqueig antibot o d'accés: '{marker}'"

    # 2. Si el text és extremadament curt (< 120 caràcters) i la mida < 35KB
    if len(raw_text.strip()) < 120 and size < 35000:
        return False, "El document conté massa poc text per a ser un article complet."

    # 3. Comprovació de conflicte de DOI (si el PDF conté explícitament un altre DOI)
    if doi and raw_text:
        clean_target_doi = doi.lower().replace("https://doi.org/", "").strip()
        pdf_dois = [d.rstrip(".,;)>]").lower() for d in re.findall(r"\b10\.\d{4,9}/[^\s\"<>]+", raw_text)]
        if pdf_dois and not any(clean_target_doi == d or d in clean_target_doi for d in pdf_dois):
            return False, f"El PDF conté un DOI diferent ({pdf_dois[0]}) al sol·licitat ({clean_target_doi})."

    # 4. Validació de concordança amb l'abstract (si està disponible)
    if abstract and len(abstract.strip()) > 60 and raw_text:
        abs_words = set(re.findall(r'\b[a-zA-Z]{6,}\b', abstract.lower()))
        stopwords = {"between", "through", "during", "without", "should", "further", "however", "results", "article", "present", "studies", "research"}
        significant_abs_words = [w for w in abs_words if w not in stopwords]
        
        if len(significant_abs_words) >= 6:
            matches = sum(1 for w in significant_abs_words if w in text_lower)
            if matches == 0:
                clean_full_title = "".join(c for c in unicodedata.normalize("NFKD", title.lower()) if c.isalnum()) if title else ""
                clean_pdf_text = "".join(c for c in unicodedata.normalize("NFKD", text_lower) if c.isalnum())
                if clean_full_title and len(clean_full_title) >= 15 and clean_full_title[:25] not in clean_pdf_text:
                    return False, "El contingut del PDF no coincideix ni amb el títol ni amb les paraules clau de l'abstract."

    return True, "PDF vàlid"

def check_and_import_from_downloads(target_path: Path, title: str = "", abstract: str = "", doi: str = "", pmid: str = "") -> bool:
    """
    Comprova si l'usuari ha descarregat el PDF manualment a ~/Downloads (p. ex. quan l'editorial
    utilitza Cloudflare o protecció antibot com MDPI, ScienceDirect, etc.).
    """
    downloads_dir = Path.home() / "Downloads"
    if not downloads_dir.is_dir():
        return False

    import time
    import hashlib
    now = time.time()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fonts_dir = target_path.parent

    def _strip_accents(text: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))

    doi_clean = re.sub(r"[^a-zA-Z0-9]", "", doi.split("/")[-1] if "/" in doi else doi).lower()
    clean_target_doi = doi.lower().replace("https://doi.org/", "").strip() if doi else ""
    clean_full_title = re.sub(r"[^a-z0-9]", "", _strip_accents(title).lower()) if title else ""
    # Només dividir per dos punts (:), mai per guió (-) per evitar tallar compostos com AI-driven o multi-level
    main_title = title.split(":")[0].strip() if ":" in title else title.strip()
    clean_main_title = re.sub(r"[^a-z0-9]", "", _strip_accents(main_title).lower()) if main_title else ""

    # Obtenir els hashes dels PDFs ja existents a la biblioteca
    existing_hashes = set()
    for ext_pdf in fonts_dir.glob("*.pdf"):
        if ext_pdf.name != target_path.name:
            try:
                existing_hashes.add(hashlib.md5(ext_pdf.read_bytes()).hexdigest())
            except Exception:
                pass

    for p in downloads_dir.glob("*.pdf"):
        try:
            mtime = p.stat().st_mtime
            if now - mtime > 28800:  # 8 hores
                continue

            # Si el PDF de Downloads ja pertany a un altre document de la biblioteca, descartar-lo!
            p_bytes = p.read_bytes()
            if hashlib.md5(p_bytes).hexdigest() in existing_hashes:
                continue

            # Validar que el PDF de Downloads tinga contingut acadèmic real i no siga un bloqueig antibot
            is_valid, _ = is_valid_pdf_content(p, title=title, abstract=abstract, doi=doi)
            if not is_valid:
                continue

            fname_clean = re.sub(r"[^a-zA-Z0-9]", "", _strip_accents(p.name).lower())

            # 1. Coincidència de nom amb DOI o PMID
            if (pmid and str(pmid) in fname_clean) or (doi_clean and len(doi_clean) > 4 and doi_clean in fname_clean):
                shutil.copy2(p, target_path)
                return True

            # 2. Coincidència de nom de fitxer amb el títol (ex: Dialnet-CreativityAndArtificialIntelligence...)
            if len(clean_main_title) >= 15 and clean_main_title[:20] in fname_clean:
                shutil.copy2(p, target_path)
                return True

            # 3. Coincidència estricta de frase al contingut de la primera pàgina
            res = subprocess.run(["pdftotext", "-l", "1", str(p), "-"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                raw_page1 = res.stdout
                clean_pdf = re.sub(r"[^a-z0-9]", "", _strip_accents(raw_page1).lower())

                # Comprovació de conflicte de DOI: si el PDF conté un DOI diferent al demanat, descartar
                if clean_target_doi:
                    pdf_dois = [d.rstrip(".,;)>]").lower() for d in re.findall(r"\b10\.\d{4,9}/[^\s\"<>]+", raw_page1)]
                    if pdf_dois and not any(clean_target_doi == d or d in clean_target_doi for d in pdf_dois):
                        continue

                if (clean_full_title and len(clean_full_title) >= 15 and clean_full_title in clean_pdf):
                    shutil.copy2(p, target_path)
                    return True

                if (clean_main_title and len(clean_main_title) >= 20 and clean_main_title in clean_pdf):
                    shutil.copy2(p, target_path)
                    return True
        except Exception:
            continue

    return False

def obtenir_pdfs_associats_nota(nota_filename: str) -> List[str]:
    """Retorna la llista de noms de fitxers PDF existents associats a una nota Markdown."""
    from .config import FONTS_DIR, NOTAS_DIR
    
    n_path = NOTAS_DIR / nota_filename
    res = []
    rutes_vistes = set()
    
    # 1. Comprovació canònica directa (@citekey.md -> citekey.pdf)
    stem = Path(nota_filename).stem
    if stem.startswith("@"):
        stem = stem[1:]
    cand_pdf = FONTS_DIR / f"{stem}.pdf"
    if cand_pdf.is_file():
        res.append(cand_pdf.name)
        rutes_vistes.add(cand_pdf.name)
        
    # 2. Anàlisi del contingut de la nota si existeix
    if n_path.is_file():
        try:
            content = n_path.read_text(encoding="utf-8", errors="ignore")
            trobats = detectar_tots_els_pdfs(n_path, content)
            for _, p, _ in trobats:
                if p.is_file() and p.name not in rutes_vistes:
                    res.append(p.name)
                    rutes_vistes.add(p.name)
        except Exception:
            pass
            
    return res

def obtenir_notes_associades_pdf(pdf_filename: str) -> List[str]:
    """Retorna la llista de noms de notes Markdown existents associades a un PDF."""
    from .config import NOTAS_DIR
    stem = Path(pdf_filename).stem
    res = []
    for cand in [f"@{stem}.md", f"{stem}.md"]:
        if (NOTAS_DIR / cand).is_file():
            res.append(cand)
    return res
