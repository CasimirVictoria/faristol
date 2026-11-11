"""
Exportació directa des de Faristol cap a Quarto Markdown (.qmd) i formats compilats
amb Pandoc + Citeproc + XeLaTeX (PDF, HTML autocontingut, DOCX) usant biblioteca.bib.
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from faristol.api.models import ExportDocumentRequest
from faristol.config import BIB_FILE_PATH

router = APIRouter()


def _preparar_markdown_exportacio(title: str, content: str, format_out: str) -> str:
    """Normalitza el frontmatter YAML i afegeix referència a biblioteca.bib per a Quarto / Pandoc."""
    raw = (content or "").strip()
    bib_path_str = str(BIB_FILE_PATH.resolve()) if BIB_FILE_PATH.exists() else ""

    # Convertir enllaços Denote [[...]] a text net Markdown per evitar artefactes en XeLaTeX/Pandoc
    def _clean_wikilink(m):
        inner = m.group(1)
        desc = m.group(2)
        return desc or inner

    cleaned_body = re.sub(r"\[\[([^\]]+?)\](?:\[([^\]]*?)\])?\]", _clean_wikilink, raw)

    has_yaml = bool(re.match(r"^---\s*\n[\s\S]*?\n---", cleaned_body))
    if has_yaml:
        if format_out == "qmd" and bib_path_str and "bibliography:" not in cleaned_body[:1500]:
            cleaned_body = re.sub(
                r"^(---\s*\n[\s\S]*?)\n---",
                lambda m: f"{m.group(1)}\nbibliography: {json.dumps(bib_path_str)}\nformat:\n  pdf:\n    pdf-engine: xelatex\n  html: default\n---",
                cleaned_body,
                count=1,
            )
        return cleaned_body

    yaml_lines = [
        "---",
        f"title: {json.dumps(title or 'Document Faristol', ensure_ascii=False)}",
    ]
    if bib_path_str:
        yaml_lines.append(f"bibliography: {json.dumps(bib_path_str)}")
    if format_out == "qmd":
        yaml_lines.extend([
            "format:",
            "  pdf:",
            "    pdf-engine: xelatex",
            "    toc: false",
            "  html:",
            "    embed-resources: true",
        ])
    yaml_lines.append("---")
    return "\n".join(yaml_lines) + "\n\n" + cleaned_body + "\n"


def _slug_fitxer(name: str) -> str:
    base = Path(name or "document").stem
    slug = re.sub(r"[^a-zA-Z0-9_\-@]+", "_", base).strip("_")
    return slug or "document"


@router.post("/export/document")
async def exportar_document(req: ExportDocumentRequest):
    """Exporta la nota o text actual a .qmd (Quarto), .pdf (XeLaTeX), .html o .docx amb cites Pandoc."""
    fmt = (req.format or "pdf").lower().strip()
    if fmt not in ("pdf", "html", "docx", "qmd"):
        raise HTTPException(status_code=400, detail="Format no suportat. Usa: pdf, html, docx o qmd")

    stem = _slug_fitxer(req.filename or req.title)
    md_text = _preparar_markdown_exportacio(req.title, req.content, fmt)

    if fmt == "qmd":
        return Response(
            content=md_text.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}.qmd"'},
        )

    pandoc_bin = shutil.which("pandoc")
    if not pandoc_bin:
        raise HTTPException(status_code=500, detail="Cal tindre 'pandoc' instal·lat al sistema per exportar")

    def _compilar_sync() -> tuple[bytes, str, str]:
        with tempfile.TemporaryDirectory(prefix="faristol-export-") as tmpdir:
            tmp_path = Path(tmpdir)
            src_md = tmp_path / f"{stem}.md"
            out_file = tmp_path / f"{stem}.{fmt}"
            src_md.write_text(md_text, encoding="utf-8")

            cmd = [
                pandoc_bin,
                str(src_md),
                "--from=markdown+tex_math_dollars+yaml_metadata_block",
                "--standalone",
                "-o",
                str(out_file),
            ]
            if BIB_FILE_PATH.exists():
                cmd.extend(["--citeproc", f"--bibliography={BIB_FILE_PATH.resolve()}"])

            if fmt == "pdf":
                xelatex_bin = shutil.which("xelatex")
                if not xelatex_bin:
                    raise RuntimeError("Cal tindre 'xelatex' instal·lat per generar PDF")
                cmd.extend([
                    f"--pdf-engine={xelatex_bin}",
                    "-V", "geometry:margin=2.2cm",
                    "-V", "fontsize=11pt",
                    "-V", "colorlinks=true",
                    "-V", "linkcolor=blue",
                ])
                media = "application/pdf"
            elif fmt == "html":
                cmd.append("--embed-resources")
                media = "text/html; charset=utf-8"
            else:
                media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not out_file.exists():
                err_msg = (proc.stderr or proc.stdout or "Error desconegut de Pandoc").strip()
                raise RuntimeError(err_msg[-800:])

            return out_file.read_bytes(), media, f"{stem}.{fmt}"

    try:
        data_bytes, media_type, out_name = await asyncio.to_thread(_compilar_sync)
        return Response(
            content=data_bytes,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error compilant el document ({fmt}): {exc}")
