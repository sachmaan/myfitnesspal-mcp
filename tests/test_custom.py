import datetime
import json

import pytest
from conftest import FIXTURES, FakeResponse, diary_after_add

from myfitnesspal_mcp import custom, diary

TODAY = datetime.date(2026, 7, 8)

MY_FOODS_API = [
    {
        "id": "5001",
        "version": "5001",
        "brand_name": "Corner Cafe",
        "description": "Veggie Bowl",
        "serving_sizes": [{"value": 1, "unit": "bowl", "nutrition_multiplier": 1}],
        "nutritional_contents": {
            "energy": {"unit": "calories", "value": 520},
            "protein": 18,
            "carbohydrates": 70,
            "fat": 19,
        },
    }
]
MEALS_API = [
    {
        "meal_id": 42,
        "description": "My protein shake",
        "foods": [
            {"description": "Isopure, 1 scoop", "calories": 100, "protein": 25},
            {"description": "Berries, 0.5 cup", "calories": 35, "protein": 0},
        ],
    }
]
RECIPES_API = {
    "items": [
        {
            "id": "7001",
            "name": "Turkey Chili",
            "servings": 4.0,
            "nutritional_contents": {
                "energy": {"unit": "calories", "value": 1600.0},
                "protein": 160.0,
                "carbohydrates": 120.0,
                "fat": 40.0,
            },
        }
    ]
}


def fixture(name):
    return FakeResponse(text=(FIXTURES / name).read_text())


@pytest.fixture
def library(client):
    """The fake client with the three 'Add Food' tabs and the detail APIs."""
    s = client.session
    s.route("GET", "food/load_my_foods", fixture("favorites_my_foods.html"))
    s.route("GET", "food/load_meals", fixture("favorites_meals.html"))
    s.route("GET", "food/load_recipes", fixture("favorites_recipes.html"))
    s.route(
        "GET", "api/services/users/foods/mine", FakeResponse(json_data=MY_FOODS_API)
    )
    s.route("GET", "api/services/users/meals/mine", FakeResponse(json_data=MEALS_API))
    s.route("GET", "v2/recipes", FakeResponse(json_data=RECIPES_API))
    return client


def test_list_custom_reads_all_three_kinds_with_details(library):
    items = custom.list_custom(library)["items"]
    by_name = {i["name"]: i for i in items}
    assert [i["kind"] for i in items] == ["food", "food", "meal", "meal", "recipe"]

    bowl = by_name["Corner Cafe - Veggie Bowl"]
    assert bowl["food_id"] == "701"
    assert bowl["servings"] == [
        {"weight_id": "7011", "serving": "1 bowl"},
        {"weight_id": "7012", "serving": "100 g"},
    ]
    assert bowl["nutrition"] == {"calories": 520, "protein": 18, "carbs": 70, "fat": 19}

    shake = by_name["My protein shake"]
    assert shake["meal_id"] == 42
    assert [f["name"] for f in shake["ingredients"]] == [
        "Isopure, 1 scoop",
        "Berries, 0.5 cup",
    ]
    assert shake["nutrition"]["calories"] == 135

    chili = by_name["Turkey Chili"]
    assert chili["recipe_id"] == "7001"
    assert chili["recipe_servings"] == 4.0
    assert chili["nutrition"] == {
        "calories": 400.0,
        "protein": 40.0,
        "carbs": 30.0,
        "fat": 10.0,
    }


def test_list_custom_filters_by_kind_and_query(library):
    items = custom.list_custom(library, kind="meals", query="SHAKE")["items"]
    assert [i["name"] for i in items] == ["My protein shake", "Protein shake, big"]
    tabs = [(m, url) for m, url, _ in library.session.calls if "food/load_" in url]
    assert tabs == [("GET", "https://www.myfitnesspal.com/food/load_meals")]
    assert not [c for c in library.session.calls if c[0] == "POST"]


def test_list_custom_reports_a_login_redirect_instead_of_no_items(library):
    # MyFitnessPal answers a request it won't serve with a 302 to its login page.
    # Following it gives a page with no rows, which read as "you have no items".
    library.session.route(
        "GET",
        "food/load_meals",
        FakeResponse(
            status_code=302,
            headers={"location": "https://www.myfitnesspal.com/account/login"},
        ),
    )
    with pytest.raises(custom.NotLoggedIn, match="not logged in"):
        custom.list_custom(library, kind="meals")


