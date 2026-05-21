"""Telegram long-poll bot: listen for messages, run the price-comparison crew,
reply with the top 3 picks.

Stays running until Ctrl+C. No webhooks, no public URL needed.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv


def _telegram_get_updates(token: str, offset: int, timeout: int = 30) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'}
    # Long-poll timeout is server-side; the HTTP read timeout must be longer.
    resp = requests.get(url, params=params, timeout=timeout + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates not ok: {data}")
    return data.get("result", [])


def _send_simple(token: str, chat_id: int, text: str) -> None:
    """Direct send bypassing the crew — used for ack and error replies."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:3900]},
            timeout=15,
        )
    except requests.RequestException:
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set. Edit .env.", file=sys.stderr)
        return 1
    if not os.getenv("GROQ_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
        print("ERROR: need at least one of GROQ_API_KEY or OPENROUTER_API_KEY in .env.", file=sys.stderr)
        return 1
    if not os.getenv("GROQ_API_KEY"):
        print("WARNING: GROQ_API_KEY not set; will use OpenRouter only (slower, less reliable).", file=sys.stderr)
    if not os.getenv("OPENROUTER_API_KEY"):
        print("WARNING: OPENROUTER_API_KEY not set; no fallback when Groq rate-limits.", file=sys.stderr)
    if not os.getenv("USER_PINCODE"):
        print("WARNING: USER_PINCODE is not set; platforms may return location-default results.", file=sys.stderr)

    # Import here so a missing API key fails fast before we incur the CrewAI startup cost
    from crew_setup import build_crew

    crew = build_crew()
    log = logging.getLogger("bot")
    log.info("Bot started, polling Telegram...")

    offset = 0
    while True:
        try:
            updates = _telegram_get_updates(token, offset, timeout=30)
        except requests.RequestException as exc:
            log.warning("Telegram poll failed (%s); retrying in 5s", exc)
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            log.info("Interrupted by user, exiting.")
            return 0

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = (msg.get("text") or "").strip()
            if not chat_id or not text:
                continue
            log.info("Query from chat_id=%s: %r", chat_id, text)
            _send_simple(token, chat_id, f"🔎 Searching across platforms for: {text}\n(takes ~30-60s)")

            try:
                crew.kickoff(inputs={"query": text, "chat_id": chat_id})
            except Exception as exc:
                log.exception("Crew kickoff failed")
                _send_simple(token, chat_id, f"Sorry, that broke: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
