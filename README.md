# AI-Powered Website Link Analysis API

A FastAPI service that takes a website URL, fetches and cleans the page, sends the content to an LLM and returns
**five structured sections**: competitors, top 5 target search queries, SEO improvements, top 5 services and
target audience.

- **Stack:** Python 3.10+, FastAPI, Uvicorn, Pydantic v2, HTTPX, BeautifulSoup, python-dotenv
- **Endpoint:** `POST http://localhost:4000/link-analysis`
- **Docs (Swagger UI):** http://localhost:4000/docs
- **LLM:** Google Gemini by default (has a **free tier**), also supports OpenAI (and OpenAI-compatible APIs) and Anthropic Claude, chosen in `.env`
- **No mock mode.** Every result comes from a real page fetch and a real LLM call. If something fails, the API says so.

## Setup

```bash
git clone <this-repo-url>
cd <repo-folder>

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# edit .env and paste your key (see below)
```

**Free key (no credit card):** create a Gemini API key at https://aistudio.google.com/apikey and set
`GEMINI_API_KEY` in `.env`. Free-tier content may be used by Google to improve its products, so do not send
confidential pages with a free key.

To use another provider set `LLM_PROVIDER=openai` (+ `OPENAI_API_KEY`) or `LLM_PROVIDER=anthropic`
(+ `ANTHROPIC_API_KEY`). `LLM_MODEL` / `LLM_FALLBACK_MODEL` override the default models.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

## API

### `POST /link-analysis`

```bash
curl -X POST http://localhost:4000/link-analysis \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

A URL without a scheme (`example.com`) is accepted and defaults to `https://`.

**Response `200 OK`** (shape; values depend on the real website and the LLM):

```json
{
  "success": true,
  "website": "https://example.com/",
  "brand": { "name": "", "category": "", "summary": "" },
  "analysis": {
    "competitor_analysis": {
      "brand_overview": "",
      "business_category": "",
      "main_products_services": [],
      "brand_profile": { "target_market": "", "main_service": "", "key_strength": "", "opportunity": "" },
      "competitors": [
        { "name": "", "website": null, "target_market": "", "main_service": "", "key_strength": "",
          "weaknesses": [], "opportunity": "", "source": "ai_suggestion" }
      ],
      "strengths": [],
      "opportunities": []
    },
    "target_search_queries": [
      { "query": "", "platforms": ["Google", "LinkedIn"], "intent": "", "business_relevance": "", "target_audience": "" }
    ],
    "seo_improvements": {
      "primary_keyword": "", "secondary_keywords": [], "long_tail_keywords": [], "business_keywords": [],
      "recommended_title": "", "title_structure": "", "meta_description": "",
      "supporting_content": [{ "type": "FAQ", "title": "", "purpose": "" }],
      "current_title": "", "current_meta_description": ""
    },
    "top_services": [
      { "name": "", "description": "", "target_industry": "", "target_customer": "", "main_benefit": "", "source": "website" }
    ],
    "target_audience": {
      "primary_audience": [], "secondary_audience": [], "industries": [], "business_types": [],
      "customer_profiles": [], "geographic_focus": "", "customer_needs": [], "customer_interests": [],
      "recommended_marketing_segments": [{ "audience": "", "business_type": "", "requirement": "" }]
    }
  },
  "notes": ["Competitors are AI-generated suggestions ...", "Search volumes are not included ..."],
  "meta": { "llm_provider": "gemini", "llm_model": "gemini-3.8-flash", "input_tokens": 0, "output_tokens": 0,
            "llm_latency_ms": 0, "total_latency_ms": 0, "cached": false, "content_chars": 0, "content_truncated": false }
}
```

> The block above only documents the response *shape*. After running the project with your own key, paste a
> real response and screenshots into `docs/screenshots/` (see "Evidence of a real run" below).

How the spec's honesty rules are handled:

| Requirement | Implementation |
|---|---|
| Use the actual website content | Only the extracted text of the submitted page is sent; the prompt forbids generic filler |
| Mark inferred services | Every service has `source: "website"` or `"inferred"` |
| Competitors = credible research or clearly marked suggestion | Every competitor has `source: "ai_suggestion"`, domains are only kept when they look valid, and a note explains that no live research was done |
| Do not invent search volumes | No volume fields exist; the prompt forbids statistics; a note states this in every response |
| Distinguish measured from estimated | `meta` returns the measured token usage and latency of each call (see LLM section) |

### Errors

All errors use the same envelope: `{"success": false, "error": "...", "details": [{"field": "...", "message": "..."}]}`.

| Status | When |
|---|---|
| 400 | URL points to a private/internal address (SSRF protection) |
| 422 | Invalid URL; website blocked us (401/403/429), page not found, not HTML, or no readable text (JS-only page) |
| 502 | Website returned 5xx / unreachable, or the LLM failed / returned an unusable answer twice |
| 503 | LLM not configured (missing key) or rate limited |
| 504 | Website or LLM timed out |