def test_tab_requests_never_follow_redirects(library):
    custom.list_custom(library, kind="foods")
    kwargs = [kw for m, url, kw in library.session.calls if "food/load_" in url]
    assert kwargs[0]["allow_redirects"] is False


def test_list_custom_survives_a_failing_detail_api(library, make_response):
    library.session.route(
        "GET", "api/services/users/meals/mine", make_response(status_code=500)
    )
    listing = custom.list_custom(library, kind="meals")
    shake = listing["items"][0]
    assert shake["food_id"] == "801"
    assert shake["ingredients"] is None
    assert "meals details unavailable" in listing["warnings"][0]


def test_list_custom_has_no_warnings_when_details_load(library):
    assert "warnings" not in custom.list_custom(library, kind="meals")


def test_recipe_list_retries_a_sporadic_400(library, make_response, monkeypatch):
    monkeypatch.setattr(custom.time, "sleep", lambda s: None)
    ok = make_response(json_data=RECIPES_API)
    library.session.route(
        "GET",
        "v2/recipes",
        lambda calls: (
            make_response(status_code=400)
            if sum("v2/recipes" in url for _, url, _ in calls) == 1
            else ok
        ),
    )
    listing = custom.list_custom(library, kind="recipes")
    assert listing["items"][0]["recipe_id"] == "7001"
    assert "warnings" not in listing


def test_delete_custom_refuses_when_details_fail(library, make_response, monkeypatch):
    monkeypatch.setattr(custom.time, "sleep", lambda s: None)
    library.session.route("GET", "v2/recipes", make_response(status_code=503))
    with pytest.raises(RuntimeError, match="can't delete safely"):
        custom.delete_custom(library, "Turkey Chili", "recipe")


def test_list_custom_rejects_unknown_kind(library):
    with pytest.raises(ValueError, match="kind must be one of"):
        custom.list_custom(library, kind="snacks")


def test_list_custom_makes_its_ids_loggable(library):
    custom.list_custom(library, kind="foods")
    assert ("701", "7012") in diary._searched_ids


def test_log_custom_logs_a_saved_meal_and_reports_every_entry(library, diary_html):
    rows = (
        '<tr class="meal_header"><td>Breakfast</td></tr>'
        '<tr><td><a data-food-entry-id="e7">Isopure, 1 scoop</a></td></tr>'
        '<tr><td><a data-food-entry-id="e8">Berries, 0.5 cup</a></td></tr>'
    )
    library.session.route("GET", "food/diary/tester", diary_after_add(diary_html, rows))
    result = custom.log_custom(library, TODAY, "breakfast", "my protein shake")
    assert result["kind"] == "meal"
    assert result["serving"] == "1 meal"
    assert [e["name"] for e in result["logged"]] == [
        "Isopure, 1 scoop",
        "Berries, 0.5 cup",
    ]
    add = [c for c in library.session.calls if c[0] == "POST" and "food/add" in c[1]]
    data = add[-1][2]["data"]
    assert data["food_entry[food_id]"] == "801"
    assert data["food_entry[weight_id]"] == "8011"
    assert data["food_entry[meal_id]"] == "0"


def test_log_custom_picks_the_named_serving(library, diary_html):
    row = '<tr><td><a data-food-entry-id="e9">Veggie Bowl, 150 g</a></td></tr>'
    library.session.route("GET", "food/diary/tester", diary_after_add(diary_html, row))
    result = custom.log_custom(
        library, TODAY, "lunch", "Corner Cafe - Veggie Bowl", quantity=1.5, serving="g"
    )
    data = [c for c in library.session.calls if "food/add" in c[1]][-1][2]["data"]
    assert data["food_entry[weight_id]"] == "7012"
    assert data["food_entry[quantity]"] == "1.5"
    assert result["serving"] == "100 g"


def test_log_custom_unknown_serving_lists_the_choices(library):
    with pytest.raises(ValueError, match="1 bowl; 100 g"):
        custom.log_custom(
            library, TODAY, "lunch", "Corner Cafe - Veggie Bowl", serving="slice"
        )


def test_log_custom_ambiguous_name_lists_candidates(library):
    with pytest.raises(custom.NoSuchItem, match="My protein shake.*Protein shake, big"):
        custom.log_custom(library, TODAY, "breakfast", "shake", kind="meals")


