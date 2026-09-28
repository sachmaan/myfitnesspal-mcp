"""Your own MyFitnessPal items: custom foods ("My Foods"), saved meals and recipes.

Reading and logging go through the legacy "Add Food to Diary" tabs
(GET /food/load_my_foods | load_meals | load_recipes; a POST without the page's
csrf header is answered with a redirect to the login page). Each row there carries
the food_id + weight_id pairs /food/add accepts, so logging reuses
diary.push_food, with its before/after diary check. Logging a saved meal
through its row expands it into its ingredients, as the web app does.
Details come from the endpoints the current web app uses:
/api/services/users/foods/mine and /api/services/users/meals/mine (session
cookie) and v2 /recipes (Bearer token).

Writes: custom foods and recipes are created and deleted through the v2 API.
A saved meal is created the way the web's "Remember Meal" link does it, from
what is already logged in one meal of one day (/meal/new -> /meal/create), and
deleted through /api/services/users/meals/delete (NextAuth csrf token).
Everything created here is private.
"""

import json
import time
from datetime import date
from urllib import parse

from lxml import html as lh

from . import diary

KINDS = {"foods": "food", "meals": "meal", "recipes": "recipe"}
TABS = {"foods": "my_foods", "meals": "meals", "recipes": "recipes"}


class NoSuchItem(LookupError):
    pass


class NotLoggedIn(RuntimeError):
    """MyFitnessPal redirected to its login page. The message says "not logged
    in", which mfp_client.is_auth_error treats as an auth failure, so the
    server refreshes the session and retries once."""


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _web(client, path: str) -> str:
    return parse.urljoin(client.BASE_URL_SECURE, path)


def _v2(client, path: str) -> str:
    return parse.urljoin(client.BASE_API_URL, f"v2/{path}")


def _json_headers(client) -> dict:
    return diary.api_headers(
        client, {"Accept": "application/json", "Content-Type": "application/json"}
    )


RETRY_STATUSES = (400, 429, 500, 502, 503, 504)


def _get(client, url, params=None, headers=None, attempts: int = 3, pause: float = 0.5):
    """GET with a short retry: v2 /recipes answers a sporadic HTTP 400 (about
    one call in ten) that succeeds when repeated."""
    for attempt in range(attempts):
        resp = client.session.get(url, params=params, headers=headers)
        if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
            break
        time.sleep(pause * (attempt + 1))
    resp.raise_for_status()
    return resp


def _all_recipes(client, max_pages: int = 50) -> list[dict]:
    """Every recipe in v2 /recipes, which pages 10 at a time through a
    `Link: <...>; rel=next` header."""
    url, params, items = _v2(client, "recipes"), {"limit": 100}, []
    for _ in range(max_pages):
        resp = _get(client, url, params=params, headers=_json_headers(client))
        items.extend(resp.json().get("items") or [])
        link = (getattr(resp, "headers", None) or {}).get("link") or ""
        nxt = [
            p.split(";")[0].strip(" <>")
            for p in link.split(",")
            if 'rel="next"' in p or "rel=next" in p
        ]
        if not nxt:
            break
        url, params = nxt[0], None
    return items


def _kinds(kind: str) -> list[str]:
    if kind == "all":
        return list(KINDS)
    plural = kind if kind in KINDS else f"{kind}s"
    if plural not in KINDS:
        raise ValueError(f"kind must be one of: all, {', '.join(KINDS)}")
    return [plural]


def _macros(contents: dict, divisor: float = 1.0) -> dict:
    def per(value):
        return None if value is None else value / divisor

    energy = contents.get("energy") or {}
    return {
        "calories": per(energy.get("value")),
        "protein": per(contents.get("protein")),
        "carbs": per(contents.get("carbohydrates")),
        "fat": per(contents.get("fat")),
    }


def _tab_rows(client, kind: str) -> list[dict]:
    resp = client.session.get(
        _web(client, f"food/load_{TABS[kind]}"),
        params={"meal": "0", "page": "1"},
        headers=diary.api_headers(client),
        allow_redirects=False,
    )
    if 300 <= resp.status_code < 400:
        where = (getattr(resp, "headers", None) or {}).get("location") or "elsewhere"
        raise NotLoggedIn(
            f"MyFitnessPal sent food/load_{TABS[kind]} to {where}: not logged in "
            "for that page, so your saved items could not be read"
        )
    resp.raise_for_status()
    if not resp.text.strip():
        return []
    rows = []
    for tr in lh.fromstring(resp.text).xpath("//tr[contains(@class, 'favorite')]"):
        food_id = tr.xpath(".//input[contains(@name, '[food_id]')]/@value")
        cells = tr.xpath("./td")
        servings = [
            {"weight_id": o.get("value"), "serving": _clean(o.text_content())}
            for o in tr.xpath(".//select[contains(@name, '[weight_id]')]/option")
            if o.get("value")
        ]
        if not food_id or len(cells) < 2 or not servings:
            continue
        rows.append(
            {
                "kind": KINDS[kind],
                "name": _clean(cells[1].text_content()),
                "food_id": food_id[0],
                "servings": servings,
            }
        )
    return rows


