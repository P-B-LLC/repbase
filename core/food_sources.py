"""Reading nutrition out of USDA FoodData Central.

The app needs four numbers per food: calories, protein, carbohydrate, fat.
FoodData Central will give all four for nearly everything it holds, but it
will also give them wrong to anyone who reads the response the obvious way.
Three things bite, and each one is a silently plausible number rather than an
error, which is why they are handled here -- once, on the server, under test --
rather than on a device where nobody can see them happen.

FIRST. Two different nutrients are both called "Energy". On breaded chicken
tenders, id 1008 is 263 kcal and id 1062 is 1101 kJ. Matching on the name gets
whichever the response happens to list first; the search endpoint hands over
the kilojoules, so a calorie count read by name is 4.2 times too high. Every
nutrient here is matched by id.

SECOND. A branded food carries its nutrients per 100 g in `foodNutrients` and
per serving in `labelNutrients`. For one 284 g chicken breast those are 165
and 469 kcal. Both are correct and they describe different things. The label
figures are the ones printed on the packet the person is holding, so those are
what a branded result reports, and it says the serving it means.

THIRD. Some records have no calories at all -- raw boneless chicken breast is
one, with protein and fat present and Energy absent. Kilojoules are converted
when they are there; failing that the macros are burned at 4/4/9, which is the
same arithmetic a label uses. A record with neither is dropped rather than
logged as a food containing nothing.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from django.conf import settings

FDC_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

#: Nutrient ids, which are stable, rather than names, which collide.
ENERGY_KCAL = 1008
ENERGY_KJ = 1062
PROTEIN = 1003
CARBOHYDRATE = 1005
FAT = 1004

#: Kilojoules in a kilocalorie.
KJ_PER_KCAL = Decimal("4.184")

#: What a gram of each macro is worth, the way a label counts it.
CALORIES_PER_GRAM = {"protein": 4, "carbohydrate": 4, "fat": 9}

#: Every branded record in a search response is per 100 g -- labelNutrients,
#: which carries the per-serving figures printed on the packet, comes back
#: only from the single-food endpoint. Fetching it would be one extra request
#: per result, which the shared rate limit cannot pay for, so every food here
#: is reported per 100 g and says so. One basis for everything is the safer
#: answer anyway: a mixed list where some rows mean a slice and others mean a
#: hundred grams is how a food log quietly stops adding up.


class FoodSourceUnavailable(Exception):
    """Upstream could not be reached, or is not configured."""


def search(term, limit=25, timeout=8):
    """Foods matching `term`, normalised, the authoritative ones first.

    Two requests rather than one. Asking FoodData Central for every kind at
    once and sorting the answer does not work: "chicken breast" returns
    twenty-five supermarket packets and the USDA's own entry for chicken
    breast is nowhere in them, because a branded product whose name is exactly
    the search term outranks a reference record called "Chicken, breast,
    boneless, skinless, raw". Asking the two sets separately is the only way
    the reference data is reachable at all.

    It costs two calls per term per week, which the cache in front of this
    makes affordable, and it is the difference between a nutrition app that
    knows what chicken is and one that knows what Tyson sells.

    Raises FoodSourceUnavailable rather than returning an empty list when the
    lookup itself fails: nothing found and nothing reachable are different
    answers, and the screen says something different for each.
    """
    generic = _request(term, "Foundation,SR Legacy", limit=10, timeout=timeout)
    branded = _request(term, "Branded", limit=limit, timeout=timeout)

    foods = []
    seen = set()
    for food in generic + branded:
        if food["source_id"] in seen:
            continue
        seen.add(food["source_id"])
        foods.append(food)
    return foods[:limit]


def _request(term, data_type, limit, timeout):
    key = getattr(settings, "USDA_FDC_API_KEY", "") or ""
    if not key:
        raise FoodSourceUnavailable("no FoodData Central API key configured")

    query = urllib.parse.urlencode(
        {
            "query": term,
            "pageSize": min(max(limit, 1), 50),
            "dataType": data_type,
            "api_key": key,
        }
    )
    request = urllib.request.Request(
        f"{FDC_SEARCH_URL}?{query}",
        headers={"User-Agent": "Repbase/1.0 (+https://github.com/P-B-LLC)"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as error:  # noqa: BLE001 - upstream shape is not ours
        raise FoodSourceUnavailable(str(error)) from error

    foods = []
    for raw in payload.get("foods") or []:
        food = normalize(raw)
        if food is not None:
            foods.append(food)
    return foods


def normalize(raw):
    """One search result as the app's four numbers, or None if unusable."""
    description = (raw.get("description") or "").strip()
    if not description:
        return None

    data_type = raw.get("dataType") or ""
    brand = (raw.get("brandName") or raw.get("brandOwner") or "").strip()

    nutrition = None
    serving = "100 g"

    if data_type == "Branded":
        # Best: the figures printed on the packet. Only the single-food
        # endpoint returns these, so in practice a search result falls past it.
        nutrition = _from_label(raw.get("labelNutrients") or {})
        if nutrition is not None:
            serving = _branded_serving(raw)

        if nutrition is None:
            # Next best, and the usual case. foodNutrients is per 100 g and
            # the response says how many grams a serving is, which is enough
            # to state the food as one serving of itself rather than as a
            # weight nobody eats in. Checked against a packet that carries
            # both: 165 kcal per 100 g at a 284 g serving computes to 468.60,
            # and the label says 469.
            grams = _decimal(raw.get("servingSize"))
            unit = (raw.get("servingSizeUnit") or "").strip().lower()
            per_hundred = _from_nutrients(raw.get("foodNutrients") or [])
            if per_hundred is not None and grams and grams > 0 and unit == "g":
                nutrition = _scale(per_hundred, grams / 100)
                serving = _branded_serving(raw)

    if nutrition is None:
        # A generic record, or a branded one that states no serving weight.
        # FoodData Central holds portions for generic foods but does not
        # return them from search, so these stay per 100 g and say so.
        nutrition = _from_nutrients(raw.get("foodNutrients") or [])
        serving = "100 g"

    if nutrition is None:
        return None

    return {
        "source_id": f"usda:{raw.get('fdcId')}",
        "name": _title(description),
        "brand": brand,
        "data_type": data_type,
        "serving_description": serving,
        **nutrition,
    }