## Security

- **SSRF protection** (`app/utils/validators.py`): only `http(s)` on ports 80/443, no credentials in the URL, and the host
  must resolve **only to public IPs** (loopback, private, link-local such as `169.254.169.254`, CGNAT, IPv4-mapped IPv6,
  etc. are rejected). Redirects are followed manually and **every hop is re-validated**.
- Download limits: 2 MB of HTML, timeouts, content-type check.
- Prompt-injection hardening: page text is wrapped in `<website_content>` tags and the system prompt tells the model it is
  untrusted data. Output is parsed and validated with Pydantic before being returned.
- API keys are read only from environment variables; `.env` is git-ignored; keys are sent in headers, never in URLs or logs.
- **Known limitation:** the IP check happens before the connection, so a DNS-rebinding attack between the check and the
  connection is theoretically possible. For production, pin the connection to the validated IP or run the service behind an
  egress proxy / firewall that blocks internal ranges.

## Tests

The tests run offline (no network, no API keys); the LLM and HTTP fetch are replaced by fakes:

```bash
python -m pytest -v
```

They cover URL/SSRF validation, redirect re-validation, HTTP error mapping, HTML extraction, JSON parsing, competitor
cleanup, retry and fallback logic, caching and every error status.

## Project structure

```
app/
├── main.py                       FastAPI app, POST /link-analysis, error handlers
├── config.py                     Environment variables / .env, default models
├── schemas.py                    Pydantic request / response models (the 5 sections)
├── services/
│   ├── website_extractor.py      Safe fetch + HTML cleaning
│   ├── llm_service.py            Gemini / OpenAI / Anthropic HTTP clients, JSON parsing
│   ├── analysis_service.py       Pipeline, prompts, retry + fallback, caching
│   └── competitor_service.py     Cleans and labels the competitor list
└── utils/
    ├── validators.py             URL validation + SSRF protection
    └── cache.py                  In-memory TTL cache
tests/test_link_analysis.py
```

## Deploy on Vercel (optional)

Import the repo in Vercel (FastAPI / Other), add the variables from `.env.example` in
**Project → Settings → Environment Variables**, and redeploy. `vercel.json` raises the function limit to 60 s.
The in-memory cache is per instance there.

---

# LLM platform and model research

