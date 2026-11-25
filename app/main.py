"""FastAPI entrypoint for the AI-Powered Website Link Analysis API."""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.schemas import ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.llm_service import LLMConfigurationError, LLMProviderError
from app.services.website_extractor import WebsiteExtractionError
from app.utils.validators import URLValidationError

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Analyzes a public website URL using an LLM to generate structured Competitor, "
        "Top 5 Search Queries, SEO Improvements, Top 5 Services, and Target Audience insights."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@lru_cache(maxsize=1)
def get_analysis_service() -> AnalysisService:
    """Provide a singleton AnalysisService with shared in-memory TTL cache."""
    return AnalysisService(settings=get_settings())


# ---------------------------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------------------------


@app.exception_handler(URLValidationError)
async def url_validation_exception_handler(_: Request, exc: URLValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(error="invalid_url", detail=str(exc)).model_dump(),
    )


@app.exception_handler(WebsiteExtractionError)
async def website_extraction_exception_handler(
    _: Request, exc: WebsiteExtractionError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error="website_extraction_failed", detail=str(exc)).model_dump(),
    )


@app.exception_handler(LLMConfigurationError)
async def llm_config_exception_handler(_: Request, exc: LLMConfigurationError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=ErrorResponse(error="llm_not_configured", detail=str(exc)).model_dump(),
    )


@app.exception_handler(LLMProviderError)
async def llm_provider_exception_handler(_: Request, exc: LLMProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content=ErrorResponse(error="llm_provider_error", detail=str(exc)).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_server_error",
            detail="An unexpected internal error occurred while analyzing the website.",
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", tags=["General"])
async def root(cfg: Settings = Depends(get_settings)) -> dict:
    """API discovery endpoint."""
    return {
        "name": cfg.app_name,
        "version": cfg.app_version,
        "status": "running",
        "endpoints": {
            "link_analysis": "POST /link-analysis",
            "health": "GET /health",
            "docs": "GET /docs",
        },
    }


@app.get("/health", tags=["General"])
async def health(cfg: Settings = Depends(get_settings)) -> dict:
    """Health check endpoint for local monitoring and cloud deployments."""
    configured_providers = [
        provider
        for provider in ("gemini", "openai", "anthropic")
        if cfg.get_api_key(provider)
    ]
    return {
        "status": "ok",
        "primary_provider": cfg.llm_provider,
        "primary_model": cfg.get_model(cfg.llm_provider),
        "configured_providers": configured_providers,
        "cache_enabled": cfg.cache_enabled,
    }


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    responses={
        422: {"model": ErrorResponse, "description": "Invalid URL, SSRF violation, or unreadable page."},
        502: {"model": ErrorResponse, "description": "Website unreachable or upstream LLM failure."},
        503: {"model": ErrorResponse, "description": "LLM API credentials are not configured."},
        504: {"model": ErrorResponse, "description": "Website fetch timed out."},
    },
    tags=["Website Analysis"],
    summary="Analyze a website URL across 5 AI-powered business & SEO sections",
)
async def link_analysis(
    payload: LinkAnalysisRequest,
    service: AnalysisService = Depends(get_analysis_service),
) -> LinkAnalysisResponse:
    """Validate and fetch the target website, analyze its content with an LLM, and return 5 structured sections."""
    return await service.analyze_url(
        url=payload.url,
        force_refresh=payload.force_refresh,
    )
