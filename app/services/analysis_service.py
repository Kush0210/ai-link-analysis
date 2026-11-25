"""End-to-end website link analysis orchestration service."""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.config import Settings
from app.schemas import (
    ExtractedWebsiteContent,
    LinkAnalysisResponse,
    ResponseMetadata,
    SupportedPlatform,
)
from app.services.competitor_service import CompetitorService
from app.services.llm_service import LLMService
from app.services.website_extractor import WebsiteExtractor
from app.utils.cache import TTLCache
from app.utils.validators import canonical_cache_key, validate_public_url

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS: tuple[SupportedPlatform, ...] = (
    "Google Search",
    "Facebook",
    "Instagram",
    "LinkedIn",
    "X (Twitter)",
)

SYSTEM_PROMPT = """You are a senior digital marketing strategist, SEO specialist, and competitive intelligence analyst.

Your task is to analyze the extracted content of a real website and return a single, strictly valid JSON object containing structured business, competitor, search query, SEO, services, and target audience insights.

CRITICAL INTEGRITY RULES:
1. Ground EVERY insight strictly in the actual extracted website content provided. Never produce generic filler unrelated to the submitted website.
2. Competitors: Identify at least 3 realistic, relevant competitors in the same category/market when reliable information exists. Set "source_type" to "website_mentioned" ONLY if the competitor is explicitly named in the website text; otherwise set "source_type" to "ai_inferred".
3. Search Queries: Generate EXACTLY 5 high-intent target search queries across the 5 supported platforms ("Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)"). Ensure every one of those 5 platforms is used at least once across the 5 queries. NEVER invent or report search volume numbers, CPCs, or ranking positions.
4. SEO Improvements: Provide primary keyword, secondary keywords, long-tail keywords, relevant business keywords, an improved SEO title (50-60 chars ideal), title structure notes, a human-friendly meta description (140-160 chars ideal), and 4-6 practical supporting content ideas covering diverse types ("Service Landing Page", "Industry-Specific Page", "FAQ", "Blog Topic", "Location-Based Landing Page", "Supporting Business Article").
5. Top 5 Services: Identify up to 5 core services offered by the website. Prefer services explicitly stated on the website ("source": "extracted_from_website"). If fewer than 5 are explicitly listed, you may include clearly labeled inferred recommendations ("source": "inferred_recommendation").
6. Target Audience: Provide primary & secondary audiences, industry segments, business types, 4-5 structured customer profiles, geographical focus (use "Not explicitly specified on website" if absent), customer needs/challenges, customer interests, and recommended marketing segments.
7. Output ONLY valid JSON matching the exact schema requested — no markdown fences, no commentary, no extra keys outside the schema.
"""


