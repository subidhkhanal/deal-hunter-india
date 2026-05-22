"""CrewAI tools: 4 grocery-platform scrapers (via Playwright) + Telegram reply.

All scrapers follow the same recipe:
  1. Launch a fresh headless Chromium via Playwright
  2. Navigate to the platform's search URL
  3. Intercept JSON responses that look like product results
  4. Pluck title/price/quantity/url/in_stock from the snippets
  5. Close the browser, return JSON string

Reference: github.com/KshKnsl/QuickCom — Node+Puppeteer implementation that
established the URL patterns and the response.snippets shape for Blinkit,
Zepto, and Instamart. BigBasket figured out separately from probing.

All tools return strings (CrewAI contract); errors come back as
{"error": "..."} JSON, never raised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any

import requests
from crewai.tools import tool

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
NAV_TIMEOUT_MS = 45_000
WAIT_AFTER_NAV_MS = 3_000
MAX_RESULTS = 8


def _safe_int_price(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    # Strings like "₹1,250" or "1250" or "1250.50"
    s = re.sub(r"[^\d.]", "", str(raw))
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _extract_blinkit_products(payloads: list[dict]) -> list[dict]:
    """Blinkit returns products under response.snippets in each search response."""
    out: list[dict] = []
    for payload in payloads:
        snippets = (payload.get("response") or {}).get("snippets") or []
        if not isinstance(snippets, list):
            continue
        for s in snippets:
            product = _blinkit_snippet_to_product(s)
            if product:
                out.append(product)
    return out


def _extract_zepto_products(payloads: list[dict]) -> list[dict]:
    """Zepto returns products under layout[].data.resolver.data.items[].productResponse.
    Prices are in paise (multiply by 0.01 to get INR)."""
    out: list[dict] = []
    for payload in payloads:
        layout = payload.get("layout") or []
        if not isinstance(layout, list):
            continue
        for widget in layout:
            wname = widget.get("widgetName", "")
            if "SEARCHED_PRODUCTS" not in wname:
                continue
            data = widget.get("data") or {}
            resolver_data = (data.get("resolver") or {}).get("data") or {}
            items = resolver_data.get("items") or data.get("items") or []
            if not isinstance(items, list):
                continue
            for item in items:
                product = _zepto_item_to_product(item)
                if product:
                    out.append(product)
    return out


def _zepto_item_to_product(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    pr = item.get("productResponse") or {}
    if not pr:
        return None
    product_data = pr.get("product") or {}
    variant = pr.get("productVariant") or {}
    name = product_data.get("name")
    if not name:
        return None
    price_paise = pr.get("discountedSellingPrice")
    mrp_paise = pr.get("mrp")
    price_inr = int(price_paise / 100) if isinstance(price_paise, (int, float)) and price_paise else None
    mrp_inr = int(mrp_paise / 100) if isinstance(mrp_paise, (int, float)) and mrp_paise else None
    qty = variant.get("formattedPacksize")
    out_of_stock = bool(pr.get("outOfStock"))
    product_id = pr.get("id")
    return {
        "platform": "zepto",
        "title": str(name)[:200],
        "price_inr": price_inr,
        "mrp_inr": mrp_inr,
        "quantity": str(qty)[:60] if qty else None,
        "url": _build_product_url("zepto", product_id, name),
        "image_url": None,  # Zepto images need CDN prefix; skip for v1
        "in_stock": not out_of_stock,
    }


def _blinkit_snippet_to_product(snippet: dict) -> dict | None:
    """Best-effort mapping from a Blinkit snippet dict to our standard product shape."""
    if not isinstance(snippet, dict):
        return None
    data = snippet.get("data") or {}
    identity = data.get("identity") or {}
    # Skip non-product containers (banners, headers, etc.)
    if identity.get("id") == "product_container":
        return None
    name = data.get("name") or {}
    name_text = name.get("text") if isinstance(name, dict) else name
    if not name_text:
        return None
    # Prices are wrapped in dicts like {"text": "₹150"} or plain strings/numbers
    def _get_text(field: Any) -> Any:
        if isinstance(field, dict):
            return field.get("text") or field.get("value")
        return field

    price = _safe_int_price(_get_text(data.get("normal_price") or data.get("final_price") or data.get("price")))
    mrp = _safe_int_price(_get_text(data.get("mrp")))
    qty = _get_text(data.get("variant") or data.get("weight"))
    image_url = _get_text(data.get("image"))
    out_of_stock = bool(data.get("is_sold_out") or data.get("out_of_stock"))
    product_id = identity.get("id") or data.get("product_id")

    return {
        "platform": "blinkit",
        "title": str(name_text)[:200],
        "price_inr": price,
        "mrp_inr": mrp,
        "quantity": str(qty)[:60] if qty else None,
        "url": _build_product_url("blinkit", product_id, name_text),
        "image_url": str(image_url)[:300] if image_url else None,
        "in_stock": not out_of_stock,
    }


def _build_product_url(platform: str, product_id: Any, name: str) -> str | None:
    if not product_id:
        return None
    pid = str(product_id)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(name)).strip("-").lower()[:60]
    if platform == "blinkit":
        return f"https://blinkit.com/prn/{slug}/prid/{pid}"
    if platform == "zepto":
        return f"https://www.zeptonow.com/pn/{slug}/pvid/{pid}"
    if platform == "instamart":
        return f"https://www.swiggy.com/instamart/item/{pid}"
    if platform == "bigbasket":
        return f"https://www.bigbasket.com/pd/{pid}/"
    return None


def _run_playwright_capture(
    search_url: str,
    response_url_match: str,
    use_stealth: bool = False,
) -> list[dict]:
    """Launch headless Chromium, navigate to search_url, intercept JSON
    responses whose URL contains response_url_match, return list of parsed JSON
    payloads. Returns empty list on any failure (caller handles)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    captured_payloads: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-IN",
            )
            page = context.new_page()

            if use_stealth:
                try:
                    from playwright_stealth import stealth_sync
                    stealth_sync(page)
                except ImportError:
                    pass

            def on_response(response):
                try:
                    if response_url_match in response.url and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct.lower():
                            body = response.json()
                            captured_payloads.append(body)
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(search_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass

            page.wait_for_timeout(WAIT_AFTER_NAV_MS)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return captured_payloads


def _run_playwright_dom_extract(
    search_url: str,
    extractor_fn,
    use_stealth: bool = True,
) -> list[dict]:
    """Sister of _run_playwright_capture, but for sites that render products
    into the DOM without exposing a JSON XHR (Flipkart). Launches a Chromium,
    navigates, then hands the loaded page to extractor_fn which returns the
    products directly."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    products: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-IN",
            )
            page = context.new_page()
            if use_stealth:
                try:
                    from playwright_stealth import Stealth
                    Stealth().apply_stealth_sync(page)
                except ImportError:
                    pass

            try:
                page.goto(search_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            page.wait_for_timeout(WAIT_AFTER_NAV_MS)

            try:
                products = extractor_fn(page) or []
            except Exception as exc:
                log.warning("DOM extractor raised: %s", exc)
                products = []
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return products


# Flipkart innertext glues price/MRP/discount with no spaces, e.g.
# "₹688₹81015% off" really means price=688, MRP=810, discount=15%.
# Split MRP from the trailing discount digits by anchoring on "% off"
# and using non-greedy capture on MRP.
FLIPKART_PRICE_PAIR_RE = re.compile(r"₹([\d,]+)\s*₹([\d,]+?)(\d{1,2})\s*%\s*off")
FLIPKART_SINGLE_PRICE_RE = re.compile(r"₹\s*([\d,]+)")
FLIPKART_QTY_RE = re.compile(r"\b(\d+(?:\.\d+)?\s*(?:kg|g|ml|l|ltr|litre|liter|piece|pack|pcs|gm|gms|grams?))\b", re.IGNORECASE)


def _extract_flipkart_dom(page) -> list[dict]:
    """Extract product cards from a loaded Flipkart search page.

    Selectors verified on flipkart.com/search?q=milk in 2026:
    - Card:    div[data-id]                 (40 cards on a typical SERP)
    - Title:   img[alt] inside the card     (full product name; visible text
               is truncated with '...')
    - Link:    a[href*='/p/']               (Flipkart product pages all match)
    - Price:   innerText pattern '₹X₹Y Z% off' — first ₹ = current price,
               second ₹ = MRP. If only one ₹ appears, it's the current price.
    - Qty:     regex over innerText for 'NN kg|g|ml|l|piece'
    """
    out: list[dict] = []
    cards = page.locator("div[data-id]").all()
    for card in cards:
        if len(out) >= MAX_RESULTS:
            break
        try:
            data_id = card.get_attribute("data-id") or ""
            # Title via image alt (full name, not truncated)
            try:
                title = card.locator("img[alt]").first.get_attribute("alt", timeout=1500) or ""
            except Exception:
                title = ""
            if not title:
                continue
            # Link
            href = ""
            try:
                href = card.locator("a[href*='/p/']").first.get_attribute("href", timeout=1500) or ""
            except Exception:
                pass
            url = ("https://www.flipkart.com" + href) if href.startswith("/") else (href or None)

            # Price + MRP from inner text
            text = card.inner_text(timeout=2000) or ""
            price_inr: int | None = None
            mrp_inr: int | None = None
            pair = FLIPKART_PRICE_PAIR_RE.search(text)
            if pair:
                price_inr = _safe_int_price(pair.group(1))
                mrp_inr = _safe_int_price(pair.group(2))
            else:
                single = FLIPKART_SINGLE_PRICE_RE.search(text)
                if single:
                    price_inr = _safe_int_price(single.group(1))
            if price_inr is None:
                continue  # no price → not a real product card
            # Sanity: MRP must be > price. Otherwise our regex mis-split the
            # ₹X₹YZ%off pattern and the "MRP" is garbage. Drop it.
            if mrp_inr is not None and (mrp_inr <= price_inr or mrp_inr > price_inr * 20):
                mrp_inr = None

            qty_match = FLIPKART_QTY_RE.search(text)
            qty = qty_match.group(1) if qty_match else None

            out.append({
                "platform": "flipkart",
                "title": str(title)[:200],
                "price_inr": price_inr,
                "mrp_inr": mrp_inr,
                "quantity": str(qty)[:60] if qty else None,
                "url": url,
                "image_url": None,
                "in_stock": True,
            })
        except Exception:
            continue
    return out


def _dedupe(products: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in products:
        key = (p.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= MAX_RESULTS:
            break
    return out


def _search_with(
    platform: str,
    query: str,
    search_url: str,
    response_match: str,
    extractor,
    use_stealth: bool = False,
) -> str:
    if not query or not query.strip():
        return json.dumps({"error": "empty query"})
    try:
        payloads = _run_playwright_capture(search_url, response_match, use_stealth=use_stealth)
    except Exception as exc:
        log.warning("%s capture failed: %s", platform, exc)
        return json.dumps({"error": f"{platform} fetch failed: {exc}"})
    products = _dedupe(extractor(payloads))
    if not products:
        return json.dumps({"error": f"no {platform} results for {query!r}"})
    return json.dumps(products, ensure_ascii=False)


@tool("Search Blinkit for a product")
def search_blinkit(query: str) -> str:
    """Search Blinkit. Returns JSON list of products or {"error": "..."}."""
    q = urllib.parse.quote_plus(query.strip())
    return _search_with(
        platform="blinkit",
        query=query,
        search_url=f"https://blinkit.com/s/?q={q}",
        response_match="/v1/layout/search",
        extractor=_extract_blinkit_products,
    )


@tool("Search Zepto for a product")
def search_zepto(query: str) -> str:
    """Search Zepto. Returns JSON list of products or {"error": "..."}."""
    q = urllib.parse.quote_plus(query.strip())
    return _search_with(
        platform="zepto",
        query=query,
        search_url=f"https://www.zeptonow.com/search?query={q}",
        # Filter out the /search/filters response (only want product results)
        response_match="user-search-service/api/v3/search",
        extractor=lambda payloads: _extract_zepto_products(
            [p for p in payloads if isinstance(p, dict) and "layout" in p]
        ),
    )


@tool("Search Flipkart for a product")
def search_flipkart(query: str) -> str:
    """Search Flipkart's general catalog via DOM scraping (no JSON XHR).
    Returns JSON list or {"error": "..."}. Flipkart results can be broader
    than groceries (a 'milk' search may surface protein shakes); the
    Comparator agent filters off-topic items."""
    if not query or not query.strip():
        return json.dumps({"error": "empty query"})
    q = urllib.parse.quote_plus(query.strip())
    url = f"https://www.flipkart.com/search?q={q}"
    try:
        products = _run_playwright_dom_extract(url, _extract_flipkart_dom, use_stealth=True)
    except Exception as exc:
        log.warning("flipkart fetch failed: %s", exc)
        return json.dumps({"error": f"flipkart fetch failed: {exc}"})
    products = _dedupe(products)
    if not products:
        return json.dumps({"error": f"no flipkart results for {query!r}"})
    return json.dumps(products, ensure_ascii=False)


@tool("Search Swiggy Instamart for a product")
def search_instamart(query: str) -> str:
    """Search Swiggy Instamart. Currently disabled — Swiggy's AWS WAF blocks
    headless browsers even with playwright-stealth. Returns an error sentinel.
    To enable: add a paid scraping service (e.g. ScraperAPI) or run on residential IPs."""
    return json.dumps({"error": "instamart blocked by AWS WAF — disabled in v1"})


@tool("Search BigBasket for a product")
def search_bigbasket(query: str) -> str:
    """Search BigBasket. Currently disabled — Akamai blocks headless browsers
    even with playwright-stealth. Returns an error sentinel.
    To enable: add a paid scraping service (e.g. ScraperAPI) or run on residential IPs."""
    return json.dumps({"error": "bigbasket blocked by Akamai — disabled in v1"})


@tool("Send Telegram reply")
def send_telegram_reply(chat_id: int, text: str) -> str:
    """Send a Telegram message to the given chat_id. Returns a status string;
    never raises — missing env or API errors come back as 'ERROR: ...'."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return "ERROR: missing env var: TELEGRAM_BOT_TOKEN"
    if not chat_id:
        return "ERROR: missing chat_id"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(chat_id),
        "text": text[:3900],  # Telegram limit is 4096, leave headroom
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("description", resp.text)
            except ValueError:
                detail = resp.text
            return f"ERROR: Telegram HTTP {resp.status_code}: {detail}"
        message_id = resp.json().get("result", {}).get("message_id")
        return f"Sent (message_id={message_id})"
    except requests.RequestException as exc:
        return f"ERROR: {exc}"
