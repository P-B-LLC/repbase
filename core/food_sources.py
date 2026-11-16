"""Read-only Open Food Facts adapter. Never invent missing nutrition values.

Public product data only: no account IDs, diary entries or images are sent.
Saved meals keep their own numeric snapshots and never depend on this service.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

FIELDS = "code,product_name,product_name_en,brands,nutriments,serving_size,serving_quantity,serving_quantity_unit,nutrition_data_per,product_quantity_unit"
MAX_BYTES = 2_000_000


class FoodSourceUnavailable(Exception):
    """Unavailable, rate limited, or malformed upstream; not an empty search."""


def barcode(value):
    """Validate GTIN-8/12/13/14, retaining leading zeroes; UPC-E is not GTIN-8."""
    value = re.sub(r"[ -]", "", value)
    if not re.fullmatch(r"(?:[0-9]{8}|[0-9]{12,14})", value):
        raise ValueError("Enter an 8, 12, 13 or 14 digit product barcode.")
    total = sum(int(n) * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(value[:-1])))
    if (10 - total % 10) % 10 != int(value[-1]):
        raise ValueError("Check the barcode digits and try again.")
    return value.zfill(13) if len(value) == 12 else value


def cache_key(term):
    if term.startswith("barcode:"):
        return "off:v1:barcode:" + barcode(term[8:])
    if re.fullmatch(r"[0-9 -]+", term):
        return "off:v1:barcode:" + barcode(term)
    return "off:v1:search:" + term


def _reserve_slot(kind):
    # A database row lock shares the upstream allowance across all web workers,
    # including deployments without Redis. Never hold the lock during HTTP.
    from .models import FoodSearchCache
    key = "off:gate:" + kind
    try:
        with transaction.atomic():
            FoodSearchCache.objects.get_or_create(term=key, defaults={"payload": {}})
            row = FoodSearchCache.objects.select_for_update().get(term=key)
            now = timezone.now().timestamp()
            if now < row.payload.get("next", 0):
                raise FoodSourceUnavailable("Catalogue is busy; retry shortly.")
            row.payload = {"next": now + (7 if kind == "search" else 5)}
            row.save(update_fields=["payload", "fetched_at"])
    except FoodSourceUnavailable:
        raise
    except Exception as error:
        raise FoodSourceUnavailable("Could not reserve catalogue capacity.") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(path, params, kind, timeout):
    origin = settings.OPEN_FOOD_FACTS_ORIGIN
    if origin not in ("https://world.openfoodfacts.org", "https://world.openfoodfacts.net"):
        raise FoodSourceUnavailable("Unapproved catalogue origin.")
    _reserve_slot(kind)
    headers = {"User-Agent": settings.OPEN_FOOD_FACTS_USER_AGENT, "Accept": "application/json"}
    if origin.endswith(".net"):
        headers["Authorization"] = "Basic b2ZmOm9mZg=="  # Public staging credentials.
    request = urllib.request.Request(origin + path + "?" + urllib.parse.urlencode(params), headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Oversized catalogue response")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Malformed catalogue response")
        return payload
    except urllib.error.HTTPError as error:
        if error.code == 404 and kind == "barcode":
            return {"status": 0}
        raise FoodSourceUnavailable("Catalogue request failed.") from error
    except Exception as error:
        raise FoodSourceUnavailable("Catalogue could not be read.") from error


def search(term, limit=25, timeout=8):
    key = cache_key(term)
    if key.startswith("off:v1:barcode:"):
        code = key.rsplit(":", 1)[1]
        payload = _request("/api/v3/product/" + code + ".json", {"fields": FIELDS}, "barcode", timeout)
        result = payload.get("result")
        if payload.get("status") == 0 or (isinstance(result, dict) and result.get("id") == "product_not_found"):
            return []
        raw = payload.get("product")
        if not isinstance(raw, dict):
            raise FoodSourceUnavailable("Malformed product response.")
        food = normalize(raw)
        return [food] if food else []
    # OFF v3 has no text search. Its documented legacy text-search route is
    # used deliberately; v2 search_terms does not provide full-text search.
    payload = _request("/cgi/search.pl", {
        "search_terms": term, "search_simple": 1, "action": "process",
        "json": 1, "page_size": min(max(limit, 1), 50), "fields": FIELDS,
    }, "search", timeout)
    products = payload.get("products")
    if not isinstance(products, list):
        raise FoodSourceUnavailable("Malformed search response.")
    found = {}
    for raw in products:
        food = normalize(raw)
        if food:
            found.setdefault(food["source_id"], food)
    return list(found.values())[:limit]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number <= 999999 else None
    except (InvalidOperation, ValueError):
        return None


def normalize(raw):
    if not isinstance(raw, dict):
        return None
    name = raw.get("product_name") or raw.get("product_name_en")
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        code = barcode(str(raw.get("code", "")))
    except ValueError:
        return None
    nutrients = raw.get("nutriments")
    if not isinstance(nutrients, dict):
        return None
    # Use one explicitly labelled basis for ALL four figures. Missing is not
    # zero. Do not derive calories from 4/4/9 (fibre/polyols make that wrong).
    serving = raw.get("serving_size")
    basis = raw.get("nutrition_data_per")
    if basis not in ("100g", "100ml", "serving"):
        return None
    unit = raw.get("serving_quantity_unit") or raw.get("product_quantity_unit")
    if basis == "100ml":
        unit = "ml"
    if not unit:
        # OFF stores liquid values under *_100g as well. Do not label a liquid
        # as grams if a serving explicitly specifies millilitres.
        unit = "ml" if isinstance(serving, str) and re.search(r"\bml\b", serving, re.I) else "g"
    if unit not in ("g", "ml"):
        return None
    for suffix in (("serving", "100g") if isinstance(serving, str) and serving.strip() else ("100g",)):
        values = {field: _number(nutrients.get(key + "_" + suffix)) for field, key in (
            ("calories", "energy-kcal"), ("protein_grams", "proteins"),
            ("carbohydrate_grams", "carbohydrates"), ("fat_grams", "fat"))}
        if values["calories"] is None:
            kj = _number(nutrients.get("energy-kj_" + suffix))
            if kj is None and nutrients.get("energy_unit") == "kJ":
                kj = _number(nutrients.get("energy_" + suffix))
            if kj is not None:
                values["calories"] = kj / Decimal("4.184")
        if any(value is None for value in values.values()):
            continue
        if suffix == "100g" and (values["calories"] > 1000 or any(values[f] > 100 for f in ("protein_grams", "carbohydrate_grams", "fat_grams"))):
            continue
        brand = raw.get("brands")
        return {"source_id": "off:" + code, "name": name.strip()[:240],
                "brand": brand[:240] if isinstance(brand, str) else "",
                "serving_description": serving.strip()[:160] if suffix == "serving" else "100 " + unit,
                **{key: value.quantize(Decimal("0.01")) for key, value in values.items()}}
    return None
