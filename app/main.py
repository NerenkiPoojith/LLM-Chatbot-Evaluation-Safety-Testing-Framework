"""FastAPI application: the chatbot's web pages and its REST API.

Run it with:
    uvicorn app.main:app --reload
Then open:
    http://127.0.0.1:8000/            the chat page (ask questions, drop in a manual)
    http://127.0.0.1:8000/guide.html  the project guide page
    http://127.0.0.1:8000/docs        the interactive Swagger UI for the API
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import PROJECT_ROOT, get_settings
from app.guardrails import detect_prompt_injection
from app.ingest import ManualError
from app.pii import redact_pii
from app.prompts import PROMPT_VERSION
from app.rag import ChatbotPipeline
from app.schemas import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    EvaluateRequest,
    GroundednessResponse,
    HealthResponse,
    ManualInfo,
    ManualUploadResponse,
    SafetyScanRequest,
    SafetyScanResponse,
    UploadProgress,
)
from app.formats import SUPPORTED_SUMMARY

# Progress of uploads still being read, by the upload_id the page sends with the file. Only the
# stage and page counts are kept, never content; an entry is removed when its upload finishes.
UPLOAD_PROGRESS: dict[str, UploadProgress] = {}
UPLOAD_ID_PATTERN = r"^[a-z0-9-]{8,64}$"

logger = logging.getLogger("appliancehelp")

DESCRIPTION = """
**ApplianceHelp** answers questions about home appliances using retrieval-augmented
generation (RAG) over product manuals. The chat page at `/` uses this API.

* `POST /v1/chat`: ask a question (optionally about one manual); returns the answer, the cited manual chunks and guardrail details.
* `POST /v1/manuals`: upload your own manual in almost any document format (PDF, Word, slides, spreadsheets, web pages, e-books, scans and photos) and get a `manual_id` to ask about it.
* `POST /v1/evaluate`: score any answer for groundedness against a context.
* `POST /v1/safety/scan`: run the prompt-injection and PII detectors on arbitrary text.
"""

TAGS = [
    {"name": "chat", "description": "Ask the chatbot questions."},
    {"name": "manuals", "description": "List, upload and delete manuals."},
    {"name": "evaluation", "description": "Score answers for groundedness and hallucination."},
    {"name": "safety", "description": "Prompt-injection and PII detectors."},
    {"name": "system", "description": "Health information."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the pipeline (load manuals, embed chunks) once at start-up, not per request.
    pipeline = ChatbotPipeline()
    app.state.pipeline = pipeline
    app.state.evaluator = None  # created on first /v1/evaluate call
    logger.info("Ready: %d chunks, provider=%s", pipeline.chunk_count, pipeline.llm_provider)
    yield


app = FastAPI(
    title="ApplianceHelp RAG Chatbot API",
    version="1.1.0",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
)

# The API runs on the user's own machine, so any website they visit could try to use it.
# Writes sent by other websites are refused here; TrustedHostMiddleware below stops DNS rebinding.
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_cross_site(request: Request) -> bool:
    """True for a request another website made the browser send (cross-site request forgery).

    Browsers send Origin with every POST and DELETE; it must name this server. Without an
    Origin, Sec-Fetch-Site still tells a same-origin request from a cross-site one. API clients
    such as curl, Postman or the tests send neither header, so they are not affected.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        return origin == "null" or urlparse(origin).netloc.lower() != request.headers.get("host", "").lower()
    return request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}


@app.middleware("http")
async def refuse_cross_site_writes(request: Request, call_next):
    # A multipart upload needs no CORS preflight, so without this check any web page could
    # quietly add a manual. The check runs before the body is read.
    if request.method in STATE_CHANGING_METHODS and _is_cross_site(request):
        return JSONResponse(status_code=403, content={"detail": "Cross-site request refused."})
    return await call_next(request)


# Added last, so it runs first: a foreign Host header means DNS rebinding (a remote page posing
# as this server) and is refused with 400. The ALLOWED_HOSTS setting lists the accepted names.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(get_settings().allowed_hosts))


