from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import re

import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
ACTIVE_COMPETITION_TYPES = {
    "cn-standard", "cn-social", "cn-desg",
    "pin-cfc-standard", "pin-cfc-social",
}
PAGE_SIZE = 250
MAX_PAGES = 10

app = FastAPI(title="TenderRadar MVP", version="0.1.1")
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
    "publication-date", "deadline", "deadline-date-lot", "deadline-date-part",
    "estimated-value-proc", "estimated-value-cur-proc",
    "estimated-value-lot", "estimated-value-cur-lot",
    "estimated-value-part", "estimated-value-cur-part",
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
        for key in ("eng", "fra", "deu", "spa", "ita", "mul", "value", "code"):
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
    except (ValueError, TypeError):
        return None


def _date_value(value: Any) -> date | None:
    text = _first_text(value)
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _link(value: Any, publication_number: str | None) -> str | None:
    """Prefer TED's human-readable notice page, never its XML download link."""
    if publication_number:
        publication_number = publication_number.strip()
        # TED publication numbers typically look like 123456-2026 or 12345678-2026.
        if re.fullmatch(r"\d{6,8}-\d{4}", publication_number):
            return f"https://ted.europa.eu/en/notice/-/detail/{publication_number}"

    # If TED did not provide a recognisable publication number, only accept a
    # direct HTML notice URL; avoid XML/PDF links that trigger file downloads.
    candidates: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str) and item.startswith("https://ted.europa.eu/"):
            candidates.append(item)
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            for child in item.values():
                collect(child)

    collect(value)
    for url in candidates:
        if "/notice/-/detail/" in url or url.rstrip("/").endswith("/html"):
            return url
    return None


def _estimated_value_and_currency(raw: dict[str, Any]) -> tuple[float | None, str | None]:
    # Prefer procedure-level estimate, then lot/part estimates; avoid award values.
    for value_field, currency_field in (
        ("estimated-value-proc", "estimated-value-cur-proc"),
        ("estimated-value-lot", "estimated-value-cur-lot"),
        ("estimated-value-part", "estimated-value-cur-part"),
    ):
        value = _number(raw.get(value_field))
        if value is not None:
            return value, _first_text(raw.get(currency_field))
    return None, None


def _normalise_notice(raw: dict[str, Any]) -> dict[str, Any]:
    publication_number = _first_text(raw.get("publication-number"))
    cpv_raw = raw.get("classification-cpv")
    if isinstance(cpv_raw, list):
        cpvs = sorted({str(x) for x in cpv_raw if x})
    else:
        cpvs = [str(cpv_raw)] if cpv_raw else []

    deadline_raw = raw.get("deadline") or raw.get("deadline-date-lot") or raw.get("deadline-date-part")
    estimated_value, currency = _estimated_value_and_currency(raw)
    return {
        "notice_id": publication_number,
        "title": _first_text(raw.get("notice-title")) or "Untitled notice",
        "buyer": _first_text(raw.get("buyer-name")) or "Not specified",
        "country": _first_text(raw.get("buyer-country")) or "Not specified",
        "publication_date": _first_text(raw.get("publication-date")),
        "deadline": _first_text(deadline_raw),
        "_deadline_date": _date_value(deadline_raw),
        "estimated_value": estimated_value,
        "currency": currency,
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
        digits = re.sub(r"[^0-9]", "", cpv)
        if digits:
            clauses.append(f"classification-cpv={digits}*")
    if country:
        clauses.append(f"buyer-country={country}")
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=30)).strftime("%Y%m%d")
    clauses.append(f"publication-date>={cutoff}")
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
    notices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        for page in range(1, MAX_PAGES + 1):
            payload = {
                "query": query,
                "fields": FIELDS,
                "limit": PAGE_SIZE,
                "page": page,
                "scope": "ACTIVE",
                "paginationMode": "PAGE_NUMBER",
                "checkQuerySyntax": False,
            }
            response = requests.post(TED_SEARCH_URL, json=payload, timeout=25)
            response.raise_for_status()
            data = response.json()
            raw_results = data.get("notices") or data.get("results") or data.get("data") or []
            if isinstance(raw_results, dict):
                raw_results = raw_results.get("results") or raw_results.get("notices") or []
            if not isinstance(raw_results, list) or not raw_results:
                break

            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                item = _normalise_notice(raw)
                notice_id = item.get("notice_id")
                notice_type = (item.get("notice_type") or "").strip().lower()
                deadline_date = item.pop("_deadline_date", None)

                if notice_type not in ACTIVE_COMPETITION_TYPES:
                    continue
                if deadline_date is None or deadline_date < datetime.now(timezone.utc).date():
                    continue
                if notice_id and notice_id in seen_ids:
                    continue
                if notice_id:
                    seen_ids.add(notice_id)

                currency = (item.get("currency") or "").strip().upper()
                if min_value > 0 and (
                    item.get("estimated_value") is None
                    or currency != "EUR"
                    or item["estimated_value"] < min_value
                ):
                    continue

                item["match_score"] = _score(
                    item, re.sub(r"[^0-9]", "", cpv or "") or None, country3
                )
                notices.append(item)
                if len(notices) >= limit:
                    break

            if len(notices) >= limit or len(raw_results) < PAGE_SIZE:
                break

    except requests.RequestException as exc:
        return {"ok": False, "error": "TED API request failed", "detail": str(exc), "query": query}
    except ValueError:
        return {"ok": False, "error": "TED API returned invalid JSON", "query": query}

    return {
        "ok": True,
        "query": query,
        "count": len(notices),
        "notices": notices[:limit],
        "source": "TED Search API",
    }