def test_log_custom_unknown_name_lists_what_exists(library):
    with pytest.raises(custom.NoSuchItem, match="Turkey Chili"):
        custom.log_custom(library, TODAY, "dinner", "lasagna", kind="recipes")


def test_create_food_posts_a_private_v2_food(client, make_response):
    created = {
        "items": [
            {
                "id": "6001",
                "version": "6001",
                "brand_name": "Home",
                "description": "Oat bar",
            }
        ]
    }
    client.session.route("POST", "v2/foods", make_response(json_data=created))
    result = custom.create_food(
        client,
        "Oat bar",
        calories=210,
        protein=9,
        carbs=30,
        fat=7,
        brand="Home",
        serving_size=1,
        serving_unit="bar",
        fiber=4,
    )
    assert result == {"id": "6001", "version": "6001", "name": "Home - Oat bar"}
    method, url, kwargs = client.session.calls[-1]
    assert url == "https://api.myfitnesspal.com/v2/foods"
    item = json.loads(kwargs["data"])["items"][0]
    assert item["public"] is False
    assert item["brand_name"] == "Home"
    assert item["serving_sizes"] == [
        {"value": 1, "unit": "bar", "nutrition_multiplier": 1}
    ]
    assert item["nutritional_contents"] == {
        "energy": {"unit": "calories", "value": 210},
        "protein": 9,
        "carbohydrates": 30,
        "fat": 7,
        "fiber": 4,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"


def test_create_food_rejects_negative_or_empty_values(client):
    with pytest.raises(ValueError, match="description"):
        custom.create_food(client, "  ", calories=1, protein=0, carbs=0, fat=0)
    with pytest.raises(ValueError, match="calories"):
        custom.create_food(client, "Bar", calories=-5, protein=0, carbs=0, fat=0)


def test_create_meal_saves_a_diary_meal_and_confirms_it(client, make_response):
    saved = [{"meal_id": 77, "description": "Office breakfast", "foods": []}]

    def meals(calls):
        posted = any(m == "POST" and "meal/create" in u for m, u, _ in calls)
        return make_response(json_data=saved if posted else [])

    client.session.route("GET", "api/services/users/meals/mine", meals)
    client.session.route("GET", "meal/new", fixture("meal_new.html"))
    client.session.route("POST", "meal/create", make_response(status_code=302))
    result = custom.create_meal(client, "Office breakfast", TODAY, "breakfast")
    assert result["meal_id"] == 77
    post = [c for c in client.session.calls if "meal/create" in c[1]][-1]
    assert post[2]["data"]["meal[description]"] == "Office breakfast"
    assert post[2]["data"]["authenticity_token"] == "MEALFORMTOKEN"
    get = [c for c in client.session.calls if "meal/new" in c[1]][-1]
    assert "meal=0" in get[1] and "date=2026-07-08" in get[1]


def test_create_meal_refuses_an_empty_diary_meal(client, make_response):
    client.session.route(
        "GET", "api/services/users/meals/mine", make_response(json_data=[])
    )
    with pytest.raises(ValueError, match="nothing is logged in dinner"):
        custom.create_meal(client, "Dinner plate", TODAY, "dinner")


def test_create_meal_refuses_a_duplicate_name(client, make_response):
    existing = [{"meal_id": 5, "description": "Office breakfast", "foods": []}]
    client.session.route(
        "GET", "api/services/users/meals/mine", make_response(json_data=existing)
    )
    with pytest.raises(ValueError, match="already exists"):
        custom.create_meal(client, "office breakfast", TODAY, "breakfast")


def test_create_recipe_uses_searched_foods_and_named_servings(client, make_response):
    client.food_details[999] = {
        "calories": 105.0,
        "verified": True,
        "nutrition": {},
        "serving_sizes": [],
    }
    diary.search_food(client, "banana", limit=1)
    food = {
        "item": {
            "id": "999",
            "version": "12",
            "serving_sizes": [
                {"value": 1.0, "unit": "medium", "nutrition_multiplier": 1.0},
                {"value": 1.0, "unit": "cup, sliced", "nutrition_multiplier": 1.5},
            ],
        }
    }
    created = {
        "items": [
            {
                "id": "7002",
                "name": "Banana bread",
                "servings": 8.0,
                "nutritional_contents": {
                    "energy": {"unit": "calories", "value": 1600.0},
                    "protein": 24.0,
                    "carbohydrates": 280.0,
                    "fat": 40.0,
                },
            }
        ]
    }
    client.session.route("GET", "v2/foods/999", make_response(json_data=food))
    client.session.route("GET", "v2/recipes", make_response(json_data={"items": []}))
    client.session.route("POST", "v2/recipes", make_response(json_data=created))
    result = custom.create_recipe(
        client,
        "Banana bread",
        8,
        [{"external_id": "999", "quantity": 3, "serving": "cup"}],
    )
    assert result["recipe_id"] == "7002"
    assert result["nutrition_per_serving"]["calories"] == 200.0
    post = [c for c in client.session.calls if c[0] == "POST" and "v2/recipes" in c[1]]
    item = json.loads(post[-1][2]["data"])["items"][0]
    assert item["name"] == "Banana bread"
    assert item["servings"] == 8
    assert item["public"] is False
    assert item["ingredients"] == [
        {
            "food": {"id": "999", "version": "12"},
            "serving_size": {
                "value": 1.0,
                "unit": "cup, sliced",
                "nutrition_multiplier": 1.5,
            },
            "servings": 3,
        }
    ]


def test_create_recipe_refuses_foods_that_were_not_searched(client):
    with pytest.raises(diary.UnknownFoodId, match="fitness_search_food"):
        custom.create_recipe(client, "Mystery", 1, [{"external_id": "31337"}])
    assert not [c for c in client.session.calls if c[0] == "POST"]


def test_create_recipe_needs_ingredients_and_servings(client):
    with pytest.raises(ValueError, match="at least one ingredient"):
        custom.create_recipe(client, "Empty", 2, [])
    with pytest.raises(ValueError, match="servings"):
        custom.create_recipe(client, "Zero", 0, [{"external_id": "1"}])


def test_delete_custom_deletes_only_an_exact_name(library, make_response):
    library.session.route("DELETE", "v2/recipes/7001", make_response(status_code=204))
    assert custom.delete_custom(library, "turkey chili", "recipe") == {
        "deleted": "Turkey Chili",
        "kind": "recipe",
    }
    with pytest.raises(custom.NoSuchItem, match="exact name"):
        custom.delete_custom(library, "Turkey", "recipe")


def test_delete_custom_food_and_meal_use_their_endpoints(library, make_response):
    s = library.session
    s.route("DELETE", "v2/foods/5001", make_response(status_code=204))
    s.route("GET", "api/auth/csrf", make_response(json_data={"csrfToken": "NA-CSRF"}))
    s.route(
        "DELETE", "api/services/users/meals/delete/42", make_response(status_code=204)
    )
    custom.delete_custom(library, "Corner Cafe - Veggie Bowl", "food")
    custom.delete_custom(library, "My protein shake", "meal")
    deletes = [(url, kw) for m, url, kw in s.calls if m == "DELETE"]
    assert deletes[0][0].endswith("v2/foods/5001")
    assert deletes[1][0].endswith("api/services/users/meals/delete/42")
    assert deletes[1][1]["headers"]["X-CSRF-Token"] == "NA-CSRF"


def test_delete_custom_rejects_unknown_kind(library):
    with pytest.raises(ValueError, match="kind must be one of"):
        custom.delete_custom(library, "x", "snack")


def test_recipe_details_follow_v2_paging(library, make_response):
    first = make_response(
        json_data=RECIPES_API,
        headers={
            "link": '<https://api.myfitnesspal.com/v2/recipes?page_token=abc>; rel="next"'
        },
    )
    second = make_response(
        json_data={"items": [{"id": "7009", "name": "Late Recipe", "servings": 1.0}]}
    )
    library.session.route(
        "GET",
        "v2/recipes",
        lambda calls: (
            second if any("page_token=abc" in url for _, url, _ in calls) else first
        ),
    )
    library.session.route(
        "GET",
        "food/load_recipes",
        FakeResponse(
            text=(FIXTURES / "favorites_recipes.html")
            .read_text()
            .replace("Turkey Chili", "Late Recipe")
        ),
    )
    item = custom.list_custom(library, kind="recipes")["items"][0]
    assert item["recipe_id"] == "7009"


def test_no_match_error_caps_the_listed_names(library):
    many = [{"name": f"Item {n}", "servings": []} for n in range(20)]
    with pytest.raises(custom.NoSuchItem, match="5 more"):
        custom._pick(many, "nope")