def _pipeline(request: Request) -> ChatbotPipeline:
    return request.app.state.pipeline


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # Never leak stack traces (or prompt contents) to API clients.
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ------------------------------------------------------------------- API
@app.get("/health", response_model=HealthResponse, tags=["system"])
def health(request: Request) -> HealthResponse:
    p = _pipeline(request)
    return HealthResponse(
        status="ok",
        llm_provider=p.llm_provider,
        chat_model=p.generator.model_name,
        prompt_version=PROMPT_VERSION,
        manuals=len(p.registry),
        chunks=p.chunk_count,
        guardrails_enabled=p.guardrails_enabled,
        max_upload_mb=p.settings.max_upload_mb,
    )


@app.get("/v1/manuals", response_model=list[ManualInfo], tags=["manuals"])
def list_manuals(request: Request) -> list[ManualInfo]:
    return [ManualInfo(**m) for m in _pipeline(request).manuals()]


@app.post(
    "/v1/manuals",
    response_model=ManualUploadResponse,
    status_code=201,
    tags=["manuals"],
    responses={
        200: {"model": ManualUploadResponse,
              "description": "A file with exactly this content was already added; that manual is returned."},
        403: {"model": ErrorResponse, "description": "Refused: the request came from another website."},
        413: {"model": ErrorResponse, "description": "The file is larger than the upload limit, or has too much text."},
        415: {"model": ErrorResponse, "description": "A file type that cannot be read as a manual (a program, "
                                                     "audio, video, an archive...)."},
    },
)
def upload_manual(
    request: Request,
    response: Response,
    file: UploadFile = File(..., description=f"The manual. Supported: {SUPPORTED_SUMMARY}."),
    title: str | None = Form(None, max_length=120, description="Optional display name, e.g. 'My washing machine'."),
    upload_id: str | None = Form(None, pattern=UPLOAD_ID_PATTERN,
                                 description="Optional id chosen by the client, to follow a long upload "
                                             "with GET /v1/manuals/progress/{upload_id}."),
) -> ManualUploadResponse:
    """Add a manual and get a `manual_id` to ask about it.

    Supported: PDF (including scanned PDFs), Word, Rich Text, OpenDocument, PowerPoint, Excel, CSV,
    web pages, EPUB, XPS, Markdown, plain text, and photos or scans of the pages. The format is
    recognised from the file's content. Scanned pages and photos are read with OCR, at about
    6 seconds per page; pass an `upload_id` to follow that progress.

    A 422 response has one of two shapes: the usual validation list (for example when the
    `file` field is missing), or `{"detail": "<message>"}` when the file itself cannot be used
    (no name, empty, damaged, password-protected, or no readable text).
    """
    pipeline = _pipeline(request)
    limit = pipeline.settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)  # read at most one byte past the limit
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"This file is larger than the {pipeline.settings.max_upload_mb} MB limit.")
    if not data:
        raise HTTPException(status_code=422, detail="This file is empty.")

    def progress(stage: str, done: int, total: int) -> None:
        if upload_id:
            UPLOAD_PROGRESS[upload_id] = UploadProgress(stage=stage, done=done, total=total)

    try:
        record, duplicate = pipeline.add_manual(data, file.filename or "", title, progress=progress)
    except ManualError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    finally:
        if upload_id:
            UPLOAD_PROGRESS.pop(upload_id, None)
    if duplicate:
        response.status_code = 200
    return ManualUploadResponse(**record.public(), duplicate=duplicate)


@app.get(
    "/v1/manuals/progress/{upload_id}",
    response_model=UploadProgress,
    tags=["manuals"],
    responses={404: {"model": ErrorResponse, "description": "No upload with this id is being read right now."}},
)
def upload_progress(upload_id: str) -> UploadProgress:
    """How far a running upload has got, e.g. `{"stage": "ocr", "done": 12, "total": 45}`."""
    progress = UPLOAD_PROGRESS.get(upload_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="No upload with this id is being read right now.")
    return progress


