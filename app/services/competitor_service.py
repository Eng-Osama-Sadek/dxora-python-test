"""Post-processing of the competitor list returned by the LLM.

Competitors are AI-generated suggestions (no live market research is performed), so every
entry is labelled `source: "ai_suggestion"` and cleaned before it is returned.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.schemas import Competitor

MAX_COMPETITORS = 6
MIN_COMPETITORS = 3
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def _host(value: str | None) -> str:
    if not value:
        return ""
    candidate = value.strip().lower()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    return host if _DOMAIN.match(host) else ""


def normalize_competitors(competitors: list[Competitor], *, brand_name: str, site_url: str) -> list[Competitor]:
    own_host = _host(site_url)
    own_name = brand_name.strip().lower()
    result: list[Competitor] = []
    seen: set[str] = set()
    for competitor in competitors:
        name = competitor.name.strip()
        key = name.lower()
        host = _host(competitor.website)
        if not name or key in seen:
            continue
        if key == own_name or (host and host == own_host):
            continue  # the brand is not its own competitor
        seen.add(key)
        result.append(
            competitor.model_copy(
                update={
                    "name": name,
                    "website": f"https://{host}" if host else None,
                    "source": "ai_suggestion",
                }
            )
        )
        if len(result) == MAX_COMPETITORS:
            break
    return result


def competitor_notes(count: int) -> list[str]:
    notes = [
        "Competitors are AI-generated suggestions based on the model's general knowledge and the "
        "website content; they were not verified with live market research."
    ]
    if count < MIN_COMPETITORS:
        notes.append(
            f"Only {count} reliable competitor(s) could be identified (target is {MIN_COMPETITORS}+). "
            "None were invented to fill the gap."
        )
    return notes
