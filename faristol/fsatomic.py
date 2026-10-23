"""
Utilitats d'escriptura atòmica i bloqueig concurrent per a Faristol.
Protegeix fitxers crítics (PDFs, notes, biblioteca.bib, col·leccions i subratllats)
contra corrupció per talls de corrent, processos concorrents i escriptures parcials.
"""
import os
import fcntl
import json
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Union, Optional

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_META_LOCK = threading.Lock()

def _get_thread_lock(key: str) -> threading.Lock:
    with _META_LOCK:
        if key not in _THREAD_LOCKS:
            _THREAD_LOCKS[key] = threading.Lock()
        return _THREAD_LOCKS[key]

@contextmanager
def bloqueig(ruta: Union[str, Path]):
    """
    Bloqueig dual (fils d'execució + processos externs mitjançant flock).
    Permet serialitzar l'accés a fitxers compartits entre Faristol, zoterless i add-paper.
    """
    path_obj = Path(ruta).resolve()
    key = str(path_obj)
    lock_file = path_obj.parent / f".{path_obj.name}.lock"
    
    t_lock = _get_thread_lock(key)
    with t_lock:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Obrir en mode append/read per al flock
        with open(lock_file, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass

def escriure_atomic(desti: Union[str, Path], dades: Union[bytes, str], backup: bool = False) -> None:
    """
    Escriu dades en un fitxer de forma estrictament atòmica:
    1. Escriu a un fitxer temporal en el mateix directori / sistema de fitxers.
    2. Fa flush() i fsync() del descriptor de fitxer.
    3. Copia de seguretat (.bak) si s'ha sol·licitat i el fitxer ja existia.
    4. Reanomena atòmicament (os.replace) sobre el destí.
    5. Fa fsync() del directori contenidor.
    """
    dest_path = Path(desti).resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    raw_bytes = dades.encode("utf-8") if isinstance(dades, str) else dades

    if backup and dest_path.is_file():
        bak_path = dest_path.with_suffix(dest_path.suffix + ".bak")
        try:
            shutil.copy2(dest_path, bak_path)
        except Exception:
            pass

    # Crear temporal al mateix directori
    fd, tmp_path_str = tempfile.mkstemp(dir=str(dest_path.parent), prefix=f".{dest_path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_path_str)

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
            f.flush()
            os.fsync(f.fileno())

        if dest_path.exists():
            try:
                os.chmod(tmp_path, dest_path.stat().st_mode & 0o777)
            except Exception:
                pass

        os.replace(tmp_path, dest_path)

        # fsync del directori pare per garantir persistència de l'entrada a la taula d'inodos
        try:
            dir_fd = os.open(str(dest_path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise

def llegir_json_segur(ruta: Union[str, Path], defecte: Any) -> Any:
    """
    Llegeix un fitxer JSON de forma segura. Si el fitxer no existeix, retorna el defecte.
    Si el JSON està corromput, fa una còpia a .corrupte i llança una excepció per evitar
    que es sobreescrigui amb una llista/diccionari buit per error.
    """
    path_obj = Path(ruta).resolve()
    if not path_obj.exists() or not path_obj.is_file():
        return defecte

    try:
        with open(path_obj, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as jde:
        corrupt_backup = path_obj.with_suffix(path_obj.suffix + ".corrupte")
        try:
            shutil.copy2(path_obj, corrupt_backup)
        except Exception:
            pass
        raise RuntimeError(
            f"El fitxer {path_obj.name} està danyat o incomplet. S'ha preservat a {corrupt_backup.name}."
        ) from jde
