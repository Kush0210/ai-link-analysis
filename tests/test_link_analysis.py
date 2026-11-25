"""Automated tests covering SSRF validation, HTML extraction, competitor post-processing, caching, and API routes."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_analysis_service
from app.schemas import (
    ExtractedWebsiteContent,
    LLMAnalysisPayload,
)
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.llm_service import LLMConfigurationError, LLMResult, LLMService
from app.services.website_extractor import WebsiteExtractionError, WebsiteExtractor
from app.utils.validators import URLValidationError, is_ip_public, normalize_url, validate_public_url

# ---------------------------------------------------------------------------
# Fixtures & Sample Data
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>NovaWeb Studio | Custom Web Development &amp; Cloud Solutions</title>
  <meta name="description" content="NovaWeb Studio builds fast web applications, e-commerce stores, and SaaS platforms for startups and SMEs in Austin, Texas." />
  <meta name="keywords" content="web development, SaaS engineering, e-commerce development" />
  <meta property="og:site_name" content="NovaWeb Studio" />
  <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Organization",
      "name": "NovaWeb Studio",
      "url": "https://novawebstudio.example",
      "areaServed": "United States"
    }
  </script>
  <style>body { color: #333; }</style>
  <script>console.log("should be stripped");</script>
</head>
<body>
  <header>
    <nav>
      <a href="/services">Services</a>
      <a href="/about">About Us</a>
      <a href="/pricing">Pricing</a>
      <a href="https://www.linkedin.com/company/novawebstudio">LinkedIn</a>
    </nav>
  </header>
  <main>
    <h1>Modern Web Development for Growing Startups</h1>
    <p>NovaWeb Studio helps startups and small businesses launch scalable web apps, APIs, and e-commerce platforms.</p>
    <h2>Our Services</h2>
    <ul>
      <li>Custom Web Application Development</li>
      <li>E-Commerce Storefront Engineering</li>
      <li>UI/UX Design Systems</li>
      <li>Cloud Architecture &amp; DevOps</li>
      <li>Technical SEO &amp; Performance Optimization</li>
    </ul>
    <p>Unlike legacy enterprise shops such as GlobalMegaConsult, we deliver transparent fixed-scope sprints.</p>
  </main>
  <footer>
    <address>Austin, TX, USA</address>
  </footer>
</body>
</html>
"""


