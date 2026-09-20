import os
from pathlib import Path

# Directori base de documents
USER_HOME = Path.home()
DOCS_DEFAULT = USER_HOME / "Documents" if (USER_HOME / "Documents").exists() else USER_HOME / "Documentos"
DOCUMENTS_DIR = Path(os.environ.get("PLEC_DOCS_DIR", DOCS_DEFAULT))

# Rutes canòniques del sistema (Plec / Zoterless)
NOTAS_DIR = Path(os.environ.get("PLEC_NOTES_DIR", DOCUMENTS_DIR / "notes"))
FONTS_DIR = Path(os.environ.get("PLEC_PDF_DIR", DOCUMENTS_DIR / "Biblioteca/pdfs"))
BIB_FILE_PATH = Path(os.environ.get("PLEC_BIB_FILE", DOCUMENTS_DIR / "Biblioteca/biblioteca.bib"))

# Configuració de l'aplicació
CONFIG_DIR = Path(os.environ.get("PLEC_CONFIG_DIR", USER_HOME / ".config/plec"))
COLLECTIONS_FILE = CONFIG_DIR / "collections.json"
HIGHLIGHTS_FILE = CONFIG_DIR / "highlights.json"

# Assegurar directoris bàsics
NOTAS_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)
BIB_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
