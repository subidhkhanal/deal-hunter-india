"""Build the 4-agent CrewAI crew for grocery price comparison.

Flow: Query Normaliser → Scout (4 platform tools) → Comparator → Responder.

Triggered per Telegram message — kickoff receives {query, chat_id} inputs.
"""

from __future__ import annotations

import os

# CrewAI 1.14.x stamps every message with a 'cache_breakpoint' property meant
# for Anthropic prompt caching. OpenRouter (via its OpenAI-compatible API) and
# Groq both reject unknown message properties with HTTP 400. Neutralise the
# marker before any agent executes — both the legacy and experimental
# executors re-import this name on every call.
import crewai.llms.cache as _crewai_cache
_crewai_cache.mark_cache_breakpoint = lambda message: message

import logging
from typing import Any

from pydantic import PrivateAttr

from crewai import Agent, Crew, LLM, Process, Task
from crewai.llms.base_llm import BaseLLM

from tools import (
    search_blinkit,
    search_zepto,
    search_instamart,
    search_bigbasket,
    send_telegram_reply,
)

log = logging.getLogger(__name__)


class FallbackLLM(BaseLLM):
    """Try the primary LLM; on rate-limit / timeout / empty response, fall back.
    Inherits BaseLLM so pydantic-strict Agent.llm validation accepts it."""

    _primary: LLM = PrivateAttr()
    _fallback: LLM = PrivateAttr()

    _FALLBACK_INDICATORS = (
        "ratelimit", "rate_limit", "429", "timeout", "timed out",
        "service unavailable", "503", "502", "504",
        "invalid response", "empty", "none or empty",
        "context_length_exceeded",
    )

    def __init__(self, primary: LLM, fallback: LLM, **kwargs: Any) -> None:
        # Inherit the primary's model name + base config so any BaseLLM consumer
        # sees the expected interface.
        super().__init__(
            model=primary.model,
            temperature=getattr(primary, "temperature", None),
            **kwargs,
        )
        self._primary = primary
        self._fallback = fallback

    def _should_fallback(self, exc: BaseException) -> bool:
        text = f"{type(exc).__name__} {exc}".lower()
        return any(needle in text for needle in self._FALLBACK_INDICATORS)

    def call(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._primary.call(*args, **kwargs)
        except Exception as exc:
            if self._should_fallback(exc):
                log.warning(
                    "Primary LLM (%s) failed: %s — falling back to %s",
                    self._primary.model, exc, self._fallback.model,
                )
                return self._fallback.call(*args, **kwargs)
            raise

    def supports_function_calling(self) -> bool:
        return self._primary.supports_function_calling() if hasattr(self._primary, "supports_function_calling") else True

    def get_context_window_size(self) -> int:
        try:
            return self._primary.get_context_window_size()
        except Exception:
            return 8192


def _build_llm():
    """Primary Groq llama-3.3-70b (fast, 12K TPM); fallback OpenRouter free model.
    Both keys must be set in .env. Either can be overridden via PRIMARY_MODEL /
    FALLBACK_MODEL env vars."""
    primary_model = os.getenv("PRIMARY_MODEL", "groq/llama-3.3-70b-versatile")
    fallback_model = os.getenv(
        "FALLBACK_MODEL",
        "openrouter/qwen/qwen3-next-80b-a3b-instruct:free",
    )

    primary = LLM(
        model=primary_model,
        api_key=os.getenv("GROQ_API_KEY") if "groq/" in primary_model else os.getenv("OPENROUTER_API_KEY"),
        temperature=0.2,
        timeout=60,
    )
    fallback = LLM(
        model=fallback_model,
        api_key=os.getenv("OPENROUTER_API_KEY") if "openrouter/" in fallback_model else os.getenv("GROQ_API_KEY"),
        temperature=0.2,
        timeout=60,
    )
    log.info("LLM: primary=%s fallback=%s", primary_model, fallback_model)
    return FallbackLLM(primary, fallback)


def _build_agents(llm) -> tuple[Agent, Agent, Agent, Agent]:
    normaliser = Agent(
        role="Grocery Query Normaliser",
        goal=(
            "Turn a casual user query like 'chiken 1kg' or 'milk 1 ltr' into a "
            "clean, search-friendly phrase like 'chicken 1 kg' or 'milk 1 litre' "
            "that grocery sites will recognise."
        ),
        backstory=(
            "You have a feel for how Indians type grocery items. You fix common "
            "typos (chiken→chicken, biskut→biscuit), expand units (1kg→1 kg, "
            "500g→500 gram), and strip filler words. You DO NOT translate items "
            "to English if the user typed them in Hindi — those work fine on "
            "Indian grocery sites."
        ),
        tools=[],
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    scout = Agent(
        role="Multi-platform Grocery Scout",
        goal=(
            "Given a normalised query, search all four grocery platforms "
            "(Blinkit, Zepto, Swiggy Instamart, BigBasket) and return their "
            "raw JSON product lists."
        ),
        backstory=(
            "You know each of your four search tools returns a JSON array of "
            "products on success, or an {\"error\": \"...\"} object on failure. "
            "Call each tool exactly once with the normalised query. Don't "
            "summarise, transform, or judge the results — pass them through. "
            "Some platforms will fail; that's OK, just include the error."
        ),
        tools=[search_blinkit, search_zepto, search_instamart, search_bigbasket],
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    comparator = Agent(
        role="Grocery Price Comparator",
        goal=(
            "Compare products across platforms, normalise to ₹/kg or ₹/litre "
            "where possible, and pick the top 3 by value. Be willing to skip a "
            "platform that returned no relevant results or only returned an error."
        ),
        backstory=(
            "You've worked grocery procurement long enough to know that "
            "'250g x 4' on one site and '1kg' on another are the same thing, "
            "and that the per-unit price is what matters. You also know that "
            "the cheapest isn't always best — a bag of unknown-brand atta at "
            "₹40/kg vs Aashirvaad at ₹55/kg is a quality call, not a price call. "
            "Mention brand quality in your top 3 picks. Skip out-of-stock items."
        ),
        tools=[],
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    responder = Agent(
        role="Telegram Reply Composer",
        goal=(
            "Format the top 3 picks into a short, scannable Telegram message "
            "and send it via your tool to the user's chat_id."
        ),
        backstory=(
            "You write Telegram messages the way a friend texts. Plain text, no "
            "markdown gymnastics. Use emoji for ranking (🥇🥈🥉). Each pick gets "
            "platform, title, price per unit if known, and the link. Telegram "
            "auto-linkifies URLs — no need to format them."
        ),
        tools=[send_telegram_reply],
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    return normaliser, scout, comparator, responder


def build_crew() -> Crew:
    llm = _build_llm()
    normaliser, scout, comparator, responder = _build_agents(llm)

    normalise_task = Task(
        description=(
            "The user typed this grocery query into our Telegram bot:\n\n"
            "    {query}\n\n"
            "Return ONLY the cleaned search phrase. No quotes, no commentary, "
            "no 'Here is the normalised query:' preamble — just the phrase itself."
        ),
        expected_output="A single line of clean grocery search text.",
        agent=normaliser,
    )

    scan_task = Task(
        description=(
            "Take the normalised query from the previous task. Call all four "
            "platform search tools with that query — exactly one call to each:\n"
            "  - Search Blinkit for a product\n"
            "  - Search Zepto for a product\n"
            "  - Search Swiggy Instamart for a product\n"
            "  - Search BigBasket for a product\n"
            "Collect the four tool outputs into one JSON object like:\n"
            "  {{\"blinkit\": <tool output>, \"zepto\": <tool output>, "
            "\"instamart\": <tool output>, \"bigbasket\": <tool output>}}\n"
            "If a tool returned an error string, include it as-is. Do not "
            "filter, dedupe, or summarise. Return only that JSON object."
        ),
        expected_output="A JSON object keyed by platform with raw tool outputs.",
        agent=scout,
        context=[normalise_task],
    )

    compare_task = Task(
        description=(
            "You have raw search results from up to 4 platforms in the previous "
            "task. Pick the top 3 best-value products that genuinely match the "
            "user's original query: {query}.\n\n"
            "Rules:\n"
            "- Skip platforms that returned an error or empty result\n"
            "- Skip out-of-stock items (in_stock=false)\n"
            "- For each pick, compute price-per-unit (₹/kg or ₹/litre) when "
            "the quantity is parseable\n"
            "- Prefer recognised Indian brands (Amul, Aashirvaad, Fortune, Tata, "
            "MTR, Nestle, Mother Dairy) over unknown ones, all else being equal\n"
            "- If only 1-2 platforms returned anything usable, still produce a "
            "top 1 or top 2 — don't fail just because coverage is low\n\n"
            "Output JSON: {{\"picks\": [{{\"rank\": 1, \"platform\": ..., "
            "\"title\": ..., \"price_inr\": ..., \"per_unit\": ..., "
            "\"url\": ..., \"note\": ...}}, ...], \"chat_id\": {chat_id}}}"
        ),
        expected_output="A JSON object with a 'picks' array and the chat_id.",
        agent=comparator,
        context=[scan_task],
    )

    notify_task = Task(
        description=(
            "Compose a Telegram message from the picks in the previous task and "
            "send it via 'Send Telegram reply'.\n\n"
            "Use the chat_id from the previous task's output (it'll be {chat_id}).\n\n"
            "Message format (plain text, under 1000 chars):\n"
            "  🛒 Results for: <original query>\n\n"
            "  🥇 <Platform> — <Title>\n"
            "  ₹<price> (<per_unit if known>)\n"
            "  <url>\n\n"
            "  🥈 ... (same shape)\n"
            "  🥉 ... (same shape)\n\n"
            "  <optional one-line note about quality/brand tradeoffs>\n\n"
            "If the picks list is empty, send: 'No usable results across "
            "platforms for that query — try rephrasing?'\n\n"
            "Then return the tool's status string verbatim as your final answer."
        ),
        expected_output="The Telegram send-tool's confirmation string.",
        agent=responder,
        context=[compare_task],
    )

    return Crew(
        agents=[normaliser, scout, comparator, responder],
        tasks=[normalise_task, scan_task, compare_task, notify_task],
        process=Process.sequential,
        verbose=True,
    )
