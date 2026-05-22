# Grocery Price Hunter India

An interactive Telegram bot that takes a grocery query, searches three Indian e-commerce platforms in parallel (Blinkit, Zepto, Flipkart) via headless Playwright, and replies with the best 3 picks ranked by per-unit price + brand quality.

```
   User on Telegram: "chicken 1kg"
                  │
                  ▼
            Query Normaliser     (LLM: clean typos, expand units)
                  │
                  ▼
                Scout            (calls 5 platform tools in parallel)
       ┌──────────┬──────────┬──────────┬──────────┐
       ▼          ▼          ▼          ▼          ▼
    Blinkit    Zepto    Flipkart   Instamart   BigBasket
      ✅          ✅          ✅           ❌           ❌
       └──────────┴──────────┴──────────┴──────────┘
                  ▼
            Comparator           (LLM: normalises ₹/kg, ranks)
                  │
                  ▼
             Responder           (formats Telegram message + sends)
                  │
                  ▼
        Bot replies: top 3 picks with links
```

## Platform status

| Platform | State | Notes |
|---|---|---|
| Blinkit | ✅ Working | Plain Playwright, JSON-response interception |
| Zepto | ✅ Working | Plain Playwright, layout-array JSON shape |
| Flipkart | ✅ Working | Stealth + DOM scraping via `div[data-id]`. General catalog — a "milk" search returns shakes/paneer alongside actual milk; the Comparator filters off-topic items. |
| BigBasket | ❌ Blocked | Akamai "Access Denied" — playwright-stealth doesn't bypass it. Disabled in v1. |
| Instamart | ❌ Blocked | AWS WAF "Something went wrong" — same. Disabled in v1. |

The Comparator works with whatever scouts succeeded — partial coverage still produces useful answers.

To enable BigBasket / Instamart later, add a paid scraping service (e.g. [ScraperAPI](https://www.scraperapi.com/)) and rewrite the two stub tools in [tools.py](tools.py) to route through it.

---

## Setup

```bash
git clone <this repo>
cd grocery-price-hunter-india
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
playwright install chromium     # ~150MB one-time download
```

## API keys you need

### 1. OpenRouter (free)
- Go to https://openrouter.ai/keys
- Sign in (Google/GitHub/email — no credit card)
- "Create Key" → copy → paste into `.env` as `OPENROUTER_API_KEY=sk-or-v1-...`

### 2. Telegram bot
- Open Telegram, search for **@BotFather**
- Send `/newbot` → pick a name → pick a username ending in `bot`
- BotFather replies with your token like `8123456789:AAFnQ_zaOQX3_xxxx`
- Copy → paste into `.env` as `TELEGRAM_BOT_TOKEN=...`
- **Open your bot's chat and send any message** (Telegram requires the user to message first before the bot can reply)

## Configure

```bash
cp .env.example .env
# Then edit .env and fill in:
#   OPENROUTER_API_KEY   from https://openrouter.ai/keys
#   TELEGRAM_BOT_TOKEN   from @BotFather
#   USER_PINCODE         your 6-digit pincode (informational for now)
```

## Smoke test (no LLM tokens consumed, no Telegram message sent)

```bash
python verify.py
```

You should see 6 `OK` lines:
- imports clean
- blinkit returned N products
- zepto returned N products
- bigbasket & instamart return clean error sentinels
- crew shape OK
- telegram tool returns ERROR string when env missing

If Blinkit or Zepto fails, they may have updated their HTML — check [tools.py](tools.py) `_extract_blinkit_products` / `_extract_zepto_products`.

## Run

```bash
python main.py
```

The bot starts and polls Telegram. Leave it running. From your phone:

1. Open your bot in Telegram
2. Type any grocery query: `milk`, `chicken 1kg`, `atta 5kg`, `eggs`, `chiken` (typo)
3. The bot replies "🔎 Searching..." immediately, then ~60-90 seconds later sends the top 3 picks

Stop the bot with `Ctrl+C`.

## How it works (short version)

Each Telegram message kicks off a CrewAI sequential pipeline:

1. **Normaliser** (LLM): fixes typos, expands units. `chiken 1kg` → `chicken 1 kg`.
2. **Scout** (4 tools): calls Blinkit, Zepto, Instamart, BigBasket search tools. Each launches a headless Playwright Chromium, navigates to the platform's search URL, intercepts the JSON response the platform's own frontend fetches, and extracts product data. Two of these currently return error sentinels (WAF blocks); the other two return real product lists.
3. **Comparator** (LLM): merges results across platforms, computes per-unit price where it can (₹/kg, ₹/litre), preferring known Indian brands (Amul, Aashirvaad, etc) at similar price points.
4. **Responder** (LLM + tool): formats top 3 picks into a plain-text Telegram message and sends via the Bot API.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: missing env var: TELEGRAM_BOT_TOKEN` | `.env` not loaded or value blank | Confirm `.env` exists in project root, no `.env.txt` typo, value isn't blank |
| `ERROR: Telegram HTTP 401: Unauthorized` | Token wrong | Re-copy from BotFather (`/mybots` → bot → API Token) |
| `ERROR: Telegram HTTP 400: chat not found` | You haven't messaged the bot first | Send any message to your bot in Telegram, then retry |
| Bot starts but doesn't reply to queries | OpenRouter rate-limited; free-tier models share quotas globally | Change `LLM_MODEL` in `.env` to a less-busy model (see `.env.example` comments) |
| `bigbasket: Access Denied` / `instamart: Something went wrong` in logs | Expected — both platforms block headless browsers | These are disabled in v1; ignore. To enable, see "Platform status" above |
| Bot replies "No usable results for that query" | Both Blinkit and Zepto failed (rare) or query is nonsense | Try a more specific query. If common items also fail, the platforms may have updated their HTML |
| Verify says `blinkit returned 0 products` | Blinkit changed their search response shape | Open `https://blinkit.com/s/?q=milk` in DevTools → Network → look for the JSON response with `response.snippets`. Compare with `_extract_blinkit_products` in [tools.py](tools.py) |

## Where to take it next

- **Stealth / paid scrapers**: enable BigBasket + Instamart via [ScraperAPI](https://www.scraperapi.com/) or [Bright Data](https://brightdata.com/) for ~$30/month. Replace the stub `search_*` functions with HTTP calls to the proxy.
- **Browser reuse**: the current code launches a fresh Chromium per platform per query (~10s overhead × 2 = 20s). Pool a single browser across queries to cut that.
- **Persistent pincode**: capture the pincode-set cookies once in a setup script, replay them on each search so location is correct.
- **Add a third source**: Flipkart's regular search (with stealth) returns products but with broader catalog match — useful for non-grocery items.
- **Result caching**: store query → top picks for 10 minutes; identical follow-up queries reply instantly.
- **Multi-user**: the bot already replies to whoever messaged it; add user pincode storage per chat_id.

## Credits

- Reference implementations that informed the platform scrapers:
  - [KshKnsl/QuickCom](https://github.com/KshKnsl/QuickCom) — Node+Puppeteer, established the JSON-intercept approach
  - [saliniyan/e-commerce-web-scarpping](https://github.com/saliniyan/e-commerce-web-scarpping) — Python+Selenium, confirmed URL patterns
- Agent framework: [CrewAI](https://docs.crewai.com/)
- LLM provider: [OpenRouter](https://openrouter.ai/) (free tier)
- Browser automation: [Playwright](https://playwright.dev/python/)
