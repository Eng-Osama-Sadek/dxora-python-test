"""Offline tests: no network and no API keys. The LLM and the HTTP fetch are replaced by fakes."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import Competitor
from app.services import analysis_service
from app.services.competitor_service import normalize_competitors
from app.services.llm_service import LLMError, LLMResult, parse_json_text
from app.services.website_extractor import WebsiteFetchError, extract_site, fetch_html
from app.utils import validators
from app.utils.validators import InvalidURLError, UnsafeURLError, normalize_url, validate_url

client = TestClient(app)

HTML = """<html lang="en"><head><title>Acme Plumbing | Fast Leak Repair</title>
<meta name="description" content="Emergency plumbing in Austin.">
<meta property="og:site_name" content="Acme Plumbing">
<script type="application/ld+json">{"@type":"Organization","name":"Acme Plumbing Co"}</script>
<script>var secret = "SCRIPT-TEXT";</script></head>
<body><nav><a href="/a">Services</a><a href="/b">Contact</a></nav>
<h1>Emergency plumbing in Austin</h1>
<p>We repair leaking pipes, install water heaters and unclog drains for homes and small businesses.</p>
<ul><li>Leak detection and repair services</li><li>Water heater installation</li></ul>
<p>We repair leaking pipes, install water heaters and unclog drains for homes and small businesses.</p>
<footer>Footer legal text that should be dropped from the content.</footer></body></html>"""


def valid_llm_json() -> dict:
    return {
        "brand": {"name": "Acme Plumbing", "category": "Plumbing services", "summary": "Austin plumbers."},
        "competitor_analysis": {
            "brand_overview": "Local plumbing company.",
            "business_category": "Home services",
            "main_products_services": ["Leak repair"],
            "brand_profile": {"target_market": "Homes", "main_service": "Repairs", "key_strength": "Speed", "opportunity": "Reviews"},
            "competitors": [
                {"name": "Acme Plumbing", "website": "acme-plumbing.com", "weaknesses": ["x"]},
                {"name": "Roto-Rooter", "website": "https://www.rotorooter.com/austin", "weaknesses": ["Pricey"]},
                {"name": "Mister Sparky", "website": "not a domain", "weaknesses": []},
                {"name": "roto-rooter", "website": None, "weaknesses": []},
                {"name": "ARS", "website": "ars.com", "weaknesses": []},
            ],
            "strengths": ["Fast response"],
            "opportunities": ["Add FAQs"],
        },
        "target_search_queries": [
            {"query": f"plumber austin {i}", "platforms": ["Google Search", "Twitter"], "intent": "Service", "business_relevance": "High", "target_audience": "Homeowners"}
            for i in range(6)
        ],
        "seo_improvements": {
            "primary_keyword": "austin plumber",
            "secondary_keywords": ["leak repair"],
            "long_tail_keywords": ["emergency plumber in austin tx"],
            "business_keywords": ["water heater"],
            "recommended_title": "Austin Plumber | Emergency Leak Repair | Acme Plumbing",
            "title_structure": "Keyword | benefit | brand",
            "meta_description": "Fast, licensed plumbers in Austin for leaks, heaters and drains.",
            "supporting_content": [{"type": "FAQ", "title": "How much does leak repair cost?", "purpose": "Capture questions"}],
        },
        "top_services": [
            {"name": f"Service {i}", "description": "d", "target_industry": "Homes", "target_customer": "Owners", "main_benefit": "b", "source": "website" if i < 3 else "inferred"}
            for i in range(5)
        ],
        "target_audience": {
            "primary_audience": ["Homeowners"], "secondary_audience": ["Landlords"], "industries": ["Real estate"],
            "business_types": ["Small business"], "customer_profiles": ["Busy families"], "geographic_focus": "Austin, TX",
            "customer_needs": ["Fast repairs"], "customer_interests": ["Home improvement"],
            "recommended_marketing_segments": [{"audience": "Landlords", "business_type": "Property mgmt", "requirement": "Maintenance contracts"}],
        },
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "model-a")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "model-b")
    analysis_service.clear_cache()
    monkeypatch.setattr(analysis_service, "RETRY_DELAY_SECONDS", 0)

    async def public_dns(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(validators, "resolve_host", public_dns)

    async def fake_fetch(url, **kwargs):
        return HTML, url

    monkeypatch.setattr("app.services.website_extractor.fetch_html", fake_fetch)


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def __call__(self, system, user, settings, *, model, client=None):
        self.calls.append({"system": system, "user": user, "model": model})
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResult(text=text, provider="gemini", model=model, input_tokens=1200, output_tokens=900, latency_ms=1500)


def use_llm(monkeypatch, *outputs) -> FakeLLM:
    fake = FakeLLM(outputs)
    monkeypatch.setattr(analysis_service, "generate_text", fake)
    return fake


# ------------------------------------------------------------------ URL validation / SSRF
def test_normalize_adds_https_and_strips_fragment():
    assert normalize_url(" Example.com/path#frag ") == "https://example.com/path"


@pytest.mark.parametrize("bad", ["", "   ", "ftp://example.com", "file:///etc/passwd", "javascript:alert(1)",
                                 "https://user:pw@example.com", "https://exa mple.com", "https://example.com:8080"])
def test_rejects_malformed_or_dangerous_urls(bad):
    with pytest.raises((InvalidURLError, UnsafeURLError)):
        normalize_url(bad)


@pytest.mark.parametrize("target", ["http://127.0.0.1", "http://10.0.0.5", "http://192.168.1.1", "http://172.16.0.1",
                                    "http://169.254.169.254/latest/meta-data", "http://0.0.0.0", "http://[::1]",
                                    "http://100.64.0.1", "http://[::ffff:127.0.0.1]", "http://localhost"])
def test_blocks_private_and_internal_targets(target):
    with pytest.raises((InvalidURLError, UnsafeURLError)):
        asyncio.run(validate_url(target))


def test_blocks_domain_resolving_to_private_ip(monkeypatch):
    async def private_dns(host):
        return ["10.1.2.3"]

    monkeypatch.setattr(validators, "resolve_host", private_dns)
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("https://evil.example.com"))


def test_blocks_domain_with_mixed_public_and_private_ips(monkeypatch):
    async def mixed_dns(host):
        return ["93.184.216.34", "127.0.0.1"]

    monkeypatch.setattr(validators, "resolve_host", mixed_dns)
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("https://rebind.example.com"))


def test_unresolvable_domain_is_invalid(monkeypatch):
    async def broken_dns(host):
        raise InvalidURLError("Could not resolve domain")

    monkeypatch.setattr(validators, "resolve_host", broken_dns)
    with pytest.raises(InvalidURLError):
        asyncio.run(validate_url("https://does-not-exist.example"))


# ------------------------------------------------------------------ fetching / extraction
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_blocks_redirect_to_internal_address():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async def run():
        async with _client(handler) as c:
            await fetch_html("https://example.com/", client=c)

    with pytest.raises((InvalidURLError, UnsafeURLError)):
        asyncio.run(run())


def test_fetch_follows_safe_redirect_and_returns_final_url():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/home"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>hello</p>")

    async def run():
        async with _client(handler) as c:
            return await fetch_html("https://example.com/", client=c)

    html, final_url = asyncio.run(run())
    assert "hello" in html and final_url == "https://example.com/home"


@pytest.mark.parametrize("status,expected", [(403, 422), (404, 422), (500, 502)])
def test_fetch_maps_http_errors(status, expected):
    async def run():
        async with _client(lambda r: httpx.Response(status)) as c:
            await fetch_html("https://example.com/", client=c)

    with pytest.raises(WebsiteFetchError) as exc:
        asyncio.run(run())
    assert exc.value.status_code == expected


def test_fetch_rejects_non_html():
    async def run():
        async with _client(lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")) as c:
            await fetch_html("https://example.com/", client=c)

    with pytest.raises(WebsiteFetchError):
        asyncio.run(run())


def test_fetch_timeout_is_504():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async def run():
        async with _client(handler) as c:
            await fetch_html("https://example.com/", client=c)

    with pytest.raises(WebsiteFetchError) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 504


def test_extract_site_keeps_signal_and_drops_noise():
    site = extract_site(HTML, url="https://acme.test/")
    assert site.title.startswith("Acme Plumbing")
    assert site.meta_description == "Emergency plumbing in Austin."
    assert site.site_name == "Acme Plumbing"
    assert site.nav_links == ["Services", "Contact"]
    assert "Emergency plumbing in Austin" in site.headings
    assert "SCRIPT-TEXT" not in site.text and "Footer legal" not in site.text
    assert site.text.lower().count("we repair leaking pipes") == 1  # duplicates removed


def test_extract_site_truncates_long_pages():
    long_html = "<body>" + "".join(f"<p>Unique paragraph number {i} with enough words to be kept.</p>" for i in range(200)) + "</body>"
    site = extract_site(long_html, url="https://x.test/", max_chars=500)
    assert site.truncated and len(site.text) <= 500


def test_extract_site_without_text_raises():
    with pytest.raises(WebsiteFetchError) as exc:
        extract_site("<html><body><div id='root'></div><script>app()</script></body></html>", url="https://spa.test/")
    assert exc.value.status_code == 422


# ------------------------------------------------------------------ LLM helpers
def test_parse_json_text_handles_fences_and_prose():
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_text("not json at all")
    with pytest.raises(ValueError):
        parse_json_text("[1, 2, 3]")


def test_competitors_are_cleaned():
    raw = [Competitor(**c) for c in valid_llm_json()["competitor_analysis"]["competitors"]]
    cleaned = normalize_competitors(raw, brand_name="Acme Plumbing", site_url="https://acme-plumbing.com/")
    assert [c.name for c in cleaned] == ["Roto-Rooter", "Mister Sparky", "ARS"]  # self + duplicate removed
    assert cleaned[0].website == "https://rotorooter.com"
    assert cleaned[1].website is None  # invalid domain dropped, not guessed
    assert all(c.source == "ai_suggestion" for c in cleaned)


# ------------------------------------------------------------------ API behaviour
def test_successful_analysis_has_all_five_sections(monkeypatch):
    use_llm(monkeypatch, valid_llm_json())
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True and body["website"] == "https://acme.test/"
    assert set(body["analysis"]) == {"competitor_analysis", "target_search_queries", "seo_improvements", "top_services", "target_audience"}
    assert body["brand"]["name"] == "Acme Plumbing"
    assert len(body["analysis"]["target_search_queries"]) == 5
    assert len(body["analysis"]["top_services"]) == 5
    assert body["analysis"]["target_search_queries"][0]["platforms"] == ["Google", "X"]  # aliases normalized
    assert [c["name"] for c in body["analysis"]["competitor_analysis"]["competitors"]][:2] == ["Roto-Rooter", "Mister Sparky"]
    seo = body["analysis"]["seo_improvements"]
    assert seo["current_title"].startswith("Acme Plumbing |") and len(seo["recommended_title"]) <= 65
    assert body["meta"]["llm_model"] == "model-a" and body["meta"]["cached"] is False
    assert any("Search volumes are not included" in n for n in body["notes"])


def test_prompt_isolates_untrusted_page_content(monkeypatch):
    fake = use_llm(monkeypatch, valid_llm_json())
    client.post("/link-analysis", json={"url": "https://acme.test"})
    assert "<website_content>" in fake.calls[0]["user"] and "Emergency plumbing in Austin" in fake.calls[0]["user"]
    assert "untrusted" in fake.calls[0]["system"].lower()


def test_second_request_is_served_from_cache(monkeypatch):
    fake = use_llm(monkeypatch, valid_llm_json())
    first = client.post("/link-analysis", json={"url": "https://acme.test"}).json()
    second = client.post("/link-analysis", json={"url": "https://acme.test"}).json()
    assert len(fake.calls) == 1
    assert first["meta"]["cached"] is False and second["meta"]["cached"] is True


def test_invalid_json_is_retried_once(monkeypatch):
    fake = use_llm(monkeypatch, "sorry, no json", valid_llm_json())
    assert client.post("/link-analysis", json={"url": "https://acme.test"}).status_code == 200
    assert len(fake.calls) == 2 and fake.calls[1]["model"] == "model-a"


def test_incomplete_analysis_returns_502(monkeypatch):
    bad = valid_llm_json()
    bad["target_search_queries"] = bad["target_search_queries"][:2]
    use_llm(monkeypatch, bad)
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 502 and response.json()["success"] is False


def test_fallback_model_is_used_when_primary_is_rate_limited(monkeypatch):
    fake = use_llm(monkeypatch, LLMError("rate limit", status_code=503, retryable=True), valid_llm_json())
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 200
    assert [c["model"] for c in fake.calls] == ["model-a", "model-b"]
    assert response.json()["meta"]["llm_model"] == "model-b"


def test_non_retryable_llm_error_is_returned(monkeypatch):
    use_llm(monkeypatch, LLMError("bad key", status_code=502, retryable=False))
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 502 and "bad key" in response.json()["details"][0]["message"]


def test_missing_api_key_returns_503(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 503 and "GEMINI_API_KEY" in response.json()["details"][0]["message"]


def test_invalid_url_returns_422_even_without_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    response = client.post("/link-analysis", json={"url": "not a url"})
    assert response.status_code == 422 and response.json()["error"] == "Invalid URL"


def test_private_url_returns_400():
    response = client.post("/link-analysis", json={"url": "http://169.254.169.254/latest/meta-data"})
    assert response.status_code == 400 and response.json()["error"] == "URL not allowed"


def test_missing_body_field_returns_validation_envelope():
    response = client.post("/link-analysis", json={})
    assert response.status_code == 422
    assert response.json() == {"success": False, "error": "Validation failed",
                               "details": [{"field": "url", "message": "Field required"}]}


def test_unreachable_website_status_is_passed_through(monkeypatch):
    async def failing_fetch(url, **kwargs):
        raise WebsiteFetchError("Could not connect to the website.", 502)

    monkeypatch.setattr("app.services.website_extractor.fetch_html", failing_fetch)
    use_llm(monkeypatch, valid_llm_json())
    response = client.post("/link-analysis", json={"url": "https://down.test"})
    assert response.status_code == 502 and response.json()["error"] == "Website could not be analyzed"


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_transient_provider_error_is_retried_on_the_same_model(monkeypatch):
    transient = LLMError("Gemini is temporarily unavailable (HTTP 503).", retryable=True, upstream_status=503)
    fake = use_llm(monkeypatch, transient, valid_llm_json())
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 200
    assert [c["model"] for c in fake.calls] == ["model-a", "model-a"]
    assert response.json()["meta"]["llm_model"] == "model-a"


def test_persistent_provider_error_falls_back_then_fails(monkeypatch):
    transient = LLMError("Gemini is temporarily unavailable (HTTP 503).", retryable=True, upstream_status=503)
    fake = use_llm(monkeypatch, transient)
    response = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert response.status_code == 502
    assert [c["model"] for c in fake.calls] == ["model-a", "model-a", "model-b", "model-b"]


def test_default_models_per_provider(monkeypatch):
    from app.config import get_settings

    for name in ("LLM_MODEL", "LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert (settings.llm_model, settings.llm_fallback_model) == ("gemini-3.5-flash-lite", "gemini-3.8-flash")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert get_settings().llm_model == "claude-haiku-5-5"
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "none")
    assert get_settings().llm_fallback_model == ""


def test_root_redirects_to_docs():
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307) and response.headers["location"] == "/docs"
