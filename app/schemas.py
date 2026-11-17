"""Pydantic models: request, the 5 analysis sections, and the API response."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]

PLATFORM_ALIASES = {
    "google": "Google",
    "google search": "Google",
    "facebook": "Facebook",
    "fb": "Facebook",
    "instagram": "Instagram",
    "ig": "Instagram",
    "linkedin": "LinkedIn",
    "x": "X",
    "twitter": "X",
    "x (twitter)": "X",
    "x/twitter": "X",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- request
class LinkAnalysisRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=3,
        max_length=2048,
        description="Website to analyze (http/https). A missing scheme defaults to https.",
        examples=["https://example.com"],
    )

    @field_validator("url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


# ------------------------------------------------- Section 1: competitors
class Brand(_Model):
    name: str = ""
    category: str = ""
    summary: str = ""


class BrandProfile(_Model):
    target_market: str = ""
    main_service: str = ""
    key_strength: str = ""
    opportunity: str = ""


class Competitor(_Model):
    name: str
    website: str | None = None
    target_market: str = ""
    main_service: str = ""
    key_strength: str = ""
    weaknesses: list[str] = Field(default_factory=list)
    opportunity: str = ""
    source: Literal["ai_suggestion"] = "ai_suggestion"


class CompetitorAnalysis(_Model):
    brand_overview: str = ""
    business_category: str = ""
    main_products_services: list[str] = Field(default_factory=list)
    brand_profile: BrandProfile = Field(default_factory=BrandProfile)
    competitors: list[Competitor] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list, description="Your brand's competitive advantages")
    opportunities: list[str] = Field(default_factory=list, description="Ways to improve market positioning")


# ------------------------------------------------- Section 2: search queries
class SearchQuery(_Model):
    query: str
    platforms: list[Platform]
    intent: str = ""
    business_relevance: str = ""
    target_audience: str = ""

    @field_validator("platforms", mode="before")
    @classmethod
    def _normalize_platforms(cls, value: object) -> object:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            canonical = PLATFORM_ALIASES.get(str(item).strip().lower())
            if canonical and canonical not in cleaned:
                cleaned.append(canonical)
        return cleaned

    @field_validator("platforms")
    @classmethod
    def _need_one_platform(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one supported platform is required")
        return value


# ------------------------------------------------- Section 3: SEO
class ContentIdea(_Model):
    type: str = ""
    title: str = ""
    purpose: str = ""


class SeoImprovements(_Model):
    primary_keyword: str = ""
    secondary_keywords: list[str] = Field(default_factory=list)
    long_tail_keywords: list[str] = Field(default_factory=list)
    business_keywords: list[str] = Field(default_factory=list)
    recommended_title: str = ""
    title_structure: str = ""
    meta_description: str = ""
    supporting_content: list[ContentIdea] = Field(default_factory=list)
    # Filled from the real page (not by the LLM), so the improvement can be compared with reality.
    current_title: str = ""
    current_meta_description: str = ""


# ------------------------------------------------- Section 4: services
class Service(_Model):
    name: str
    description: str = ""
    target_industry: str = ""
    target_customer: str = ""
    main_benefit: str = ""
    source: Literal["website", "inferred"] = "website"


# ------------------------------------------------- Section 5: audience
class AudienceSegment(_Model):
    audience: str = ""
    business_type: str = ""
    requirement: str = ""


class TargetAudience(_Model):
    primary_audience: list[str] = Field(default_factory=list)
    secondary_audience: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    business_types: list[str] = Field(default_factory=list)
    customer_profiles: list[str] = Field(default_factory=list)
    geographic_focus: str = ""
    customer_needs: list[str] = Field(default_factory=list)
    customer_interests: list[str] = Field(default_factory=list)
    recommended_marketing_segments: list[AudienceSegment] = Field(default_factory=list)


# ------------------------------------------------- combined
class Analysis(_Model):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SeoImprovements
    top_services: list[Service]
    target_audience: TargetAudience

    @field_validator("target_search_queries")
    @classmethod
    def _exactly_five_queries(cls, value: list[SearchQuery]) -> list[SearchQuery]:
        if len(value) < 5:
            raise ValueError(f"expected 5 search queries, got {len(value)}")
        return value[:5]

    @field_validator("top_services")
    @classmethod
    def _exactly_five_services(cls, value: list[Service]) -> list[Service]:
        if len(value) < 5:
            raise ValueError(f"expected 5 services, got {len(value)}")
        return value[:5]


class LLMOutput(Analysis):
    """What the LLM must return: the brand block plus the five sections."""

    brand: Brand


class Meta(BaseModel):
    llm_provider: str
    llm_model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    llm_latency_ms: int | None = None
    total_latency_ms: int = 0
    cached: bool = False
    content_chars: int = 0
    content_truncated: bool = False


class LinkAnalysisResponse(BaseModel):
    success: bool = True
    website: str
    brand: Brand
    analysis: Analysis
    notes: list[str] = Field(default_factory=list)
    meta: Meta
