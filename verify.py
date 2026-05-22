"""Smoke checks for the grocery-price-comparison bot.

Exercises imports, the 2 working platform scrapers (Blinkit, Zepto), crew
construction, and the Telegram error-string guard — without consuming
LLM tokens or sending an actual Telegram message.
"""

from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _fail(label: str, reason: str) -> None:
    print(f"X {label}: {reason}")
    sys.exit(1)


def _ok(label: str) -> None:
    print(f"OK {label}")


def check_imports() -> None:
    try:
        from crewai import Agent, Crew, LLM, Process, Task  # noqa: F401
        from crewai.tools import tool  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        import requests  # noqa: F401
        from dotenv import load_dotenv  # noqa: F401
        import tools  # noqa: F401
        import crew_setup  # noqa: F401
    except Exception as exc:
        _fail("imports", repr(exc))
    _ok("imports clean")


def check_at_least_one_platform_works() -> None:
    """Both Blinkit and Zepto rate-limit aggressive crawlers. In verify, we
    accept partial coverage: at least one of the two should return products.
    The crew handles partial coverage gracefully too — Comparator works with
    whatever the Scout returned."""
    from tools import search_blinkit, search_zepto
    results = {}
    for label, tool in (("blinkit", search_blinkit), ("zepto", search_zepto)):
        raw = tool.func("milk")
        data = json.loads(raw)
        if isinstance(data, list) and data:
            first = data[0]
            if first.get("title") and first.get("price_inr") is not None:
                results[label] = (len(data), first["title"][:40], first["price_inr"])
                print(f"OK   {label}: {len(data)} products (top: {first['title'][:40]!r} at ₹{first['price_inr']})")
                continue
        if isinstance(data, dict) and "error" in data:
            print(f"WARN {label}: {data['error']}")
        else:
            print(f"WARN {label}: unexpected shape {str(data)[:80]!r}")
    if not results:
        _fail("at least one platform works", "both Blinkit and Zepto failed — likely transient rate-limit; rerun in 5 min")
    _ok(f"at least one platform works ({', '.join(results)})")


def check_blocked_platforms_return_clean_errors() -> None:
    from tools import search_bigbasket, search_instamart
    for label, tool in (("bigbasket", search_bigbasket), ("instamart", search_instamart)):
        raw = tool.func("milk")
        data = json.loads(raw)
        if not (isinstance(data, dict) and "error" in data):
            _fail(f"{label} returns error sentinel", f"got {data!r}")
    _ok("bigbasket & instamart return clean error sentinels (disabled by WAF blocks)")


def check_crew_shape() -> None:
    os.environ.setdefault("GROQ_API_KEY", "verify-stub")
    os.environ.setdefault("OPENROUTER_API_KEY", "verify-stub")
    from crew_setup import build_crew
    from tools import (
        search_blinkit, search_zepto, search_flipkart, search_instamart, search_bigbasket,
        send_telegram_reply,
    )
    crew = build_crew()
    if len(crew.agents) != 4:
        _fail("crew has 4 agents", f"got {len(crew.agents)}")
    if len(crew.tasks) != 4:
        _fail("crew has 4 tasks", f"got {len(crew.tasks)}")

    scout_tools = {getattr(t, "name", str(t)) for t in crew.agents[1].tools}
    expected_scout = {
        search_blinkit.name, search_zepto.name, search_flipkart.name,
        search_instamart.name, search_bigbasket.name,
    }
    if not expected_scout.issubset(scout_tools):
        _fail("scout has all 5 platform tools", f"missing: {expected_scout - scout_tools}")

    responder_tools = {getattr(t, "name", str(t)) for t in crew.agents[3].tools}
    if send_telegram_reply.name not in responder_tools:
        _fail("responder has telegram tool", f"got: {responder_tools}")
    _ok("crew shape: 4 agents, 4 tasks, tools wired correctly")


def check_telegram_missing_env() -> None:
    for k in ("TELEGRAM_BOT_TOKEN",):
        os.environ.pop(k, None)
    from tools import send_telegram_reply
    result = send_telegram_reply.func(12345, "test")
    if not isinstance(result, str) or not result.startswith("ERROR: missing env var"):
        _fail("telegram missing-env guard", f"got {result!r}")
    _ok("telegram tool returns ERROR string when bot token missing")


def main() -> int:
    check_imports()
    check_at_least_one_platform_works()
    check_blocked_platforms_return_clean_errors()
    check_crew_shape()
    check_telegram_missing_env()
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