class AnalysisService:
    """Coordinates validation, caching, extraction, LLM generation, and post-processing."""

    def __init__(
        self,
        settings: Settings,
        extractor: Optional[WebsiteExtractor] = None,
        llm_service: Optional[LLMService] = None,
        competitor_service: Optional[CompetitorService] = None,
        cache: Optional[TTLCache[LinkAnalysisResponse]] = None,
    ) -> None:
        self._settings = settings
        self._extractor = extractor or WebsiteExtractor(settings)
        self._llm_service = llm_service or LLMService(settings)
        self._competitor_service = competitor_service or CompetitorService()
        self._cache = cache or TTLCache[LinkAnalysisResponse](
            max_size=settings.cache_max_size,
            ttl_seconds=settings.cache_ttl_seconds if settings.cache_enabled else 0,
        )

    async def analyze_url(
        self,
        url: str,
        force_refresh: bool = False,
    ) -> LinkAnalysisResponse:
        """Analyze a website URL end-to-end and return the structured 5-section response."""
        normalized_url = validate_public_url(url)
        cache_key = canonical_cache_key(normalized_url)

        if self._settings.cache_enabled and not force_refresh:
            cached_response = self._cache.get(cache_key)
            if cached_response is not None:
                logger.info("Returning cached analysis for %s", cache_key)
                return cached_response.model_copy(
                    update={
                        "metadata": cached_response.metadata.model_copy(
                            update={"cached": True, "latency_ms": 0}
                        )
                    }
                )

        extracted = await self._extractor.extract(normalized_url)
        user_prompt = self._build_user_prompt(extracted)
        llm_result = await self._llm_service.generate_analysis(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        payload = llm_result.payload

        # Post-process Section 1 via CompetitorService
        refined_competitors = self._competitor_service.refine(
            competitor_analysis=payload.analysis.competitor_analysis,
            brand=payload.brand,
            extracted=extracted,
        )

        # Populate real extracted SEO baselines so the user can compare current vs recommended
        refined_seo = payload.analysis.seo_improvements.model_copy(
            update={
                "current_title": extracted.title,
                "current_meta_description": extracted.meta_description,
            }
        )

        # Keep top 5 queries and top 5 services cleanly bounded
        queries = payload.analysis.target_search_queries[:5]
        services = payload.analysis.top_services[:5]

        refined_analysis = payload.analysis.model_copy(
            update={
                "competitor_analysis": refined_competitors,
                "target_search_queries": queries,
                "seo_improvements": refined_seo,
                "top_services": services,
            }
        )

        response = LinkAnalysisResponse(
            success=True,
            website=extracted.final_url,
            brand=payload.brand,
            analysis=refined_analysis,
            metadata=ResponseMetadata(
                provider=llm_result.provider,
                model=llm_result.model,
                cached=False,
                extracted_pages=1 + len(extracted.subpages_crawled),
                input_tokens=llm_result.input_tokens,
                output_tokens=llm_result.output_tokens,
                estimated_cost_usd=llm_result.estimated_cost_usd,
                latency_ms=llm_result.latency_ms,
            ),
        )

        if self._settings.cache_enabled:
            self._cache.set(cache_key, response)

        return response

    @staticmethod
    def _build_user_prompt(extracted: ExtractedWebsiteContent) -> str:
        """Assemble a compact, high-signal prompt from extracted website data."""
        schema_template = {
            "brand": {
                "name": "string",
                "category": "string",
                "summary": "string (1-2 sentences)",
                "main_products_and_services": ["string"],
            },
            "analysis": {
                "competitor_analysis": {
                    "brand_name": "string",
                    "brand_overview": "string",
                    "main_business_category": "string",
                    "main_products_and_services": ["string"],
                    "competitors": [
                        {
                            "name": "string",
                            "website": "string or null",
                            "main_service": "string",
                            "target_market": "string",
                            "strengths": ["string"],
                            "weaknesses_or_gaps": ["string"],
                            "source_type": "ai_inferred | website_mentioned",
                        }
                    ],
                    "comparison_table": [
                        {
                            "dimension": "Brand Name | Main Service | Target Market | Key Strength | Opportunity",
                            "your_brand": "string",
                            "competitor_benchmark": "string",
                        }
                    ],
                    "strengths": ["string (your brand's competitive advantages)"],
                    "opportunities": ["string (market positioning improvements)"],
                },
                "target_search_queries": [
                    {
                        "query": "string",
                        "platforms": ["Google Search | Facebook | Instagram | LinkedIn | X (Twitter)"],
                        "primary_platform": "Google Search | Facebook | Instagram | LinkedIn | X (Twitter)",
                        "intent": "string (e.g., Service Search, Business Discovery, Inspiration, B2B Search, Service Discovery)",
                        "business_relevance": "string",
                        "target_audience": "string",
                    }
                ],
                "seo_improvements": {
                    "primary_keyword": "string",
                    "secondary_keywords": ["string (4-6 items)"],
                    "long_tail_keywords": ["string (4-6 items)"],
                    "relevant_business_keywords": ["string (4-6 items)"],
                    "recommended_title": "string (50-60 chars ideal)",
                    "title_structure_notes": "string",
                    "meta_description": "string (140-160 chars ideal)",
                    "supporting_content": [
                        {
                            "content_type": "Service Landing Page | Industry-Specific Page | FAQ | Blog Topic | Location-Based Landing Page | Supporting Business Article",
                            "title": "string",
                            "purpose": "string",
                            "target_keywords": ["string"],
                        }
                    ],
                },
                "top_services": [
                    {
                        "service_name": "string",
                        "description": "string",
                        "target_industry": "string",
                        "target_customer": "string",
                        "main_benefit": "string",
                        "source": "extracted_from_website | inferred_recommendation",
                    }
                ],
                "target_audience": {
                    "primary_audience": ["string"],
                    "secondary_audience": ["string"],
                    "industries": ["string"],
                    "business_types": ["string"],
                    "customer_profiles": [
                        {
                            "segment": "string",
                            "business_type": "string",
                            "requirement": "string",
                            "key_challenge": "string",
                        }
                    ],
                    "geographical_focus": "string",
                    "customer_needs": ["string"],
                    "customer_interests": ["string"],
                    "recommended_marketing_segments": ["string"],
                },
            },
        }

        website_context = {
            "url": extracted.final_url,
            "domain": extracted.domain,
            "language": extracted.language,
            "title": extracted.title,
            "meta_description": extracted.meta_description,
            "meta_keywords": extracted.meta_keywords,
            "og_site_name": extracted.og_site_name,
            "og_title": extracted.og_title,
            "og_description": extracted.og_description,
            "headings": extracted.headings,
            "navigation_links": extracted.navigation_links,
            "social_links": extracted.social_links,
            "structured_data": extracted.structured_data,
            "subpages_crawled": extracted.subpages_crawled,
            "cleaned_content": extracted.main_text,
        }

        return (
            "Analyze the following extracted website data and return ONLY a valid JSON object matching the exact schema below.\n\n"
            f"REQUIRED JSON SCHEMA:\n{json.dumps(schema_template, indent=2)}\n\n"
            f"EXTRACTED WEBSITE DATA:\n{json.dumps(website_context, ensure_ascii=False)}"
        )
