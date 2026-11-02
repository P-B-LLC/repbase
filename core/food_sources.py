"""Read-only Open Food Facts adapter. Never invent missing nutrition values.

Public product data only: no account IDs, diary entries or images are sent.
Saved meals keep their own numeric snapshots and never depend on this service.
"""
import json
import hashlib
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

FIELDS = "code,product_name,product_name_en,brands,nutriments,serving_size,serving_quantity,serving_quantity_unit,nutrition_data_per,product_quantity_unit"
MAX_BYTES = 2_000_000
DISCOVERY_ORIGIN = "https://search.openfoodfacts.org"
NEGATIVE_LIFETIME = timedelta(minutes=15)
FOLLOWER_WAIT = 0.6
LEASE_SECONDS = 12
LEGACY_HYDRATION_LIMIT = 3
logger = logging.getLogger(__name__)


class FoodSourceUnavailable(Exception):
    """Unavailable, rate limited, or malformed upstream; not an empty search."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = None if retry_after is None else max(1, math.ceil(retry_after))


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
    service = kind
    kind = "search" if kind == "discover" else kind
    key = "off:gate:" + kind
    try:
        with transaction.atomic():
            FoodSearchCache.objects.get_or_create(term=key, defaults={"payload": {}})
            row = FoodSearchCache.objects.select_for_update().get(term=key)
            now = timezone.now().timestamp()
            payload = row.payload if isinstance(row.payload, dict) else {}
            cooldowns = payload.get("blocked", {})
            blocked = cooldowns.get(service, 0) if isinstance(cooldowns, dict) else 0
            if isinstance(blocked, (int, float)) and now < blocked:
                raise FoodSourceUnavailable("Catalogue is busy; retry shortly.", blocked - now)
            timestamps = payload.get("requests", [])
            timestamps = [stamp for stamp in timestamps if isinstance(stamp, (int, float)) and now - 60 < stamp <= now] if isinstance(timestamps, list) else []
            limit = 10 if kind == "search" else 15
            if len(timestamps) >= limit:
                raise FoodSourceUnavailable("Catalogue is busy; retry shortly.", timestamps[0] + 60 - now)
            row.payload = {"requests": timestamps + [now], "blocked": cooldowns}
            row.save(update_fields=["payload", "fetched_at"])
    except FoodSourceUnavailable:
        raise
    except Exception as error:
        raise FoodSourceUnavailable("Could not reserve catalogue capacity.") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _retry_after(headers):
    value = headers.get("Retry-After") if headers else None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - timezone.now().timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(300, max(1, math.ceil(seconds))) if math.isfinite(seconds) else None


def _defer_gate(kind, seconds):
    from .models import FoodSearchCache
    key = "off:gate:" + ("search" if kind == "discover" else kind)
    try:
        with transaction.atomic():
            row = FoodSearchCache.objects.select_for_update().get(term=key)
            payload = row.payload if isinstance(row.payload, dict) else {}
            cooldowns = payload.get("blocked", {})
            if not isinstance(cooldowns, dict):
                cooldowns = {}
            cooldowns[kind] = max(cooldowns.get(kind, 0), timezone.now().timestamp() + seconds)
            payload["blocked"] = cooldowns
            row.payload = payload
            row.save(update_fields=["payload", "fetched_at"])
    except Exception as error:
        raise FoodSourceUnavailable("Could not record catalogue cooldown.", seconds) from error


def _read_body(response, deadline):
    chunks = []
    size = 0
    # read1 returns after at most one underlying read, unlike read(n), which
    # can keep a slow-dripping response alive beyond the overall deadline.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Catalogue response deadline exceeded")
        try:
            response.fp.raw._sock.settimeout(remaining)
        except AttributeError:
            pass
        chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
        if not isinstance(chunk, bytes):
            raise ValueError("Malformed catalogue body")
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError("Oversized catalogue response")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(path, params, kind, timeout):
    origin = DISCOVERY_ORIGIN if kind == "discover" else settings.OPEN_FOOD_FACTS_ORIGIN
    if kind != "discover" and origin not in ("https://world.openfoodfacts.org", "https://world.openfoodfacts.net"):
        raise FoodSourceUnavailable("Unapproved catalogue origin.")
    if timeout <= 0:
        raise FoodSourceUnavailable("Catalogue request deadline exceeded.")
    deadline = time.monotonic() + timeout
    _reserve_slot(kind)
    headers = {"User-Agent": settings.OPEN_FOOD_FACTS_USER_AGENT, "Accept": "application/json"}
    if origin.endswith(".net"):
        headers["Authorization"] = "Basic b2ZmOm9mZg=="  # Public staging credentials.
    request = urllib.request.Request(origin + path + "?" + urllib.parse.urlencode(params), headers=headers)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Catalogue request deadline exceeded")
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=remaining) as response:
            body = _read_body(response, deadline)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Malformed catalogue response")
        return payload
    except urllib.error.HTTPError as error:
        if error.code == 404 and kind == "barcode":
            return {"status": 0}
        wait = _retry_after(error.headers)
        if wait is not None or error.code == 429:
            wait = wait or 60
            _defer_gate(kind, wait)
        raise FoodSourceUnavailable("Catalogue request failed.", wait) from error
    except Exception as error:
        raise FoodSourceUnavailable("Catalogue could not be read.") from error


def _literal_query(term):
    # Preserve normal text: this service treats an outer pair of quotes as
    # literal characters, not a phrase. Escape query metacharacters instead.
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&<>=])', r'\\\1', term)
    return re.sub(r"\b(?:AND|OR|NOT|TO)\b", lambda match: "\\" + match.group(0), escaped)


def discover(term, limit=25, timeout=8):
    limit = min(max(limit, 1), 25)
    payload = _request("/search", {"q": _literal_query(term), "page_size": limit,
        "fields": "code,product_name,product_name_en,brands"}, "discover", timeout)
    if payload.get("timed_out") or payload.get("errors") or payload.get("error"):
        raise FoodSourceUnavailable("Discovery query did not complete.")
    hits = payload.get("hits")
    if not isinstance(hits, list):
        raise FoodSourceUnavailable("Malformed discovery response.")
    found = {}
    for raw in hits:
        if not isinstance(raw, dict):
            continue
        name = raw.get("product_name") or raw.get("product_name_en")
        if isinstance(name, dict):
            name = name.get("en") or next((value for value in name.values() if isinstance(value, str) and value.strip()), None)
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            code = barcode(str(raw.get("code", "")))
        except ValueError:
            continue
        brand = raw.get("brands")
        if isinstance(brand, list):
            brand = ", ".join(value for value in brand if isinstance(value, str))
        found.setdefault("off:" + code, {"source_id": "off:" + code, "name": name.strip()[:240], "brand": brand[:240] if isinstance(brand, str) else ""})
        if len(found) >= limit:
            break
    return list(found.values())


def _remaining(deadline, maximum=8):
    remaining = min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise FoodSourceUnavailable("Catalogue request deadline exceeded.")
    return remaining


def search(term, limit=25, timeout=8):
    if settings.OPEN_FOOD_FACTS_ORIGIN not in ("https://world.openfoodfacts.org", "https://world.openfoodfacts.net"):
        raise FoodSourceUnavailable("Unapproved catalogue origin.")
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
        if food is not None and food["source_id"] != "off:" + code:
            raise FoodSourceUnavailable("Product response did not match its barcode.")
        return [food] if food else []
    # OFF v3 has no text search. Its documented legacy text-search route is
    # used deliberately; v2 search_terms does not provide full-text search.
    deadline = time.monotonic() + min(timeout, 8)
    try:
        payload = _request("/cgi/search.pl", {
            "search_terms": term, "search_simple": 1, "action": "process",
            "json": 1, "page_size": min(max(limit, 1), 50), "fields": FIELDS,
        }, "search", _remaining(deadline, 2))
        if not isinstance(payload.get("products"), list):
            raise FoodSourceUnavailable("Malformed search response.")
    except FoodSourceUnavailable as original:
        candidates = cached_lookup(term, discovery=True, timeout=_remaining(deadline, 3))
        found = []
        last_error = None
        for candidate in candidates[:LEGACY_HYDRATION_LIMIT]:
            try:
                found.extend(cached_lookup("barcode:" + candidate["source_id"][4:], timeout=_remaining(deadline, 2)))
            except FoodSourceUnavailable as error:
                last_error = error
        if found:
            return PartialFoodResults(found[:limit]) if last_error is not None else found[:limit]
        if last_error is not None:
            raise last_error from original
        return []
    products = payload.get("products")
    if not isinstance(products, list):
        raise FoodSourceUnavailable("Malformed search response.")
    found = {}
    for raw in products:
        food = normalize(raw)
        if food:
            found.setdefault(food["source_id"], food)
    return list(found.values())[:limit]


class PartialFoodResults(list):
    """Useful complete foods, but a transient failure must not poison a cache."""


def _cache_payload(rows):
    return [{key: format(value, "f") if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows]


def _cache_age(row):
    return timezone.now() - row.fetched_at


def _safe_cache(row, discovery):
    if row is None or not isinstance(row.payload, list):
        return None
    for item in row.payload:
        if not isinstance(item, dict) or not isinstance(item.get("source_id"), str) or not item["source_id"].startswith("off:"):
            return None
        try:
            if item["source_id"] != "off:" + barcode(item["source_id"][4:]):
                return None
        except ValueError:
            return None
        if row.term.startswith("off:v1:barcode:") and item["source_id"] != "off:" + row.term.rsplit(":", 1)[1]:
            return None
        if not isinstance(item.get("name"), str) or not item["name"].strip() or not isinstance(item.get("brand"), str):
            return None
        if not discovery and (
            not isinstance(item.get("serving_description"), str) or not item["serving_description"].strip()
            or any(_number(item.get(field)) is None for field in ("calories", "protein_grams", "carbohydrate_grams", "fat_grams"))
        ):
            return None
    return row


def _fresh(row):
    return row is not None and isinstance(row.payload, list) and _cache_age(row) < (NEGATIVE_LIFETIME if not row.payload else row.LIFETIME)


def _lease(key):
    from .models import FoodSearchCache
    lease_key = "off:lease:" + hashlib.sha256(key.encode()).hexdigest()
    token = uuid.uuid4().hex
    try:
        with transaction.atomic():
            FoodSearchCache.objects.get_or_create(term=lease_key, defaults={"payload": {}})
            row = FoodSearchCache.objects.select_for_update().get(term=lease_key)
            payload = row.payload if isinstance(row.payload, dict) else {}
            now = timezone.now().timestamp()
            if payload.get("until", 0) > now:
                return lease_key, None
            row.payload = {"until": now + LEASE_SECONDS, "owner": token}
            row.save(update_fields=["payload", "fetched_at"])
        return lease_key, token
    except Exception as error:
        raise FoodSourceUnavailable("Could not reserve catalogue lookup.", 1) from error


def cached_lookup(term, *, discovery=False, timeout=8):
    """Exact-key fresh/stale cache, shared in-flight coalescing, no HTTP locks.

    This is a small response cache, not a local copy of the OFF database.
    A cold external lookup can still fail; no made-up successful empty result.
    """
    from .models import FoodSearchCache
    key = "off:v2:discover:" + term if discovery else cache_key(term)
    cached = _safe_cache(FoodSearchCache.objects.filter(term=key).first(), discovery)
    if _fresh(cached):
        return cached.payload
    try:
        lease_key, token = _lease(key)
    except FoodSourceUnavailable:
        # Another owner can publish and delete its lease between our cache
        # read and the row lock. Recheck the answer before showing "busy".
        # Retained data also remains useful if reserving a lookup fails.
        if cached is not None and cached.payload and _cache_age(cached) < FoodSearchCache.RETENTION:
            return cached.payload
        try:
            shared = _safe_cache(FoodSearchCache.objects.filter(term=key).first(), discovery)
        except DatabaseError:
            shared = None
        if _fresh(shared):
            return shared.payload
        raise
    if token is None:
        if cached is not None and isinstance(cached.payload, list) and cached.payload and _cache_age(cached) < FoodSearchCache.RETENTION:
            return cached.payload
        deadline = time.monotonic() + min(FOLLOWER_WAIT, timeout)
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            shared = _safe_cache(FoodSearchCache.objects.filter(term=key).first(), discovery)
            if _fresh(shared):
                return shared.payload
        raise FoodSourceUnavailable("This catalogue lookup is already in progress.", 1)
    try:
        # The first cache read may predate another process's finished lookup.
        shared = _safe_cache(FoodSearchCache.objects.filter(term=key).first(), discovery)
        if _fresh(shared):
            return shared.payload
        rows = discover(term, timeout=timeout) if discovery else search(term, timeout=timeout)
        payload = _cache_payload(rows)
        if not isinstance(rows, PartialFoodResults):
            with transaction.atomic():
                owner = FoodSearchCache.objects.select_for_update().filter(term=lease_key).first()
                if owner is not None and isinstance(owner.payload, dict) and owner.payload.get("owner") == token and owner.payload.get("until", 0) > timezone.now().timestamp():
                    FoodSearchCache.objects.update_or_create(term=key, defaults={"payload": payload})
        return payload
    except FoodSourceUnavailable:
        if cached is not None and isinstance(cached.payload, list) and cached.payload and _cache_age(cached) < FoodSearchCache.RETENTION:
            return cached.payload
        raise
    finally:
        # Compare ownership under the lock; never release a newer lease after
        # this process's lease expired or another worker took over.
        try:
            with transaction.atomic():
                row = FoodSearchCache.objects.select_for_update().filter(term=lease_key).first()
                if row is not None and isinstance(row.payload, dict) and row.payload.get("owner") == token:
                    row.delete()
        except DatabaseError:
            # The short lease expires on its own. A cleanup fault must not
            # turn valid food details into an HTTP 500 or expose search text.
            logger.warning("Food catalogue lease cleanup failed")


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