def build_sample_llm_payload() -> LLMAnalysisPayload:
    return LLMAnalysisPayload.model_validate(
        {
            "brand": {
                "name": "NovaWeb Studio",
                "category": "Web Development & Digital Engineering",
                "summary": "NovaWeb Studio builds custom web applications, e-commerce platforms, and cloud backends for startups and SMEs.",
                "main_products_and_services": [
                    "Custom Web Application Development",
                    "E-Commerce Storefront Engineering",
                    "UI/UX Design Systems",
                    "Cloud Architecture & DevOps",
                    "Technical SEO & Performance Optimization",
                ],
            },
            "analysis": {
                "competitor_analysis": {
                    "brand_name": "NovaWeb Studio",
                    "brand_overview": "Austin-based web development studio serving startups and SMEs with fixed-scope engineering sprints.",
                    "main_business_category": "Web Development & Digital Engineering",
                    "main_products_and_services": [
                        "Custom Web Application Development",
                        "E-Commerce Storefront Engineering",
                        "Cloud Architecture & DevOps",
                    ],
                    "competitors": [
                        {
                            "name": "NovaWeb Studio",
                            "website": "https://novawebstudio.example",
                            "main_service": "Web Development",
                            "target_market": "Startups",
                            "strengths": ["Self reference should be stripped"],
                            "weaknesses_or_gaps": [],
                            "source_type": "ai_inferred",
                        },
                        {
                            "name": "GlobalMegaConsult",
                            "website": None,
                            "main_service": "Enterprise IT Consulting",
                            "target_market": "Enterprise",
                            "strengths": ["Global brand recognition"],
                            "weaknesses_or_gaps": ["Slow delivery cycles and high retainers"],
                            "source_type": "ai_inferred",
                        },
                        {
                            "name": "Toptal",
                            "website": "https://toptal.com",
                            "main_service": "Freelance Software Engineering Talent",
                            "target_market": "Startups & Mid-Market",
                            "strengths": ["Large vetted talent pool"],
                            "weaknesses_or_gaps": ["Higher hourly rates and less cohesive studio delivery"],
                            "source_type": "ai_inferred",
                        },
                        {
                            "name": "Thoughtbot",
                            "website": "https://thoughtbot.com",
                            "main_service": "Product Design & Web Development",
                            "target_market": "Venture-backed Startups",
                            "strengths": ["Strong design-led engineering reputation"],
                            "weaknesses_or_gaps": ["Premium pricing for early-stage SMBs"],
                            "source_type": "ai_inferred",
                        },
                    ],
                    "comparison_table": [
                        {
                            "dimension": "Brand Name",
                            "your_brand": "NovaWeb Studio",
                            "competitor_benchmark": "GlobalMegaConsult / Thoughtbot",
                        },
                        {
                            "dimension": "Main Service",
                            "your_brand": "Fixed-scope web apps & e-commerce",
                            "competitor_benchmark": "Custom enterprise & product engineering",
                        },
                    ],
                    "strengths": [
                        "Transparent fixed-scope sprints tailored to startups and SMEs",
                        "End-to-end coverage from UI/UX to DevOps and technical SEO",
                    ],
                    "opportunities": [
                        "Publish detailed case studies with measurable performance gains",
                        "Create dedicated industry landing pages for Austin and US SaaS startups",
                    ],
                },
                "target_search_queries": [
                    {
                        "query": "custom web application development agency Austin",
                        "platforms": ["Google", "LinkedIn"],
                        "primary_platform": "Google",
                        "intent": "Service Search",
                        "business_relevance": "Directly matches NovaWeb Studio's core web app development offering and Austin location.",
                        "target_audience": "Startup founders and CTOs",
                    },
                    {
                        "query": "fixed scope web development services for small business",
                        "platforms": ["Facebook", "Google Search"],
                        "primary_platform": "Facebook",
                        "intent": "Business Discovery",
                        "business_relevance": "Matches fixed-scope sprint model for small businesses.",
                        "target_audience": "Small business owners",
                    },
                    {
                        "query": "modern SaaS UI UX design system showcase",
                        "platforms": ["Instagram"],
                        "primary_platform": "Instagram",
                        "intent": "Inspiration",
                        "business_relevance": "Highlights NovaWeb Studio's UI/UX Design Systems capability.",
                        "target_audience": "Product managers and startup founders",
                    },
                    {
                        "query": "web development partner for seed stage SaaS startups",
                        "platforms": ["LinkedIn"],
                        "primary_platform": "LinkedIn",
                        "intent": "B2B Search",
                        "business_relevance": "Connects B2B decision makers looking for a startup engineering studio.",
                        "target_audience": "Seed-stage SaaS founders and VPs of Engineering",
                    },
                    {
                        "query": "fast ecommerce storefront engineering agency recommendations",
                        "platforms": ["X", "LinkedIn"],
                        "primary_platform": "X",
                        "intent": "Service Discovery",
                        "business_relevance": "Captures founders asking for vetted e-commerce development studios.",
                        "target_audience": "DTC and retail founders",
                    },
                ],
                "seo_improvements": {
                    "primary_keyword": "custom web application development",
                    "secondary_keywords": [
                        "startup web development agency",
                        "e-commerce storefront engineering",
                        "UI/UX design systems",
                        "cloud architecture and DevOps",
                    ],
                    "long_tail_keywords": [
                        "fixed scope web development agency for startups",
                        "custom web application development studio Austin TX",
                        "technical SEO optimization for SaaS web apps",
                    ],
                    "relevant_business_keywords": [
                        "SaaS product engineering",
                        "scalable API development",
                        "SMB digital transformation",
                    ],
                    "recommended_title": "Custom Web Application Development Studio | NovaWeb Studio",
                    "title_structure_notes": "Places the primary keyword at the front, followed by brand name within 60 characters.",
                    "meta_description": "Launch scalable web apps, e-commerce stores, and cloud backends with NovaWeb Studio's fixed-scope sprints for startups and SMEs.",
                    "supporting_content": [
                        {
                            "content_type": "Service Landing Page",
                            "title": "Custom Web Application Development for Startups",
                            "purpose": "Capture high-intent service searches with architecture and sprint details.",
                            "target_keywords": ["custom web application development", "startup web agency"],
                        },
                        {
                            "content_type": "Location-Based Landing Page",
                            "title": "Web Development & Cloud Engineering Studio in Austin, TX",
                            "purpose": "Improve local organic visibility across Austin and Texas.",
                            "target_keywords": ["web development agency Austin TX"],
                        },
                        {
                            "content_type": "FAQ",
                            "title": "Fixed-Scope Web Development Pricing & Timeline FAQs",
                            "purpose": "Answer pre-sales objections and capture featured snippets.",
                            "target_keywords": ["fixed scope web development cost"],
                        },
                        {
                            "content_type": "Blog Topic",
                            "title": "How Startups Avoid Technical Debt When Scaling from MVP to Series A",
                            "purpose": "Attract early-stage founders researching scalable architecture.",
                            "target_keywords": ["scalable SaaS architecture", "startup MVP web app"],
                        },
                    ],
                },
                "top_services": [
                    {
                        "service_name": "Custom Web Application Development",
                        "description": "End-to-end engineering of scalable web apps and APIs.",
                        "target_industry": "SaaS & Technology",
                        "target_customer": "Startups and growing SMEs",
                        "main_benefit": "Accelerates product launch with predictable fixed-scope sprints.",
                        "source": "extracted_from_website",
                    },
                    {
                        "service_name": "E-Commerce Storefront Engineering",
                        "description": "High-performance online storefronts built for conversion.",
                        "target_industry": "Retail & E-Commerce",
                        "target_customer": "DTC brands and online retailers",
                        "main_benefit": "Improves page speed, checkout reliability, and sales.",
                        "source": "extracted_from_website",
                    },
                    {
                        "service_name": "UI/UX Design Systems",
                        "description": "Reusable design libraries and responsive interface engineering.",
                        "target_industry": "Software & Digital Products",
                        "target_customer": "Product teams and founders",
                        "main_benefit": "Ensures visual consistency and faster feature iteration.",
                        "source": "extracted_from_website",
                    },
                    {
                        "service_name": "Cloud Architecture & DevOps",
                        "description": "Automated deployment pipelines and resilient cloud infrastructure.",
                        "target_industry": "Technology & FinTech",
                        "target_customer": "Scaling engineering teams",
                        "main_benefit": "Reduces downtime and simplifies scaling under traffic growth.",
                        "source": "extracted_from_website",
                    },
                    {
                        "service_name": "Technical SEO & Performance Optimization",
                        "description": "Core Web Vitals, crawlability, and site speed optimization.",
                        "target_industry": "B2B Services & E-Commerce",
                        "target_customer": "Growth-focused SMEs",
                        "main_benefit": "Drives higher organic search rankings and better user retention.",
                        "source": "extracted_from_website",
                    },
                ],
                "target_audience": {
                    "primary_audience": ["Early-stage and growth-stage SaaS startups", "Small and medium businesses"],
                    "secondary_audience": ["DTC e-commerce brands", "Product managers needing UX modernization"],
                    "industries": ["SaaS & Technology", "Retail & E-Commerce", "Professional Services"],
                    "business_types": ["Startups", "Small Businesses", "Mid-Market Companies", "Online Retailers"],
                    "customer_profiles": [
                        {
                            "segment": "Seed-Stage SaaS Founders",
                            "business_type": "Startups",
                            "requirement": "MVP and scalable web application engineering",
                            "key_challenge": "Limited internal engineering bandwidth and tight launch timelines",
                        },
                        {
                            "segment": "Growing SMB Owners",
                            "business_type": "Small Businesses",
                            "requirement": "Predictable fixed-scope web modernization",
                            "key_challenge": "Unpredictable agency retainers and slow legacy websites",
                        },
                    ],
                    "geographical_focus": "Austin, Texas & United States",
                    "customer_needs": [
                        "Predictable delivery timelines without enterprise overhead",
                        "Fast, SEO-friendly, scalable web platforms",
                    ],
                    "customer_interests": [
                        "Modern web frameworks",
                        "Cloud reliability",
                        "Conversion rate optimization",
                    ],
                    "recommended_marketing_segments": [
                        "Austin-area tech founders on LinkedIn",
                        "DTC e-commerce operators seeking performance upgrades",
                    ],
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# 1. URL Validation & SSRF Protection Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "not-a-url",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://localhost:4000",
        "http://127.0.0.1/admin",
        "http://[::1]:8000",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://172.16.0.5/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://metadata.google.internal/",
        "http://user:pass@example.com",
        "http://internalservice/api",
        "http://printer.local/",
    ],
)
def test_ssrf_and_invalid_urls_rejected(bad_url: str) -> None:
    with pytest.raises(URLValidationError):
        normalize_url(bad_url)


def test_is_ip_public_checks_ipv4_and_ipv6() -> None:
    assert is_ip_public("93.184.216.34") is True
    assert is_ip_public("1.1.1.1") is True
    assert is_ip_public("127.0.0.1") is False
    assert is_ip_public("10.1.2.3") is False
    assert is_ip_public("169.254.169.254") is False
    assert is_ip_public("::1") is False
    assert is_ip_public("::ffff:127.0.0.1") is False


def test_dns_resolution_blocking_private_ip() -> None:
    fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.10.25", 443))]
    with patch("app.utils.validators.socket.getaddrinfo", return_value=fake_addr):
        with pytest.raises(URLValidationError, match="SSRF"):
            validate_public_url("https://rebind.example.com")


def test_dns_resolution_allowing_public_ip() -> None:
    fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("app.utils.validators.socket.getaddrinfo", return_value=fake_addr):
        result = validate_public_url("https://EXAMPLE.com/about/")
        assert result == "https://example.com/about/"


# ---------------------------------------------------------------------------
# 2. Website HTML Extraction Tests
# ---------------------------------------------------------------------------


def test_html_parser_extracts_signals_and_strips_noise() -> None:
    extractor = WebsiteExtractor(Settings())
    extracted = extractor.parse_html(
        requested_url="https://novawebstudio.example/",
        final_url="https://novawebstudio.example/",
        html=SAMPLE_HTML,
    )

    assert extracted.title == "NovaWeb Studio | Custom Web Development & Cloud Solutions"
    assert "Austin, Texas" in (extracted.meta_description or "")
    assert extracted.og_site_name == "NovaWeb Studio"
    assert "Modern Web Development for Growing Startups" in extracted.headings["h1"]
    assert "Services" in extracted.navigation_links
    assert "LinkedIn" in extracted.social_links
    assert len(extracted.structured_data) == 1
    assert extracted.structured_data[0]["@type"] == "Organization"
    assert "should be stripped" not in extracted.main_text
    assert "Custom Web Application Development" in extracted.main_text


# ---------------------------------------------------------------------------
# 3. Competitor Post-Processing & Schema Normalization Tests
# ---------------------------------------------------------------------------


def test_competitor_service_removes_self_and_verifies_mentions() -> None:
    extractor = WebsiteExtractor(Settings())
    extracted = extractor.parse_html(
        requested_url="https://novawebstudio.example/",
        final_url="https://novawebstudio.example/",
        html=SAMPLE_HTML,
    )
    payload = build_sample_llm_payload()

    # Check platform alias normalization ("Google" -> "Google Search", "X" -> "X (Twitter)")
    assert payload.analysis.target_search_queries[0].primary_platform == "Google Search"
    assert payload.analysis.target_search_queries[4].primary_platform == "X (Twitter)"

    refined = CompetitorService().refine(
        competitor_analysis=payload.analysis.competitor_analysis,
        brand=payload.brand,
        extracted=extracted,
    )

    names = [c.name for c in refined.competitors]
    assert "NovaWeb Studio" not in names
    assert len(refined.competitors) == 3

    by_name = {c.name: c for c in refined.competitors}
    assert by_name["GlobalMegaConsult"].source_type == "website_mentioned"
    assert by_name["Toptal"].source_type == "ai_inferred"
    assert by_name["Thoughtbot"].source_type == "ai_inferred"


# ---------------------------------------------------------------------------
# 4. LLM JSON Parsing Tests
# ---------------------------------------------------------------------------


def test_llm_parse_handles_markdown_fenced_json() -> None:
    payload = build_sample_llm_payload()
    fenced = f"```json\n{payload.model_dump_json()}\n```"
    parsed = LLMService._parse_and_validate(fenced)
    assert parsed.brand.name == "NovaWeb Studio"
    assert len(parsed.analysis.top_services) == 5


# ---------------------------------------------------------------------------
# 5. FastAPI Endpoint Integration Tests (with Mocked Network & LLM)
# ---------------------------------------------------------------------------


def test_health_and_root_endpoints() -> None:
    client = TestClient(app)
    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert root_resp.json()["status"] == "running"

    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"


def test_link_analysis_success_and_caching() -> None:
    settings = Settings(cache_enabled=True, cache_ttl_seconds=3600)
    extractor = WebsiteExtractor(settings)
    extracted = extractor.parse_html(
        requested_url="https://novawebstudio.example/",
        final_url="https://novawebstudio.example/",
        html=SAMPLE_HTML,
    )

    mock_extractor = AsyncMock(spec=WebsiteExtractor)
    mock_extractor.extract.return_value = extracted

    mock_llm = AsyncMock(spec=LLMService)
    mock_llm.generate_analysis.return_value = LLMResult(
        payload=build_sample_llm_payload(),
        provider="gemini",
        model="gemini-3.5-flash-lite",
        input_tokens=1650,
        output_tokens=1380,
        estimated_cost_usd=0.003945,
        latency_ms=1420,
    )

    service = AnalysisService(
        settings=settings,
        extractor=mock_extractor,
        llm_service=mock_llm,
    )

    app.dependency_overrides[get_analysis_service] = lambda: service
    try:
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.validators.socket.getaddrinfo", return_value=fake_addr):
            client = TestClient(app)

            # First call: executes extraction + LLM
            resp1 = client.post("/link-analysis", json={"url": "https://novawebstudio.example"})
            assert resp1.status_code == 200
            body1 = resp1.json()
            assert body1["success"] is True
            assert body1["brand"]["name"] == "NovaWeb Studio"
            assert len(body1["analysis"]["competitor_analysis"]["competitors"]) == 3
            assert len(body1["analysis"]["target_search_queries"]) == 5
            assert len(body1["analysis"]["top_services"]) == 5
            assert body1["analysis"]["seo_improvements"]["current_title"] == extracted.title
            assert body1["metadata"]["cached"] is False
            assert mock_llm.generate_analysis.await_count == 1

            # Second call: served from TTL cache without calling LLM again
            resp2 = client.post("/link-analysis", json={"url": "https://novawebstudio.example/"})
            assert resp2.status_code == 200
            assert resp2.json()["metadata"]["cached"] is True
            assert mock_llm.generate_analysis.await_count == 1

            # Third call with force_refresh=True: bypasses cache
            resp3 = client.post(
                "/link-analysis",
                json={"url": "https://novawebstudio.example", "force_refresh": True},
            )
            assert resp3.status_code == 200
            assert resp3.json()["metadata"]["cached"] is False
            assert mock_llm.generate_analysis.await_count == 2
    finally:
        app.dependency_overrides.clear()


def test_link_analysis_rejects_ssrf_url_with_422() -> None:
    client = TestClient(app)
    resp = client.post("/link-analysis", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert resp.status_code == 422
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "invalid_url"


def test_link_analysis_handles_inaccessible_site_gracefully() -> None:
    settings = Settings(cache_enabled=False)
    mock_extractor = AsyncMock(spec=WebsiteExtractor)
    mock_extractor.extract.side_effect = WebsiteExtractionError(
        "Website returned HTTP 403.", status_code=422
    )
    service = AnalysisService(settings=settings, extractor=mock_extractor, llm_service=AsyncMock())

    app.dependency_overrides[get_analysis_service] = lambda: service
    try:
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.validators.socket.getaddrinfo", return_value=fake_addr):
            client = TestClient(app)
            resp = client.post("/link-analysis", json={"url": "https://blocked-site.example"})
            assert resp.status_code == 422
            assert resp.json()["error"] == "website_extraction_failed"
    finally:
        app.dependency_overrides.clear()


def test_link_analysis_handles_missing_api_key_with_503() -> None:
    settings = Settings(
        cache_enabled=False,
        gemini_api_key=None,
        openai_api_key=None,
        anthropic_api_key=None,
    )
    mock_extractor = AsyncMock(spec=WebsiteExtractor)
    mock_extractor.extract.return_value = ExtractedWebsiteContent(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        domain="example.com",
        title="Example",
        main_text="Example business website text content for testing.",
        content_length_chars=50,
    )
    service = AnalysisService(
        settings=settings,
        extractor=mock_extractor,
        llm_service=LLMService(settings),
    )

    app.dependency_overrides[get_analysis_service] = lambda: service
    try:
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.validators.socket.getaddrinfo", return_value=fake_addr):
            client = TestClient(app)
            resp = client.post("/link-analysis", json={"url": "https://example.com"})
            assert resp.status_code == 503
            assert resp.json()["error"] == "llm_not_configured"
    finally:
        app.dependency_overrides.clear()
