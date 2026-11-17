"""FastAPI entry point.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 4000
Docs: http://localhost:4000/docs
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.schemas import LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import analyze_website
from app.services.llm_service import LLMError
from app.services.website_extractor import WebsiteFetchError
from app.utils.validators import InvalidURLError, UnsafeURLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI-Powered Website Link Analysis API",
    version="1.0.0",
    description=(
        "Fetches a website, understands it with an LLM and returns five structured sections: "
        "competitors, top search queries, SEO improvements, top services and target audience."
    ),
)


@app.get("/", include_in_schema=False)
async def redirect_to_docs() -> RedirectResponse:
    return RedirectResponse(url="/docs")


def _error(status: int, message: str, details: list[dict] | None = None) -> JSONResponse:
    body: dict = {"success": False, "error": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()) if part != "body") or "body"
        message = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
        details.append({"field": field, "message": message})
    return _error(422, "Validation failed", details)


@app.exception_handler(InvalidURLError)
async def invalid_url_handler(request: Request, exc: InvalidURLError) -> JSONResponse:
    return _error(422, "Invalid URL", [{"field": "url", "message": str(exc)}])


@app.exception_handler(UnsafeURLError)
async def unsafe_url_handler(request: Request, exc: UnsafeURLError) -> JSONResponse:
    return _error(400, "URL not allowed", [{"field": "url", "message": str(exc)}])


@app.exception_handler(WebsiteFetchError)
async def website_error_handler(request: Request, exc: WebsiteFetchError) -> JSONResponse:
    return _error(exc.status_code, "Website could not be analyzed", [{"field": "url", "message": exc.message}])


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    logger.error("LLM error: %s", exc.message)
    return _error(exc.status_code, "LLM analysis failed", [{"field": "llm", "message": exc.message}])


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unexpected error")
    return _error(500, "Internal server error")


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    tags=["analysis"],
    summary="Analyze a website URL",
    responses={
        400: {"description": "URL points to a private/internal address (SSRF protection)"},
        422: {"description": "Invalid URL, or the website blocked access / has no readable content"},
        502: {"description": "Website or LLM provider failed"},
        503: {"description": "LLM not configured or rate limited"},
        504: {"description": "Website or LLM timed out"},
    },
)
async def link_analysis(payload: LinkAnalysisRequest) -> LinkAnalysisResponse:
    """Validate the URL, fetch the page, analyze it with the configured LLM and return 5 sections."""
    return await analyze_website(payload.url, get_settings())
