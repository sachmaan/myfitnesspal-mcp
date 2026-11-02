import asyncio
import json

import pytest

from myfitnesspal_mcp import intents, server


class Posts:
    def __init__(self, fail=False):
        self.bodies = []
        self.fail = fail

    def __call__(self, intent):
        if self.fail:
            raise RuntimeError("lake-mcp down")
        self.bodies.append(intent)
        return "01INTENT"


class Ctx:
    def __init__(self, client=None):
        class Req:
            headers = {"x-client": client} if client else {}

        class RC:
            request = Req()

        self.request_context = RC()


def test_log_food_posts_one_intent_before_returning(monkeypatch):
    posts = Posts()

    async def fake_log_food(**kw):
        return {
            "ok": True,
            "logged": "Butter",
            "added_entries": [{"meal": "snacks", "name": "Butter, 1 tbsp"}],
            "date": "2015-01-01",
        }

    fn = intents.wrap("fitness_log_food", fake_log_food, posts)
    reply = asyncio.run(
        fn(
            query="Butter",
            date="2015-01-01",
            meal="snacks",
            quantity=1,
            ctx=Ctx("openwebui"),
        )
    )
    assert "intent_id" not in reply and len(posts.bodies) == 1
    i = posts.bodies[0]
    assert i["tool"] == "mfp_fitness_log_food" and i["actor"] == "openwebui"
    assert i["request"] == {
        "query": "Butter",
        "date": "2015-01-01",
        "meal": "snacks",
        "quantity": 1,
    }
    assert i["reply"] == reply
    assert i["target"] == {
        "source": "mfp",
        "stream": "diary",
        "record_key": "2015-01-01",
    }
    assert i["expected"] == {"diary": "Butter, 1 tbsp"}
    assert i["called_at"].endswith("Z") and i["subject"] == "household"
    json.dumps(i)


def test_post_targets_the_tool_path(monkeypatch):
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"intent_id": "01X", "tool": "mfp_fitness_log_food"}'

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("INTENTS_SUBJECT", "sachin")
    intent = intents.build(
        "fitness_log_food",
        {"date": "2015-01-01"},
        {"logged": "x", "date": "2015-01-01"},
        "openwebui",
    )
    assert intents.post(intent, "http://lakemcp:8080/actions") == "01X"
    assert (
        seen["url"] == "http://lakemcp:8080/actions/mfp_fitness_log_food"
        and seen["body"]["subject"] == "sachin"
    )


def test_error_is_recorded_then_raised(monkeypatch):
    posts = Posts()

    async def boom(**kw):
        raise RuntimeError("no entry was added")

    fn = intents.wrap("fitness_delete_food", boom, posts)
    with pytest.raises(RuntimeError, match="no entry was added"):
        asyncio.run(fn(query="Peanut Butter", date="2015-01-01", ctx=Ctx()))
    assert posts.bodies[0]["reply"] == {"error": "no entry was added"}
    assert posts.bodies[0]["expected"] == {}


def test_default_actor_when_no_header(monkeypatch):
    monkeypatch.setenv("INTENTS_DEFAULT_ACTOR", "mfp-gateway")
    posts = Posts()

    async def ok(**kw):
        return {"ok": True, "complete": True, "date": "2015-01-01"}

    fn = intents.wrap("fitness_complete_day", ok, posts)
    asyncio.run(fn(complete=True, date="2015-01-01", ctx=None))
    assert posts.bodies[0]["actor"] == "mfp-gateway"
    assert posts.bodies[0]["expected"] == {"complete": True}


def test_complete_day_report_is_not_a_write():
    posts = Posts()

    async def ok(**kw):
        return {"complete": False}

    fn = intents.wrap("fitness_complete_day", ok, posts)
    asyncio.run(fn(complete=None, date="2015-01-01", ctx=None))
    assert posts.bodies == []


def test_failed_post_keeps_the_reply_and_marks_it():
    async def ok(**kw):
        return {"ok": True, "logged": "x", "date": "2015-01-01"}

    fn = intents.wrap("fitness_log_food", ok, Posts(fail=True))
    reply = asyncio.run(fn(query="x", ctx=None))
    assert reply["ok"] is True and "lake-mcp down" in reply["intent_error"]


def test_install_wraps_every_write_tool_only_with_intents_url(monkeypatch):
    monkeypatch.delenv("INTENTS_URL", raising=False)
    assert intents.install(server.mcp, Posts()) == []
    monkeypatch.setenv("INTENTS_URL", "http://lake-mcp:8080/actions")
    names = intents.install(server.mcp, Posts())
    assert set(names) >= {
        "fitness_log_food",
        "fitness_log_custom",
        "fitness_create_custom",
        "fitness_modify_food",
        "fitness_delete_food",
        "fitness_complete_day",
    }
    assert intents.install(server.mcp, Posts()) == []  # idempotent
    tool = server.mcp._tool_manager.get_tool("fitness_log_food")
    assert tool.fn._intents_wrapped and tool.parameters["properties"].get("query")


def test_custom_items_target_the_custom_stream():
    posts = Posts()

    async def ok(**kw):
        return {"ok": True}

    fn = intents.wrap("fitness_create_custom", ok, posts)
    asyncio.run(
        fn(
            kind="food",
            name="R-CHAT-020 test food",
            nutrition={"calories": 1},
            ctx=None,
        )
    )
    assert posts.bodies[0]["target"] == {
        "source": "mfp",
        "stream": "custom",
        "record_key": "R-CHAT-020 test food",
    }
    assert posts.bodies[0]["expected"] == {}
