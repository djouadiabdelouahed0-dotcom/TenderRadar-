from __future__ import annotations

from typing import Any
import re

import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

app = FastAPI(title="TenderRadar MVP", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

COUNTRY_CODES = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "HR": "HRV", "CY": "CYP",
    "CZ": "CZE", "DK": "DNK", "EE": "EST", "FI": "FIN", "FR": "FRA",
    "DE": "DEU", "GR": "GRC", "HU": "HUN", "IE": "IRL", "IT": "ITA",
    "LV": "LVA", "LT": "LTU", "LU": "LUX", "MT": "MLT", "NL": "NLD",
    "PL": "POL", "PT": "PRT", "RO": "ROU", "SK": "SVK", "SI": "SVN",
    "ES": "ESP", "SE": "SWE", "NO": "NOR", "IS": "ISL", "LI": "LIE",
}

FIELDS = [
    "publication-number", "notice-title", "buyer-name", "buyer-country",
    "publication-date", "deadline", "total-value", "total-value-cur",
    "classification-cpv", "place-of-performance", "notice-type", "links",
]


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            result = _first_text(item)
            if result:
                return result
        return None
    if isinstance(value, dict):
        # Prefer English, then common language keys, then first non-empty value.
        for key in ("eng", "fra", "deu", "spa", "ita", "mul"):
            if key in value:
                result = _first_text(value[key])
                if result:
                    return result
        for item in value.values():
            result = _first_text(item)
            if result:
                return result
    return str(value).strip() or None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        for item in value:
            n = _number(item)
            if n is not None:
                return n
    if isinstance(value, dict):
        for key in ("amount", "value", "total", "numeric"):
            if key in value:
                n = _number(value[key])
                if n is not None:
                    return n
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _link(value: Any, publication_number: str | None) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, list):
        for item in value:
            result = _link(item, publication_number)
            if result:
                return result
    if isinstance(value, dict):
        # TED may return nested URL structures. Search recursively.
        for item in value.values():
            result = _link(item, publication_number)
            if result:
                return result
    if publication_number:
        return f"https://ted.europa.eu/en/notice/-/detail/{publication_number}"
    return None


def _normalise_notice(raw: dict[str, Any]) -> dict[str, Any]:
    publication_number = _first_text(raw.get("publication-number"))
    cpv_raw = raw.get("classification-cpv")
    if isinstance(cpv_raw, list):
        cpvs = sorted({str(x) for x in cpv_raw if x})
    else:
        cpvs = [str(cpv_raw)] if cpv_raw else []

    return {
        "notice_id": publication_number,
        "title": _first_text(raw.get("notice-title")) or "Untitled notice",
        "buyer": _first_text(raw.get("buyer-name")) or "Not specified",
        "country": _first_text(raw.get("buyer-country")) or "Not specified",
        "publication_date": _first_text(raw.get("publication-date")),
        "deadline": _first_text(raw.get("deadline")),
        "estimated_value": _number(raw.get("total-value")),
        "currency": _first_text(raw.get("total-value-cur")),
        "cpv": cpvs,
        "place_of_performance": _first_text(raw.get("place-of-performance")),
        "notice_type": _first_text(raw.get("notice-type")),
        "source_url": _link(raw.get("links"), publication_number),
    }


def _score(item: dict[str, Any], requested_cpv: str | None, requested_country: str | None) -> int:
    score = 30
    cpvs = item.get("cpv", [])
    country = item.get("country")
    if requested_cpv and any(str(c).startswith(requested_cpv[:4]) for c in cpvs):
        score += 40
    if requested_country and country == requested_country:
        score += 20
    if item.get("deadline"):
        score += 5
    if item.get("estimated_value") is not None:
        score += 5
    return min(score, 100)


def _build_query(cpv: str | None, country: str | None) -> str:
    clauses: list[str] = []
    if cpv:
        cpv = re.sub(r"[^0-9]", "", cpv)
        if cpv:
            clauses.append(f"classification-cpv={cpv}")
    if country:
        clauses.append(f"buyer-country={country}")
    clauses.append("publication-date>=today(-30)")
    return " AND ".join(clauses) + " SORT BY publication-date DESC"


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "TenderRadar MVP"}


@app.get("/api/tenders")
def search_tenders(
    cpv: str | None = Query(default=None, description="CPV code, e.g. 72000000"),
    country: str | None = Query(default=None, description="ISO-2 country, e.g. DE"),
    min_value: float = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
):
    country3 = COUNTRY_CODES.get((country or "").upper(), None)
    query = _build_query(cpv, country3)

    payload = {
        "query": query,
        "fields": FIELDS,
        "limit": limit,
        "page": 1,
        "scope": "ACTIVE",
        "paginationMode": "PAGE_NUMBER",
        "checkQuerySyntax": False,
    }

    try:
        response = requests.post(TED_SEARCH_URL, json=payload, timeout=25)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": "TED API request failed",
            "detail": str(exc),
            "query": query,
        }
    except ValueError:
        return {"ok": False, "error": "TED API returned invalid JSON", "query": query}

    raw_results = data.get("notices") or data.get("results") or data.get("data") or []
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("results") or raw_results.get("notices") or []

    notices = [_normalise_notice(x) for x in raw_results if isinstance(x, dict)]
    # When the user sets a minimum value, exclude notices whose value is unknown.
    # Otherwise, an unknown value could incorrectly pass a financial threshold.
    if min_value > 0:
        notices = [
            x for x in notices
            if x["estimated_value"] is not None
            and x["estimated_value"] >= min_value
        ]
    for notice in notices:
        notice["match_score"] = _score(notice, cpv, country3)

    return {
        "ok": True,
        "query": query,
        "count": len(notices),
        "notices": notices,
        "source": "TED Search API",
    }
