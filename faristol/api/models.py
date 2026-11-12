"""Models Pydantic de l'API."""

from typing import List, Optional

from pydantic import BaseModel


class GuardarNotaRequest(BaseModel):
    contingut: str

class NovaNotaRequest(BaseModel):
    titol: str
    tags: str = ""
    plantilla: str = "lliure"
    template: Optional[str] = None
    contingut: Optional[str] = None

class SaveHighlightRequest(BaseModel):
    pdf_path: str
    page: int
    text: str
    rects: list
    color: str = "#fef08a"
    comment: str = ""

class AcademicImportRequest(BaseModel):
    identifier: str
    vpn: bool = False
    pdf_url: Optional[str] = None

class FaristolCompanionNoteRequest(BaseModel):
    pdf: str

class FaristolOpenExternalRequest(BaseModel):
    pdf: str

class FaristolOpenNoteExternalRequest(BaseModel):
    filename: str

class FaristolIngestUrlRequest(BaseModel):
    url: str

class NovaColleccioRequest(BaseModel):
    name: str
    parent_id: Optional[str] = None
    icon: Optional[str] = None

class ActualitzarColleccioRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None
    icon: Optional[str] = None

class ItemColleccioRequest(BaseModel):
    type: str  # "note" o "pdf"
    filename: str

class SetItemCollectionsRequest(BaseModel):
    type: str
    filename: str
    collection_ids: List[str]

class GuardarColleccionsRequest(BaseModel):
    collections: List[dict]

class NlmQueryRequest(BaseModel):
    notebook_id: str
    query: str
    conversation_id: Optional[str] = None
    source_id: Optional[str] = None
    source_ids: Optional[List[str]] = None

class NlmAddUrlRequest(BaseModel):
    notebook_id: str
    url: str

class NlmDeleteRequest(BaseModel):
    target_type: str  # 'notebook' or 'source'
    item_id: str
    notebook_id: Optional[str] = None

class NlmSintesiRequest(BaseModel):
    fitxer_desti: str
    pregunta: str
    resposta: str
    origen: str


class LocalRagQueryRequest(BaseModel):
    query: str
    mode: str = "evidences"  # "evidences" (~170ms hybrid) or "synthesis" (hybrid + local LLM)
    top_k: int = 6
    filenames: Optional[List[str]] = None
    collection_id: Optional[str] = None
    llm_model: Optional[str] = None
    include_notes: bool = True


class LocalRagIndexRequest(BaseModel):
    filenames: Optional[List[str]] = None
    collection_id: Optional[str] = None
    force: bool = False
    include_notes: bool = True


class NoteSummarizeRequest(BaseModel):
    model: Optional[str] = None
    overwrite: bool = False


class ExportDocumentRequest(BaseModel):
    filename: Optional[str] = None
    title: str = "Document Faristol"
    content: str
    format: str = "pdf"  # "pdf", "html", "docx", "qmd"