def _details(client, kind: str) -> tuple[dict[str, dict], str | None]:
    """Extra fields per item name, keyed lowercase, and an error note if they
    could not be read. Best effort: listing and logging work without them."""
    try:
        return _read_details(client, kind), None
    except Exception as exc:
        return {}, f"{kind} details unavailable ({exc}); names and ids still work"


def _read_details(client, kind: str) -> dict[str, dict]:
    if kind == "foods":
        resp = client.session.get(
            _web(client, "api/services/users/foods/mine"),
            params={"search": ""},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        out = {}
        for f in resp.json() or []:
            brand = _clean(f.get("brand_name") or "")
            name = _clean(f.get("description") or "")
            extra = {
                "id": f.get("id"),
                "nutrition": _macros(f.get("nutritional_contents") or {}),
            }
            out[name.lower()] = extra
            if brand:
                out[f"{brand} - {name}".lower()] = extra
        return out
    if kind == "meals":
        resp = client.session.get(
            _web(client, "api/services/users/meals/mine"),
            params={"limit": 100, "search": ""},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        out = {}
        for m in resp.json() or []:
            foods = [
                {
                    "name": _clean(f.get("description") or ""),
                    **{k: f.get(k) for k in ("calories", "protein", "carbs", "fat")},
                }
                for f in m.get("foods") or []
            ]
            totals = {
                k: sum(f.get(k) or 0 for f in foods)
                for k in ("calories", "protein", "carbs", "fat")
            }
            out[_clean(m.get("description") or "").lower()] = {
                "meal_id": m.get("meal_id"),
                "ingredients": foods,
                "nutrition": totals,
            }
        return out
    out = {}
    for r in _all_recipes(client):
        servings = r.get("servings") or 1.0
        out[_clean(r.get("name") or "").lower()] = {
            "recipe_id": r.get("id"),
            "recipe_servings": servings,
            "nutrition": _macros(r.get("nutritional_contents") or {}, servings),
        }
    return out


_EMPTY_DETAILS = {
    "foods": {"id": None, "nutrition": None},
    "meals": {"meal_id": None, "ingredients": None, "nutrition": None},
    "recipes": {"recipe_id": None, "recipe_servings": None, "nutrition": None},
}


def list_custom(client, kind: str = "all", query: str | None = None) -> dict:
    """Your custom foods, saved meals and recipes. Every food_id + weight_id
    listed here becomes loggable with diary.push_food."""
    needle = (query or "").lower()
    items, warnings = [], []
    for plural in _kinds(kind):
        rows = [r for r in _tab_rows(client, plural) if needle in r["name"].lower()]
        details, problem = _details(client, plural) if rows else ({}, None)
        if problem:
            warnings.append(problem)
        for row in rows:
            extra = details.get(row["name"].lower(), _EMPTY_DETAILS[plural])
            items.append({**row, **extra})
            for serving in row["servings"]:
                diary._searched_ids.add(
                    (str(row["food_id"]), str(serving["weight_id"]))
                )
    return {"items": items, **({"warnings": warnings} if warnings else {})}


def _pick(items: list[dict], name: str, exact_only: bool = False) -> dict:
    wanted = _clean(name).lower()
    exact = [i for i in items if i["name"].lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [] if exact_only else [i for i in items if wanted in i["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    pool = exact or partial or items
    listed = "; ".join(repr(i["name"]) for i in pool[:15]) or "(none)"
    if len(pool) > 15:
        listed += f"; … {len(pool) - 15} more (narrow with fitness_list_custom query=)"
    if exact or partial:
        raise NoSuchItem(
            f"'{name}' matches several items: {listed}. Use one exact name."
        )
    needs = "an exact name" if exact_only else "a name"
    raise NoSuchItem(f"nothing matches '{name}'; give {needs} from: {listed}")


def log_custom(
    client,
    day: date,
    meal: str,
    name: str,
    kind: str = "all",
    quantity: float = 1.0,
    serving: str | None = None,
) -> dict:
    item = _pick(list_custom(client, kind)["items"], name)
    options = item["servings"]
    if serving is None:
        chosen = options[0]
    else:
        wanted = serving.lower()
        exact = [o for o in options if o["serving"].lower() == wanted]
        partial = [o for o in options if wanted in o["serving"].lower()]
        if not (exact or partial):
            choices = "; ".join(o["serving"] for o in options)
            raise ValueError(f"'{item['name']}' has no serving '{serving}': {choices}")
        chosen = (exact or partial)[0]
    result = diary.push_food(
        client,
        day,
        meal,
        item["name"],
        quantity,
        food_id=item["food_id"],
        weight_id=chosen["weight_id"],
    )
    return {**result, "kind": item["kind"], "serving": chosen["serving"]}


def create_food(
    client,
    description: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    brand: str | None = None,
    serving_size: float = 1,
    serving_unit: str = "serving",
    fiber: float | None = None,
    sugar: float | None = None,
    sodium: float | None = None,
    saturated_fat: float | None = None,
) -> dict:
    """Creates a private custom food. Nutrition is per one serving of
    serving_size x serving_unit."""
    if not _clean(description or ""):
        raise ValueError("description is required")
    numbers = {"calories": calories, "protein": protein, "carbs": carbs, "fat": fat}
    for key, value in numbers.items():
        if value is None or value < 0:
            raise ValueError(f"{key} must be zero or more")
    if not serving_size or serving_size <= 0 or not _clean(serving_unit or ""):
        raise ValueError("serving_size must be above zero and serving_unit set")
    contents = {
        "energy": {"unit": "calories", "value": calories},
        "protein": protein,
        "carbohydrates": carbs,
        "fat": fat,
    }
    optional = {
        "fiber": fiber,
        "sugar": sugar,
        "sodium": sodium,
        "saturated_fat": saturated_fat,
    }
    contents.update({k: v for k, v in optional.items() if v is not None})
    item = {
        "description": _clean(description),
        "public": False,
        "serving_sizes": [
            {
                "value": serving_size,
                "unit": _clean(serving_unit),
                "nutrition_multiplier": 1,
            }
        ],
        "nutritional_contents": contents,
    }
    if brand and _clean(brand):
        item["brand_name"] = _clean(brand)
    resp = client.session.post(
        _v2(client, "foods"),
        data=json.dumps({"items": [item]}),
        headers=_json_headers(client),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"MyFitnessPal /v2/foods returned HTTP {resp.status_code}")
    made = resp.json()["items"][0]
    brand_name = _clean(made.get("brand_name") or "")
    desc = _clean(made.get("description") or "")
    return {
        "id": made.get("id"),
        "version": made.get("version"),
        "name": f"{brand_name} - {desc}" if brand_name else desc,
    }


def _saved_meals(client) -> list[dict]:
    resp = client.session.get(
        _web(client, "api/services/users/meals/mine"),
        params={"limit": 100, "search": ""},
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json() or []


def create_meal(client, name: str, day: date, meal: str) -> dict:
    """Saves what is logged in one meal of one day as a named saved meal."""
    title = _clean(name or "")
    if not title:
        raise ValueError("name is required")
    target = diary._normalize_meal(meal)
    if target not in diary.MEALS:
        raise ValueError(f"meal must be one of: {', '.join(diary.MEALS)}")
    if any(
        _clean(m.get("description") or "").lower() == title.lower()
        for m in _saved_meals(client)
    ):
        raise ValueError(f"a saved meal named '{title}' already exists")
    doc, _ = diary.diary_page(client, day)
    if not [e for e in diary.diary_entries(doc) if e["meal"] == target]:
        raise ValueError(
            f"nothing is logged in {target} on {day.isoformat()}; log the foods first"
        )
    page_url = _web(
        client, f"meal/new?date={day.isoformat()}&meal={diary.MEAL_INDEX[target]}"
    )
    page = client.session.get(page_url, headers=diary.api_headers(client))
    page.raise_for_status()
    forms = lh.fromstring(page.text).xpath("//form[@action='/meal/create']")
    if not forms:
        raise RuntimeError("couldn't read MyFitnessPal's save-meal form")
    fields = {
        i.get("name"): i.get("value") or ""
        for i in forms[0].xpath(".//input")
        if i.get("name")
    }
    fields["meal[description]"] = title
    resp = client.session.post(
        _web(client, "meal/create"),
        data=fields,
        headers={"Origin": client.BASE_URL_SECURE.rstrip("/"), "Referer": page_url},
        allow_redirects=False,
    )
    if resp.status_code not in (200, 201, 302, 303):
        raise RuntimeError(
            f"MyFitnessPal /meal/create returned HTTP {resp.status_code}"
        )
    for m in _saved_meals(client):
        if _clean(m.get("description") or "").lower() == title.lower():
            return {
                "meal_id": m.get("meal_id"),
                "name": title,
                "ingredients": [
                    _clean(f.get("description") or "") for f in m.get("foods") or []
                ],
            }
    raise RuntimeError(f"MyFitnessPal did not save the meal '{title}'")


def create_recipe(client, name: str, servings: float, ingredients: list[dict]) -> dict:
    """Creates a private recipe. Each ingredient: {external_id, quantity=1,
    serving=None}; external_id comes from a fitness_search_food candidate,
    serving picks one of that food's serving sizes by name (default: its
    first), and quantity is how many of that serving go in."""
    title = _clean(name or "")
    if not title:
        raise ValueError("name is required")
    if not servings or servings <= 0:
        raise ValueError("servings must be above zero")
    if not ingredients:
        raise ValueError("a recipe needs at least one ingredient")
    unknown = [
        str(i.get("external_id"))
        for i in ingredients
        if str(i.get("external_id")) not in diary._searched_external_ids
    ]
    if unknown:
        raise diary.UnknownFoodId(
            f"external_id {', '.join(unknown)} was not returned by "
            "fitness_search_food on this server, so no recipe was created. Search "
            "for each ingredient first and pass a candidate's external_id."
        )
    if any(
        _clean(r.get("name") or "").lower() == title.lower()
        for r in _all_recipes(client)
    ):
        raise ValueError(f"a recipe named '{title}' already exists")

    lines = []
    for ingredient in ingredients:
        ext = str(ingredient["external_id"])
        resp = client.session.get(
            _v2(client, f"foods/{ext}"),
            params=[
                ("fields[]", "serving_sizes"),
                ("fields[]", "nutritional_contents"),
            ],
            headers=_json_headers(client),
        )
        resp.raise_for_status()
        food = resp.json()["item"]
        sizes = food.get("serving_sizes") or []
        if not sizes:
            raise RuntimeError(f"food {ext} has no serving sizes")
        wanted = (ingredient.get("serving") or "").lower()
        chosen = next(
            (s for s in sizes if wanted and wanted in s["unit"].lower()), None
        )
        if wanted and chosen is None:
            choices = "; ".join(f"{s['value']} {s['unit']}" for s in sizes)
            raise ValueError(
                f"food {ext} has no serving '{ingredient['serving']}': {choices}"
            )
        chosen = chosen or sizes[0]
        lines.append(
            {
                "food": {"id": food["id"], "version": food["version"]},
                "serving_size": {
                    "value": chosen["value"],
                    "unit": chosen["unit"],
                    "nutrition_multiplier": chosen["nutrition_multiplier"],
                },
                "servings": ingredient.get("quantity", 1),
            }
        )
    item = {"name": title, "servings": servings, "public": False, "ingredients": lines}
    resp = client.session.post(
        _v2(client, "recipes"),
        data=json.dumps({"items": [item]}),
        headers=_json_headers(client),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"MyFitnessPal /v2/recipes returned HTTP {resp.status_code}")
    made = resp.json()["items"][0]
    made_servings = made.get("servings") or servings
    contents = made.get("nutritional_contents") or {}
    return {
        "recipe_id": made.get("id"),
        "name": _clean(made.get("name") or title),
        "servings": made_servings,
        "nutrition_total": _macros(contents),
        "nutrition_per_serving": _macros(contents, made_servings),
    }


def delete_custom(client, name: str, kind: str) -> dict:
    """Deletes one custom food, saved meal or recipe, matched by exact name."""
    plural = _kinds(kind)
    if len(plural) != 1:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    plural = plural[0]
    rows = _tab_rows(client, plural)
    details, problem = _details(client, plural)
    if problem:
        raise RuntimeError(f"can't delete safely: {problem}")
    items = [
        {**r, **details.get(r["name"].lower(), _EMPTY_DETAILS[plural])} for r in rows
    ]
    item = _pick(items, name, exact_only=True)
    if plural == "foods" and item.get("id"):
        resp = client.session.delete(
            _v2(client, f"foods/{item['id']}"), headers=_json_headers(client)
        )
    elif plural == "recipes" and item.get("recipe_id"):
        resp = client.session.delete(
            _v2(client, f"recipes/{item['recipe_id']}"), headers=_json_headers(client)
        )
    elif plural == "meals" and item.get("meal_id") is not None:
        token = client.session.get(_web(client, "api/auth/csrf")).json()["csrfToken"]
        resp = client.session.delete(
            _web(client, f"api/services/users/meals/delete/{item['meal_id']}"),
            headers={"Accept": "application/json", "X-CSRF-Token": token},
        )
    else:
        raise RuntimeError(f"couldn't find the MyFitnessPal id of '{item['name']}'")
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"MyFitnessPal delete returned HTTP {resp.status_code}")
    return {"deleted": item["name"], "kind": KINDS[plural]}
