"""Competitor post-processing, verification, and comparison matrix normalization."""

from __future__ import annotations

import re

from app.schemas import (
    BrandComparisonRow,
    BrandInfo,
    CompetitorAnalysis,
    CompetitorItem,
    ExtractedWebsiteContent,
)


class CompetitorService:
    """Ensures competitor outputs are deduplicated, transparently labeled, and structured."""

    def refine(
        self,
        competitor_analysis: CompetitorAnalysis,
        brand: BrandInfo,
        extracted: ExtractedWebsiteContent,
    ) -> CompetitorAnalysis:
        """Normalize competitor list, verify source attribution, and guarantee comparison rows."""
        brand_name_norm = self._normalize_name(brand.name)
        domain_root = extracted.domain.removeprefix("www.").split(".")[0].lower()
        searchable_text = " ".join(
            filter(
                None,
                [
                    extracted.title,
                    extracted.meta_description,
                    extracted.main_text,
                ],
            )
        ).lower()

        cleaned_competitors: list[CompetitorItem] = []
        seen_names: set[str] = set()

        for comp in competitor_analysis.competitors:
            norm = self._normalize_name(comp.name)
            if not norm:
                continue
            # Prevent the analyzed brand itself from appearing in its own competitor list
            if norm == brand_name_norm or (domain_root and len(domain_root) > 3 and norm == domain_root):
                continue
            if norm in seen_names:
                continue
            seen_names.add(norm)

            # Verify whether the competitor is genuinely mentioned on the website
            mentioned_on_site = bool(
                re.search(rf"\b{re.escape(comp.name.strip().lower())}\b", searchable_text)
            )
            verified_source = "website_mentioned" if mentioned_on_site else "ai_inferred"

            cleaned_competitors.append(
                comp.model_copy(update={"source_type": verified_source})
            )

        comparison_table = competitor_analysis.comparison_table
        if not comparison_table:
            comparison_table = self._build_default_comparison_table(
                brand=brand,
                competitor_analysis=competitor_analysis,
                competitors=cleaned_competitors,
            )

        return competitor_analysis.model_copy(
            update={
                "brand_name": competitor_analysis.brand_name or brand.name,
                "main_business_category": competitor_analysis.main_business_category or brand.category,
                "main_products_and_services": (
                    competitor_analysis.main_products_and_services or brand.main_products_and_services
                ),
                "competitors": cleaned_competitors,
                "comparison_table": comparison_table,
            }
        )

    @staticmethod
    def _build_default_comparison_table(
        brand: BrandInfo,
        competitor_analysis: CompetitorAnalysis,
        competitors: list[CompetitorItem],
    ) -> list[BrandComparisonRow]:
        lead_comp = competitors[0] if competitors else None
        return [
            BrandComparisonRow(
                dimension="Brand Name",
                your_brand=brand.name,
                competitor_benchmark=lead_comp.name if lead_comp else "Category Competitors",
            ),
            BrandComparisonRow(
                dimension="Main Service",
                your_brand=(
                    brand.main_products_and_services[0]
                    if brand.main_products_and_services
                    else brand.category
                ),
                competitor_benchmark=lead_comp.main_service if lead_comp else brand.category,
            ),
            BrandComparisonRow(
                dimension="Target Market",
                your_brand=brand.category,
                competitor_benchmark=lead_comp.target_market if lead_comp else "Broader Market Segment",
            ),
            BrandComparisonRow(
                dimension="Key Strength",
                your_brand=(
                    competitor_analysis.strengths[0]
                    if competitor_analysis.strengths
                    else "Specialized offering"
                ),
                competitor_benchmark=(
                    lead_comp.strengths[0]
                    if lead_comp and lead_comp.strengths
                    else "Established market presence"
                ),
            ),
            BrandComparisonRow(
                dimension="Opportunity",
                your_brand=(
                    competitor_analysis.opportunities[0]
                    if competitor_analysis.opportunities
                    else "Strengthen organic visibility and positioning"
                ),
                competitor_benchmark=(
                    lead_comp.weaknesses_or_gaps[0]
                    if lead_comp and lead_comp.weaknesses_or_gaps
                    else "Address underserved customer segments"
                ),
            ),
        ]

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", name.lower()).strip()