> **Prices were checked on 17 Nov 2026.** Provider pricing changes often (several Gemini prices below double on
> 1 Jan 2027), so re-check before budgeting. **Accuracy and speed were not benchmarked by me** (see "Measured vs
> estimated"), so no model is claimed to be "the most accurate".

## A. Comparison

Typical call for this app (see section D): about **6,000 input + 2,500 output tokens**.

| Provider | Model (API id) | Input / Output per 1M tokens | Est. cost per analysis | Free tier | JSON output | Context window |
|---|---|---|---|---|---|---|
| Google | `gemini-3.8-flash` | $0.75 / $3.75 until 31 Dec 2026, then $1.50 / $7.50 | ~$0.014 (then ~$0.028) | **Yes** | Native JSON mode (`responseMimeType`) | Not confirmed on the pricing page; far above what this app sends |
| Google | `gemini-3.5-flash-lite` | $0.30 / $2.50 | ~$0.008 | **Yes** | Native JSON mode | Not confirmed on the pricing page |
| Anthropic | `claude-haiku-5-5` | $0.10 / $0.50 for prompts up to 100k tokens ($0.50 / $2.50 above) | ~$0.002 | No | Prompt-enforced JSON + parser/validation retry | 1M |
| Anthropic | `claude-sonnet-5-5` | $2 / $10 | ~$0.037 | No | Prompt-enforced JSON + retry | Not re-verified |
| OpenAI | `gpt-5.4-nano` | $0.20 / $1.25 | ~$0.004 | No | `response_format: json_object` | ~400K (third-party listing) |
| OpenAI | `gpt-5.4-mini` | $0.75 input (official page snippet); output rate not re-verified | n/a | No | `response_format: json_object` | Not re-verified |

Sources: Google rows from the official Gemini API pricing page (ai.google.dev). Claude Haiku/Sonnet rates from Anthropic's
launch announcements as reported by several outlets and price trackers (I could not open Anthropic's own page).
OpenAI nano rates from third-party price trackers; the mini input price is from openai.com's pricing page.
Cost = `6000 × input + 2500 × output`; thinking tokens, if any, are billed as output, so the real figure can be higher.

| Criteria | Notes |
|---|---|
| Response speed | **Not measured here.** Expect roughly 5-30 s per analysis because the answer is ~2,500 tokens of JSON; small "flash / lite / nano / haiku" models are the fastest options. The API returns the measured `llm_latency_ms` of each call so you can fill the table below |
| Accuracy | **Not benchmarked.** One third-party test reported that Haiku 5.5 used about 3x more output tokens per task than GPT-6 Luna, which would raise its real cost above the estimate |
| JSON reliability | Gemini and OpenAI offer a JSON mode; Claude relies on the prompt. In all cases the app parses and validates the result with Pydantic and retries once, then falls back to a second model |

## B. Model selection

Priority for this project: low cost, reliable JSON, good understanding of a page's content.

- **Default: `gemini-3.8-flash`.** It is the strongest Flash-class model with a **free tier**, so development, testing and
  the demo cost nothing, and its paid price is still small (cents per analysis).
- **Automatic fallback: `gemini-3.5-flash-lite`** (also free-tier). It is used when the main model is rate limited, down or
  returns an invalid answer twice.
- **Cheapest paid option at scale: `claude-haiku-5-5`** (about $0.002 per analysis on these assumptions). Switch with
  `LLM_PROVIDER=anthropic`. If the quality of its competitor and SEO sections is not good enough in your own test,
  use `claude-sonnet-5-5` as the stronger paid model.

## C. Cost optimization (implemented unless noted)

1. **Clean before sending:** scripts, styles, SVG, forms, nav, footer are removed; only headings, paragraphs and list items
   are kept; duplicate lines are dropped (`website_extractor.py`).
2. **Hard cap on input:** `MAX_CONTENT_CHARS` (default 12,000 characters ≈ 3,000 tokens) and a 2 MB download limit.
3. **Short prompt, strict schema:** one compact JSON template, "keep every string concise", exactly 5 queries / 5 services.
4. **Single LLM call** produces all five sections (no per-section calls).
5. **Cheap model first, stronger model only as fallback**, never the other way round.
6. **Cache:** the same URL + model is answered from an in-memory TTL cache (`CACHE_TTL_SECONDS`, default 1 hour).
7. **No wasted retries:** only invalid JSON (1 retry per model) and retryable HTTP errors (429/5xx/timeouts) trigger another call.
8. **Fail before spending:** invalid URLs, SSRF, missing key and unreachable/empty sites are rejected before any LLM call.
9. *Possible next steps:* provider batch APIs (about half price) for bulk jobs, prompt caching for the fixed system prompt,
   and a persistent cache (Redis) in production.

## D. Final recommendation

| Item | Value |
|---|---|
| Recommended provider / model | **Google Gemini / `gemini-3.8-flash`** (free tier for development and demo) |
| Fallback model | `gemini-3.5-flash-lite` (automatic); alternative provider: Anthropic `claude-haiku-5-5` |
| Estimated input tokens | ~6,000 (≈3,000 page text + ≈1,300 system prompt/schema + page metadata and overhead) |
| Estimated output tokens | ~2,500 (five sections as JSON; more if the model "thinks") |
| Estimated cost per analysis | **$0 on the free tier**; ~$0.014 paid until 31 Dec 2026, ~$0.028 after that (Gemini 3.8 Flash) |
| Expected response time | Roughly 5-30 s (**estimate, not measured**) |
| Reason | Free to develop and demo, native JSON mode, small paid price, and a second free-tier model as fallback |
| Balancing accuracy, speed, cost | Cheap, fast model by default; validate output strictly; retry once; fall back to a second model; cache repeats; upgrade to a larger model only if your own evaluation shows the cheap one is not good enough |

### Measured vs estimated

- **Estimated:** token counts, cost per analysis, expected latency.
- **Measured (by the API itself):** every response contains `meta.input_tokens`, `meta.output_tokens`, `meta.llm_latency_ms`
  and `meta.total_latency_ms` for that call.
- **Not done:** an accuracy benchmark. To compare models fairly, run the same 5-10 websites with each
  `LLM_MODEL`, and score services/competitors/queries by hand.

Fill this table after running the project with your key:

| Website | Model | Input tokens | Output tokens | LLM latency (ms) | Notes |
|---|---|---|---|---|---|
| _your test 1_ | gemini-3.8-flash | | | | |
| _your test 2_ | gemini-3.5-flash-lite | | | | |

## Evidence of a real run

Add screenshots / logs here after your own run (Swagger UI, a real `POST /link-analysis` response, the passing tests):

<!-- ![Swagger UI](docs/screenshots/swagger.png)
![Real response](docs/screenshots/response.png)
![Tests](docs/screenshots/tests.png) -->

## Limitations

- Only the submitted page is analyzed (no site crawl); JavaScript-only pages without server-rendered text are rejected.
- Competitors come from the model's knowledge, not live research, and may be outdated.
- No search-volume data (it would require a paid data source).
- Pricing and model names change; verify them against the provider pages before relying on the numbers.
