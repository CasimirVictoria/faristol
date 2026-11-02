import os
from pathlib import Path

# Directori base de documents
USER_HOME = Path.home()
DOCS_DEFAULT = USER_HOME / "Documents" if (USER_HOME / "Documents").exists() else USER_HOME / "Documentos"
DOCUMENTS_DIR = Path(os.environ.get("FARISTOL_DOCS_DIR", DOCS_DEFAULT))

# Rutes canòniques del sistema (Faristol / zoterless)
NOTAS_DIR = Path(os.environ.get("FARISTOL_NOTES_DIR", DOCUMENTS_DIR / "notes"))
FONTS_DIR = Path(os.environ.get("FARISTOL_PDF_DIR", DOCUMENTS_DIR / "Biblioteca/pdfs"))
BIB_FILE_PATH = Path(os.environ.get("FARISTOL_BIB_FILE", DOCUMENTS_DIR / "Biblioteca/biblioteca.bib"))
PAPERERA_DIR = Path(os.environ.get("FARISTOL_TRASH_DIR", DOCUMENTS_DIR / "Biblioteca/Paperera"))

# Integracions opcionals amb servidors MCP locals (projectes germans).
# Configurables per entorn; si el directori no existeix, les funcions que els
# empren degraden amb gràcia (retornen resultats buits) en compte de fallar.
MCP_OPEN_DATA_DIR = Path(os.environ.get(
    "FARISTOL_MCP_OPEN_DATA_DIR", USER_HOME / "Projects/mcp-server-open-data-spain"))
MCP_REAL_ESTATE_DIR = Path(os.environ.get(
    "FARISTOL_MCP_REAL_ESTATE_DIR", USER_HOME / "Projects/mcp-server-real-estate-spain"))
MCP_ACADEMIC_DIR = Path(os.environ.get(
    "FARISTOL_MCP_ACADEMIC_DIR", USER_HOME / "Projects/mcp-server-academic-spain"))

# Configuració de l'aplicació
DEFAULT_CONFIG_DIR = USER_HOME / ".config/faristol"
CONFIG_DIR = Path(os.environ.get("FARISTOL_CONFIG_DIR", DEFAULT_CONFIG_DIR))
COLLECTIONS_FILE = CONFIG_DIR / "collections.json"
HIGHLIGHTS_FILE = CONFIG_DIR / "highlights.json"

# Assegurar directoris bàsics
NOTAS_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)
PAPERERA_DIR.mkdir(parents=True, exist_ok=True)
(PAPERERA_DIR / "notes").mkdir(parents=True, exist_ok=True)
(PAPERERA_DIR / "documents").mkdir(parents=True, exist_ok=True)
BIB_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