def _branded_serving(raw):
    """What one serving of a branded food is, in the packet's own words."""
    household = (raw.get("householdServingFullText") or "").strip()
    size = raw.get("servingSize")
    unit = (raw.get("servingSizeUnit") or "").strip()

    measured = ""
    if size:
        measured = f"{_trim(size)} {unit}".strip()

    if household and measured:
        return f"{household} ({measured})"
    return household or measured or "1 serving"


def _from_label(label):
    """Per-serving figures, as printed on the packet."""
    values = {}
    for field, key in (
        ("calories", "calories"),
        ("protein_grams", "protein"),
        ("carbohydrate_grams", "carbohydrates"),
        ("fat_grams", "fat"),
    ):
        entry = label.get(key) or {}
        values[field] = _decimal(entry.get("value"))

    if values["calories"] is None:
        values["calories"] = _burn(values)
    if values["calories"] is None:
        return None
    return _clamp({field: value or Decimal("0") for field, value in values.items()})


def _from_nutrients(nutrients):
    """Per 100 g figures, matched by nutrient id rather than by name."""
    by_id = {}
    for entry in nutrients:
        identifier = entry.get("nutrientId")
        if identifier is None:
            # The detail endpoint nests what the search endpoint flattens.
            identifier = (entry.get("nutrient") or {}).get("id")
        amount = entry.get("value")
        if amount is None:
            amount = entry.get("amount")
        if identifier is not None and amount is not None:
            by_id.setdefault(int(identifier), _decimal(amount))

    values = {
        "calories": by_id.get(ENERGY_KCAL),
        "protein_grams": by_id.get(PROTEIN),
        "carbohydrate_grams": by_id.get(CARBOHYDRATE),
        "fat_grams": by_id.get(FAT),
    }

    if values["calories"] is None and by_id.get(ENERGY_KJ) is not None:
        values["calories"] = (by_id[ENERGY_KJ] / KJ_PER_KCAL).quantize(Decimal("0.01"))
    if values["calories"] is None:
        values["calories"] = _burn(values)
    if values["calories"] is None:
        return None

    return _clamp({field: value or Decimal("0") for field, value in values.items()})


def _burn(values):
    """Calories implied by the macros, when the record states none."""
    grams = [
        (values.get("protein_grams"), CALORIES_PER_GRAM["protein"]),
        (values.get("carbohydrate_grams"), CALORIES_PER_GRAM["carbohydrate"]),
        (values.get("fat_grams"), CALORIES_PER_GRAM["fat"]),
    ]
    if all(amount is None for amount, _ in grams):
        return None
    total = sum((amount or Decimal("0")) * factor for amount, factor in grams)
    return total.quantize(Decimal("0.01"))


def _scale(nutrition, factor):
    """Per 100 g figures restated for a serving of `factor` hundred grams."""
    return {
        field: (value * factor).quantize(Decimal("0.01"))
        for field, value in nutrition.items()
    }


def _clamp(nutrition):
    """No negative quantities leave here.

    USDA computes carbohydrate "by difference" -- what is left of 100 g once
    water, protein, fat and ash are subtracted -- so on a food that is almost
    entirely water and protein the rounding can land just under zero. Raw
    chicken breast with skin comes back as -0.43 g of carbohydrate. That is an
    artefact of the subtraction, not a food that removes carbohydrate from the
    person eating it, and a food log that accepts it starts counting down.
    """
    return {
        field: value if value > 0 else Decimal('0')
        for field, value in nutrition.items()
    }


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _trim(value):
    """284.0 reads better as 284."""
    text = f"{value}"
    return text[:-2] if text.endswith(".0") else text


def _title(description):
    """USDA shouts branded names. Sentence case reads as a food, not a label."""
    if description.isupper():
        return description.title()
    return description
