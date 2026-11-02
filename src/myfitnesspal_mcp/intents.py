"""One intent per MyFitnessPal write (personal-cluster AD-06, chat.md R-CHAT-022/023).

With INTENTS_URL set (lake-mcp's POST /actions), every write tool is wrapped: after the
write, and before the tool returns, one intent is posted that records the tool, its
arguments, the reply (or the error), who called (X-Client header, else
INTENTS_DEFAULT_ACTOR), what the write targeted and what a later read should show.
The reconcile model joins intents to later diary observations. Without INTENTS_URL
nothing changes. No cookie or token ever goes into an intent.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from . import config

logger = logging.getLogger(__name__)

SERVER_PREFIX = "mfp_"  # Open WebUI names the tool <server>_<tool> (lake.md L5)


# tool -> (target stream, builder of (record_key, expected) from (args, reply))
def _diary(args: dict, reply: Any) -> tuple[str, dict]:
    day = (
        (reply.get("date") if isinstance(reply, dict) else None)
        or args.get("date")
        or _today()
    )
    return day, {}


def _logged(args: dict, reply: Any) -> tuple[str, dict]:
    day, _ = _diary(args, reply)
    logged = reply.get("logged") if isinstance(reply, dict) else None
    if isinstance(logged, str):
        return day, {"entry_name": logged}
    if isinstance(logged, list):
        return day, {"entry_names": [str(x) for x in logged]}
    return day, {}


def _deleted(args: dict, reply: Any) -> tuple[str, dict]:
    day, _ = _diary(args, reply)
    return day, {"entry_absent": args.get("query")}


def _modified(args: dict, reply: Any) -> tuple[str, dict]:
    day, expected = _logged(args, reply)
    return day, expected or {"entry_name": args.get("new_query") or args.get("query")}


def _completed(args: dict, reply: Any) -> tuple[str, dict]:
    day, _ = _diary(args, reply)
    return day, {"complete": bool(args.get("complete"))}


def _custom(args: dict, reply: Any) -> tuple[str, dict]:
    return str(args.get("name") or ""), {
        "name": args.get("name"),
        "kind": args.get("kind"),
    }


def _custom_gone(args: dict, reply: Any) -> tuple[str, dict]:
    return str(args.get("name") or ""), {
        "absent": args.get("name"),
        "kind": args.get("kind"),
    }


def _measurement(args: dict, reply: Any) -> tuple[str, dict]:
    day = args.get("date") or _today()
    return day, {
        k: v for k, v in args.items() if k in ("weight", "cups", "amount", "note")
    }


WRITE_TOOLS: dict[str, tuple[str, Callable[[dict, Any], tuple[str, dict]]]] = {
    "fitness_log_food": ("diary", _logged),
    "fitness_log_custom": ("diary", _logged),
    "fitness_modify_food": ("diary", _modified),
    "fitness_delete_food": ("diary", _deleted),
    "fitness_complete_day": ("diary", _completed),
    "fitness_create_custom": ("custom", _custom),
    "fitness_delete_custom": ("custom", _custom_gone),
    "fitness_log_weight": ("measurements", _measurement),
    "fitness_log_water": ("water", _measurement),
    "fitness_log_note": ("notes", _measurement),
    "fitness_delete_exercise": ("exercise", _deleted),
}


def _today() -> str:
    return datetime.date.today().isoformat()


def is_write(tool: str, args: dict) -> bool:
    """fitness_complete_day with complete=None only reports; it is a read."""
    if tool not in WRITE_TOOLS:
        return False
    if tool == "fitness_complete_day" and args.get("complete") is None:
        return False
    return True


def build(tool: str, args: dict, reply: Any, actor: str) -> dict:
    stream, builder = WRITE_TOOLS[tool]
    record_key, expected = builder(args, reply)
    return {
        "tool": SERVER_PREFIX + tool,
        "subject": config.intents_subject(),
        "actor": actor,
        "called_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "request": _plain(args),
        "reply": _plain(reply),
        "target": {"source": "mfp", "stream": stream, "record_key": record_key},
        "expected": _plain(expected),
    }


def _plain(value: Any) -> Any:
    """JSON-safe copy; anything odd becomes its str()."""
    return json.loads(json.dumps(value, default=str))


def post(intent: dict, url: str | None = None, attempts: int = 3) -> str:
    """POST the intent to <INTENTS_URL>/<tool> (lake-mcp's /actions/<tool>); returns the id lake-mcp assigned.
    Raises after the last attempt."""
    url = (url or config.intents_url()).rstrip("/") + "/" + intent["tool"]
    data = json.dumps(intent).encode()
    last: Exception | None = None
    for _ in range(attempts):
        req = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.load(r)
                return str(body.get("intent_id") or body.get("id") or "")
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500:
                break  # a rejected body will not get better with a retry
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"intent not recorded: {type(last).__name__}: {str(last)[:120]}")


def actor_of(ctx: Any) -> str:
    """X-Client of the HTTP request behind this tool call (Open WebUI sends `openwebui`),
    else INTENTS_DEFAULT_ACTOR (mfp-gateway sets no header)."""
    try:
        request = ctx.request_context.request
        value = request.headers.get("x-client") if request is not None else None
        if value:
            return str(value)
    except Exception:  # no HTTP request behind the call (stdio, tests)
        pass
    return config.intents_default_actor()


def wrap(
    tool_name: str, fn: Callable[..., Any], poster: Callable[[dict], str] = post
) -> Callable[..., Any]:
    """The tool function, with one intent posted after the write and before the return.
    An error from MyFitnessPal is recorded as the intent's reply and then re-raised. A
    failed post never hides a write that happened: the reply carries `intent_error`."""

    @functools.wraps(fn)
    async def wrapped(**kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        args = {k: v for k, v in kwargs.items() if k != "ctx"}
        if not is_write(tool_name, args):
            return await fn(**kwargs)
        error: BaseException | None = None
        try:
            reply = await fn(**kwargs)
        except Exception as exc:
            error, reply = exc, {"error": str(exc)}
        intent = build(tool_name, args, reply, actor_of(ctx))
        try:
            intent_id = await asyncio.to_thread(poster, intent)
        except Exception as exc:
            logger.warning("intent for %s not recorded: %s", tool_name, exc)
            if isinstance(reply, dict):
                reply["intent_error"] = str(exc)
            intent_id = None
        if error is not None:
            raise error
        if isinstance(reply, dict) and intent_id:
            reply["intent_id"] = intent_id
        return reply

    return wrapped


def install(
    mcp: Any, poster: Callable[[dict], str] = post, force: bool = False
) -> list[str]:
    """Wraps every registered write tool when INTENTS_URL is set. Returns the names wrapped."""
    if not (force or config.intents_url()):
        return []
    wrapped = []
    for name in WRITE_TOOLS:
        try:
            tool = mcp._tool_manager.get_tool(name)
        except Exception:
            tool = None
        if tool is None or getattr(tool.fn, "_intents_wrapped", False):
            continue
        tool.fn = wrap(name, tool.fn, poster)
        tool.fn._intents_wrapped = True  # type: ignore[attr-defined]
        wrapped.append(name)
    return wrapped