@app.delete(
    "/v1/manuals/{manual_id}",
    status_code=204,
    tags=["manuals"],
    responses={
        403: {"model": ErrorResponse, "description": "Built-in manuals cannot be deleted."},
        404: {"model": ErrorResponse, "description": "Unknown manual_id."},
    },
)
def delete_manual(manual_id: str, request: Request) -> Response:
    try:
        _pipeline(request).delete_manual(manual_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown manual_id.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Built-in manuals cannot be deleted.") from exc
    return Response(status_code=204)


@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    tags=["chat"],
    responses={
        404: {"model": ErrorResponse, "description": "Unknown manual_id."},
        502: {"model": ErrorResponse, "description": "The language model is unavailable."},
    },
)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
    try:
        result = _pipeline(request).ask(body.question, top_k=body.top_k, manual_id=body.manual_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown manual_id. Use GET /v1/manuals to list them.") from exc
    except Exception as exc:  # e.g. the Claude API is unreachable
        logger.exception("Chat failed")
        raise HTTPException(status_code=502, detail="The language model is unavailable. Try again later.") from exc
    return ChatResponse(**result.__dict__)


@app.post("/v1/evaluate", response_model=GroundednessResponse, tags=["evaluation"])
def evaluate(body: EvaluateRequest, request: Request) -> GroundednessResponse:
    from evaluation.groundedness import build_evaluator  # local import keeps API start-up light

    state = request.app.state
    if state.evaluator is None:
        state.evaluator = build_evaluator(state.pipeline.settings, state.pipeline.embeddings)
    result = state.evaluator.evaluate(body.question, body.answer, body.contexts, use_judge=body.use_llm_judge)
    return GroundednessResponse(**result.to_dict())


@app.post("/v1/safety/scan", response_model=SafetyScanResponse, tags=["safety"])
def safety_scan(body: SafetyScanRequest, request: Request) -> SafetyScanResponse:
    injection = detect_prompt_injection(body.text)
    # Only the official contacts are allowed here: an uploaded manual's support line is
    # allowed only in answers from that manual.
    redacted, findings = redact_pii(body.text, _pipeline(request).base_allowlist)
    return SafetyScanResponse(
        injection_flagged=injection.flagged,
        injection_rules=injection.matched_rules,
        pii=[{"kind": f.kind, "value": f.value} for f in findings],
        redacted_text=redacted,
    )


# ----------------------------------------------------------------- pages
# Only these exact files are served. Nothing else in the project folder (such as .env)
# can be reached, because no URL is ever mapped onto a path the client chooses.
TEXT = "text/plain; charset=utf-8"
PUBLIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("site/index.html", "text/html; charset=utf-8"),
    "/index.html": ("site/index.html", "text/html; charset=utf-8"),
    "/guide.html": ("site/guide.html", "text/html; charset=utf-8"),
    "/brand-kit.html": ("site/brand-kit.html", "text/html; charset=utf-8"),
    "/sample-report.html": ("site/sample-report.html", "text/html; charset=utf-8"),
    # Linked from the guide page as ../<path>, which resolves to these URLs when served here.
    "/docs/LLM_Chatbot_Eval_Guide.pdf": ("docs/LLM_Chatbot_Eval_Guide.pdf", "application/pdf"),
    "/README.md": ("README.md", TEXT),
    "/evaluation/datasets/golden_qa.jsonl": ("evaluation/datasets/golden_qa.jsonl", TEXT),
    "/postman/ApplianceHelp.postman_collection.json": ("postman/ApplianceHelp.postman_collection.json", "application/json"),
    "/tests/test_safety.py": ("tests/test_safety.py", TEXT),
    "/data/manuals/aquawave_wm7000_washing_machine.md": ("data/manuals/aquawave_wm7000_washing_machine.md", TEXT),
}


def _serve(relative_path: str, media_type: str):
    path = PROJECT_ROOT / relative_path

    def handler() -> FileResponse:
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, media_type=media_type)

    return handler


for _url, (_path, _media_type) in PUBLIC_FILES.items():
    app.add_api_route(_url, _serve(_path, _media_type), methods=["GET"], include_in_schema=False)
