"""Pydantic schemas for request validation, website extraction, and API responses."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SupportedPlatform = Literal[
    "Google Search",
    "Facebook",
    "Instagram",
    "LinkedIn",
    "X (Twitter)",
]

_PLATFORM_ALIASES: dict[str, SupportedPlatform] = {
    "google": "Google Search",
    "google search": "Google Search",
    "facebook": "Facebook",
    "fb": "Facebook",
    "instagram": "Instagram",
    "ig": "Instagram",
    "linkedin": "LinkedIn",
    "x": "X (Twitter)",
    "twitter": "X (Twitter)",
    "x (twitter)": "X (Twitter)",
    "twitter (x)": "X (Twitter)",
    "x/twitter": "X (Twitter)",
}


def _normalize_platform(value: object) -> object:
    if isinstance(value, str):
        mapped = _PLATFORM_ALIASES.get(value.strip().lower())
        if mapped:
            return mapped
    return value


def _normalize_content_type(value: object) -> object:
    if not isinstance(value, str):
        return value
    low = value.strip().lower()
    if "service" in low:
        return "Service Landing Page"
    if "industr" in low:
        return "Industry-Specific Page"
    if "faq" in low or "question" in low:
        return "FAQ"
    if "location" in low or "local" in low or "city" in low or "geo" in low:
        return "Location-Based Landing Page"
    if "blog" in low:
        return "Blog Topic"
    if "article" in low or "guide" in low or "case study" in low or "support" in low:
        return "Supporting Business Article"
    return value


# ---------------------------------------------------------------------------
# Request Schema
# ---------------------------------------------------------------------------


class LinkAnalysisRequest(BaseModel):
    """Input payload for POST /link-analysis."""

    url: str = Field(
        ...,
        description="Public website URL to analyze (http or https).",
        examples=["https://stripe.com", "https://example.com"],
    )
    force_refresh: bool = Field(
        default=False,
        description="When true, bypasses the in-memory analysis cache and re-runs extraction and LLM analysis.",
    )

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("URL must not be empty.")
        return cleaned


# ---------------------------------------------------------------------------
# Internal Extracted Website Representation
# ---------------------------------------------------------------------------


class ExtractedWebsiteContent(BaseModel):
    """Structured, cleaned signals extracted from the target website."""

    requested_url: str
    final_url: str
    domain: str
    language: Optional[str] = None
    title: Optional[str] = None
    meta_description: Optional[str] = None
    meta_keywords: list[str] = Field(default_factory=list)
    og_title: Optional[str] = None
    og_description: Optional[str] = None
    og_site_name: Optional[str] = None
    headings: dict[str, list[str]] = Field(default_factory=dict)
    navigation_links: list[str] = Field(default_factory=list)
    social_links: dict[str, str] = Field(default_factory=dict)
    structured_data: list[dict] = Field(default_factory=list)
    main_text: str = ""
    subpages_crawled: list[str] = Field(default_factory=list)
    content_length_chars: int = 0


# ---------------------------------------------------------------------------
# Brand Summary
# ---------------------------------------------------------------------------


class BrandInfo(BaseModel):
    """High-level summary of the analyzed brand."""

    name: str = Field(..., description="Brand or organization name identified from the site.")
    category: str = Field(..., description="Primary business category or industry.")
    summary: str = Field(..., description="Concise 1-2 sentence overview of what the business does.")
    main_products_and_services: list[str] = Field(
        default_factory=list,
        description="Core products or services offered by the brand.",
    )


# ---------------------------------------------------------------------------
# Section 1: Competitor vs Your Brand Details
# ---------------------------------------------------------------------------


class CompetitorItem(BaseModel):
    """Details on an identified competitor."""

    name: str = Field(..., description="Competitor brand name.")
    website: Optional[str] = Field(
        default=None,
        description="Known competitor domain or website if applicable.",
    )
    main_service: str = Field(..., description="Competitor's primary product or service.")
    target_market: str = Field(..., description="Competitor's primary target market.")
    strengths: list[str] = Field(
        default_factory=list,
        description="Notable competitor strengths.",
    )
    weaknesses_or_gaps: list[str] = Field(
        default_factory=list,
        description="Competitor weaknesses or market gaps.",
    )
    source_type: Literal["website_mentioned", "ai_inferred"] = Field(
        default="ai_inferred",
        description="Indicates whether the competitor was explicitly mentioned on the website or inferred by the LLM.",
    )


class BrandComparisonRow(BaseModel):
    """Side-by-side comparison dimension between the brand and competitors."""

    dimension: str = Field(..., description="Comparison criterion (e.g., Main Service, Target Market, Key Strength).")
    your_brand: str = Field(..., description="How the analyzed brand positions on this dimension.")
    competitor_benchmark: str = Field(..., description="Typical competitor positioning on this dimension.")

    @model_validator(mode="before")
    @classmethod
    def normalize_comparison_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "dimension" not in out:
            for alias in ("comparison", "criteria", "criterion", "aspect", "metric", "attribute", "feature"):
                if alias in out:
                    out["dimension"] = out.pop(alias)
                    break
        if "your_brand" not in out:
            for alias in ("brand", "target_brand", "submitted_brand"):
                if alias in out:
                    out["your_brand"] = out.pop(alias)
                    break
        if "competitor_benchmark" not in out:
            for alias in ("competitor", "competitors", "competitor_value", "industry_benchmark", "benchmark"):
                if alias in out:
                    out["competitor_benchmark"] = out.pop(alias)
                    break
            else:
                # Fallback if the LLM used a specific competitor's name as the 3rd key
                remaining = [k for k in out if k not in ("dimension", "your_brand")]
                if remaining:
                    val = out.pop(remaining[0])
                    out["competitor_benchmark"] = str(val)
        return out


class CompetitorAnalysis(BaseModel):
    """Section 1: Competitor vs Your Brand Details."""

    brand_name: str = Field(..., description="Your brand name.")
    brand_overview: str = Field(..., description="Detailed business overview.")
    main_business_category: str = Field(..., description="Primary business category.")
    main_products_and_services: list[str] = Field(default_factory=list)
    competitors: list[CompetitorItem] = Field(
        default_factory=list,
        description="Relevant competitors (minimum 3 where reliable information is available).",
    )
    comparison_table: list[BrandComparisonRow] = Field(
        default_factory=list,
        description="Structured comparison matrix between the brand and competitors.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="Your brand's competitive advantages and key strengths.",
    )
    opportunities: list[str] = Field(
        default_factory=list,
        description="Actionable opportunities to improve market positioning.",
    )


# ---------------------------------------------------------------------------
# Section 2: Top 5 Target Search Queries
# ---------------------------------------------------------------------------


class TargetSearchQuery(BaseModel):
    """A high-intent customer search query mapped to supported platforms."""

    query: str = Field(..., description="Realistic search query potential customers would type.")
    platforms: list[SupportedPlatform] = Field(
        ...,
        min_length=1,
        description="Platforms where this query is relevant (Google Search, Facebook, Instagram, LinkedIn, X (Twitter)).",
    )
    primary_platform: SupportedPlatform = Field(
        ...,
        description="Most suitable platform for this specific query.",
    )
    intent: str = Field(
        ...,
        description="Search intent (e.g., Service Search, Business Discovery, Inspiration, B2B Search, Problem Solving).",
    )
    business_relevance: str = Field(
        ...,
        description="Why this query directly connects to the brand's offering.",
    )
    target_audience: str = Field(
        ...,
        description="Customer segment most likely to perform this search.",
    )

    @field_validator("primary_platform", mode="before")
    @classmethod
    def normalize_primary_platform(cls, value: object) -> object:
        return _normalize_platform(value)

    @field_validator("platforms", mode="before")
    @classmethod
    def normalize_platforms(cls, value: object) -> object:
        if isinstance(value, list):
            normalized: list[object] = []
            for item in value:
                mapped = _normalize_platform(item)
                if mapped not in normalized:
                    normalized.append(mapped)
            return normalized
        return value


# ---------------------------------------------------------------------------
# Section 3: SEO Improvement
# ---------------------------------------------------------------------------


class SupportingContentIdea(BaseModel):
    """Recommended supporting content page or topic to boost organic visibility."""

    content_type: Literal[
        "Service Landing Page",
        "Industry-Specific Page",
        "FAQ",
        "Blog Topic",
        "Location-Based Landing Page",
        "Supporting Business Article",
    ]
    title: str = Field(..., description="Suggested page or article title.")
    purpose: str = Field(..., description="How this content improves search visibility and conversions.")
    target_keywords: list[str] = Field(default_factory=list)

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return _normalize_content_type(value)


class SEOImprovements(BaseModel):
    """Section 3: Actionable SEO keyword, title, meta description, and content recommendations."""

    primary_keyword: str = Field(..., description="Single highest-priority target keyword.")
    secondary_keywords: list[str] = Field(default_factory=list)
    long_tail_keywords: list[str] = Field(default_factory=list)
    relevant_business_keywords: list[str] = Field(default_factory=list)
    current_title: Optional[str] = Field(
        default=None,
        description="Existing HTML <title> extracted from the website, if present.",
    )
    recommended_title: str = Field(
        ...,
        description="Optimized SEO title incorporating the primary keyword (ideally 50-60 characters).",
    )
    title_structure_notes: str = Field(
        ...,
        description="Brief explanation of the recommended title structure.",
    )
    current_meta_description: Optional[str] = Field(
        default=None,
        description="Existing meta description extracted from the website, if present.",
    )
    meta_description: str = Field(
        ...,
        description="Human-friendly, keyword-rich meta description (ideally 140-160 characters).",
    )
    supporting_content: list[SupportingContentIdea] = Field(
        default_factory=list,
        description="Additional content recommendations (service pages, FAQs, blogs, location/industry pages).",
    )


# ---------------------------------------------------------------------------
# Section 4: Top 5 Services
# ---------------------------------------------------------------------------


class ServiceItem(BaseModel):
    """One of the top 5 services identified from the website."""

    service_name: str = Field(..., description="Name of the service.")
    description: str = Field(..., description="Short explanation of the service.")
    target_industry: str = Field(..., description="Relevant business industry.")
    target_customer: str = Field(..., description="Potential customer type.")
    main_benefit: str = Field(..., description="Why customers need this service.")
    source: Literal["extracted_from_website", "inferred_recommendation"] = Field(
        default="extracted_from_website",
        description="Clearly distinguishes services found directly on the website from inferred recommendations.",
    )


# ---------------------------------------------------------------------------
# Section 5: Target Audience Identification
# ---------------------------------------------------------------------------


class CustomerProfile(BaseModel):
    """Structured customer segment profile."""

    segment: str = Field(..., description="Audience label (e.g., Small Businesses, SaaS Startups).")
    business_type: str = Field(..., description="Type of organization or buyer.")
    requirement: str = Field(..., description="Core requirement or job-to-be-done.")
    key_challenge: str = Field(..., description="Primary pain point solved by the brand.")


class TargetAudience(BaseModel):
    """Section 5: Target Audience Identification."""

    primary_audience: list[str] = Field(default_factory=list)
    secondary_audience: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    business_types: list[str] = Field(default_factory=list)
    customer_profiles: list[CustomerProfile] = Field(default_factory=list)
    geographical_focus: str = Field(
        default="Not explicitly specified on website",
        description="Location or regional focus extracted from the website, if available.",
    )
    customer_needs: list[str] = Field(
        default_factory=list,
        description="Key customer needs and challenges.",
    )
    customer_interests: list[str] = Field(
        default_factory=list,
        description="Relevant topics and interests of potential customers.",
    )
    recommended_marketing_segments: list[str] = Field(
        default_factory=list,
        description="Actionable audience segments for targeted marketing campaigns.",
    )


# ---------------------------------------------------------------------------
# Combined Analysis & Full API Response
# ---------------------------------------------------------------------------


class AnalysisSections(BaseModel):
    """All five structured analysis sections."""

    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[TargetSearchQuery] = Field(..., min_length=1, max_length=10)
    seo_improvements: SEOImprovements
    top_services: list[ServiceItem] = Field(..., min_length=1, max_length=10)
    target_audience: TargetAudience


class LLMAnalysisPayload(BaseModel):
    """Strict schema validated against the LLM's structured JSON output."""

    brand: BrandInfo
    analysis: AnalysisSections


class ResponseMetadata(BaseModel):
    """Operational telemetry for transparency, cost tracking, and debugging."""

    provider: str
    model: str
    cached: bool = False
    extracted_pages: int = 1
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    latency_ms: int = 0
    data_disclaimer: str = (
        "Competitors are labeled with source_type ('website_mentioned' or 'ai_inferred'), "
        "services are labeled with source ('extracted_from_website' or 'inferred_recommendation'), "
        "and no unverified search volumes or rankings are generated."
    )


class LinkAnalysisResponse(BaseModel):
    """Complete JSON response returned by POST /link-analysis."""

    success: bool = True
    website: str
    brand: BrandInfo
    analysis: AnalysisSections
    metadata: ResponseMetadata


class ErrorResponse(BaseModel):
    """Standardized error response payload."""

    success: bool = False
    error: str
    detail: str
