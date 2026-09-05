import asyncio
import io
import logging
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, InvalidSessionIdException
from webdriver_manager.chrome import ChromeDriverManager

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "900"))

# Headless Chrome tends to crash/leak memory after running unattended for
# a while (this is what caused "InvalidSessionIdException: session deleted
# as the browser has closed the connection" after ~10 cycles). Restart the
# driver preventively every N cycles, and reactively any time it's found
# to have died, instead of letting one crash kill the whole script.
DRIVER_RESTART_EVERY_N_CYCLES = 15

# Generate this under your lzt.market account settings → API. Requests to
# api.lzt.market are unauthenticated without it, which is why every field
# was silently coming back "N/A" — the API was rejecting the call, not
# returning empty data.
LZT_API_TOKEN = os.environ.get("LZT_API_TOKEN", "")

# Where persistent state (seen-accounts cache + saved images) lives.
# Point this at a mounted Fly volume (e.g. /data) so it survives
# redeploys and restarts -- the container filesystem itself is ephemeral.
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID environment "
        "variables. Set them as Fly secrets (fly secrets set "
        "TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...) or in your local "
        "environment before running."
    )

# Only origins in this list are allowed through. Anything scraped with an
# origin NOT in this set (phishing / stealer / brute / social-engineering /
# unknown) is discarded before an alert is ever sent.
ALLOWED_ORIGINS = {"original", "self-registered", "N/A", "n/a"}

FILTERS = [
    "https://lzt.market/riot?pmax=11&weaponSkin[]=4f5ee03a-4204-5526-6941-bca4f911a768&order_by=price_to_up",
    "https://lzt.market/riot?pmax=10&weaponSkin[]=4f5ee03a-4204-5526-6941-bca4f911a768&order_by=price_to_up",
    "https://lzt.market/riot?pmax=8&weaponSkin[]=4f5ee03a-4204-5526-6941-bca4f911a768&order_by=price_to_up",
    "https://lzt.market/riot?pmax=5&weaponSkin[]=4f5ee03a-4204-5526-6941-bca4f911a768&order_by=price_to_up",
    "https://lzt.market/riot?pmax=12&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=price_to_up",
    "https://lzt.market/riot?pmax=10&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=price_to_up",
    "https://lzt.market/riot?pmax=9&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=price_to_up",
    "https://lzt.market/riot?pmax=7&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=price_to_up",
    "https://lzt.market/riot?pmax=5&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=price_to_up",
    "https://lzt.market/riot?pmax=6&weaponSkin[]=000ad7b1-44b0-9345-ea47-9cbd7dcdbb38&order_by=price_to_up",
    "https://lzt.market/riot?pmax=8&weaponSkin[]=000ad7b1-44b0-9345-ea47-9cbd7dcdbb38&order_by=price_to_up",
]

SEEN_FILE = DATA_DIR / "seen_accounts.json"
IMAGE_DIR = DATA_DIR / "account_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lzt_sniper")

# ─── DATA ─────────────────────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    account_id:     str
    url:            str
    price:          str = "N/A"
    account_type:   str = "N/A"
    rank:           str = "N/A"
    last_rank:      str = "N/A"
    prev_season:    str = "N/A"
    level:          str = "N/A"
    vp:             str = "N/A"
    radiant_pts:    str = "N/A"
    knives:         str = "N/A"
    locale:         str = "N/A"
    free_agents:    str = "N/A"
    last_activity:  str = "N/A"
    email_linked:   str = "N/A"
    country:        str = "N/A"
    phone_linked:   str = "N/A"
    email_domain:   str = "N/A"
    account_origin: str = "N/A"
    image_path:     Optional[str] = None       # kept for backwards-compat (first image)
    image_paths:    list          = field(default_factory=list)
    filter_url:     str = ""
    seen_at:        str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

# ─── PERSISTENCE ──────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── SELENIUM ─────────────────────────────────────────────────────────────────

def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # In the Docker/Fly.io image, Chromium + chromedriver are installed by
    # apt at fixed paths and CHROME_BIN / CHROMEDRIVER_PATH are set in the
    # Dockerfile -- use those directly rather than letting webdriver_manager
    # try to download a chromedriver at runtime (slower, and can pick a
    # version that doesn't match the apt-installed Chromium). Locally
    # (e.g. on Windows), those env vars won't be set, so it falls back to
    # the original auto-download behavior.
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        opts.binary_location = chrome_bin

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

def driver_is_alive(driver: webdriver.Chrome) -> bool:
    """Cheap check that the underlying Chrome process/session is still
    responding, without navigating anywhere."""
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False

def restart_driver(old_driver: webdriver.Chrome) -> webdriver.Chrome:
    try:
        old_driver.quit()
    except Exception:
        pass  # already dead — nothing to clean up
    log.warning("🔄 Restarting Chrome driver")
    return build_driver()

def selenium_get_html(driver: webdriver.Chrome, url: str, timeout: int = 20) -> str:
    driver.get(url)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href]"))
        )
    except Exception:
        pass
    time.sleep(2.5)
    return driver.page_source

# ─── LISTING PARSER ───────────────────────────────────────────────────────────

_LISTING_ID_RE = re.compile(r"^/(\d{6,9})/?$")

PRICE_SELECTORS = [
    ".price-block__price",
    ".price__number",
    "[class*='price_val']",
    "[class*='price-val']",
    "[class*='item-price']",
    "[class*='cost']",
    "[class*='price']",
]

def parse_listings(html: str, filter_url: str) -> list[dict]:
    soup     = BeautifulSoup(html, "html.parser")
    items:    list[dict] = []
    seen_ids: set        = set()

    # ── Strategy 1: data-item-id ──
    for card in soup.select("[data-item-id]"):
        raw_id = card.get("data-item-id", "").strip()
        if not raw_id or not raw_id.isdigit() or raw_id in seen_ids:
            continue
        seen_ids.add(raw_id)

        price = "N/A"
        for sel in PRICE_SELECTORS:
            tag = card.select_one(sel)
            if tag:
                price = tag.get_text(strip=True)
                break

        items.append({"id": raw_id, "price": price, "filter_url": filter_url})

    if items:
        log.info(f"[data-item-id] hit → {len(items)} listing(s)")
        return items

    # ── Strategy 2: anchor grep inside container ──
    CONTAINER_SELECTORS = [
        ".market-items", ".items-list", "#market-items",
        "[class*='items-list']", "[class*='market-list']", "main",
    ]

    container = None
    for sel in CONTAINER_SELECTORS:
        container = soup.select_one(sel)
        if container:
            log.info(f"Container found via '{sel}'")
            break

    search_root = container if container else soup

    for a in search_root.select("a[href]"):
        href = a.get("href", "").split("?")[0].rstrip("/")
        path = re.sub(r"^https?://[^/]+", "", href)
        m    = _LISTING_ID_RE.match(path) or _LISTING_ID_RE.match(path + "/")
        if not m:
            continue

        acc_id = m.group(1)
        if acc_id in seen_ids:
            continue
        seen_ids.add(acc_id)

        price = "N/A"
        card  = a.find_parent(attrs={"class": re.compile(r"item|card|listing", re.I)})
        if card:
            for sel in PRICE_SELECTORS:
                tag = card.select_one(sel)
                if tag:
                    price = tag.get_text(strip=True)
                    break

        items.append({"id": acc_id, "price": price, "filter_url": filter_url})

    log.info(f"Anchor-grep → {len(items)} listing(s)")
    return items

# ─── STATS PARSER ─────────────────────────────────────────────────────────────

TYPE_KEYWORDS = [
    "resale", "phishing", "stealer", "stealer log",
    "original", "temporarily", "full access", "brute",
    "self-registered", "social engineering",
]

TYPE_SELECTORS = [
    "[class*='account-type']", "[class*='item-type']",
    "[class*='label']", "[class*='tag']", "[class*='badge']",
    "[class*='warn']", "[class*='notice']", "[class*='alert']",
    "[class*='info-block']", "span", "div", "li",
]

LABEL_KEY_MAP = {
    "inventory value":      "vp",
    "valorant points":      "vp",
    "vp":                   "vp",
    "radiant points":       "radiant_pts",
    "free agents":          "free_agents",
    "current rank":         "rank",
    "last rank":            "last_rank",
    "previous season rank": "prev_season",
    "level":                "level",
    "knives":               "knives",
    "locale":               "locale",
}

RANK_NAMES = [
    "iron", "bronze", "silver", "gold", "platinum",
    "diamond", "ascendant", "immortal", "radiant",
    "ranked ready", "unrated", "no rank",
]

def parse_stats(html: str) -> dict:
    soup  = BeautifulSoup(html, "html.parser")
    stats = {
        "rank": "N/A", "last_rank": "N/A", "prev_season": "N/A",
        "level": "N/A", "vp": "N/A", "radiant_pts": "N/A",
        "knives": "N/A", "locale": "N/A", "free_agents": "N/A",
        "price": "N/A", "account_type": "N/A",
        "last_activity": "N/A", "email_linked": "N/A",
        "country": "N/A", "phone_linked": "N/A",
        "email_domain": "N/A", "account_origin": "N/A",
    }

    # ── Price ──
    for sel in PRICE_SELECTORS:
        tag = soup.select_one(sel)
        if tag:
            raw = tag.get_text(strip=True)
            if re.search(r"\d", raw):
                stats["price"] = raw
                break

    # ── Account type ──
    for sel in TYPE_SELECTORS:
        for el in soup.select(sel):
            txt = el.get_text(strip=True).lower()
            for kw in TYPE_KEYWORDS:
                if kw in txt and len(el.get_text(strip=True)) < 60:
                    stats["account_type"] = el.get_text(strip=True)
                    break
            if stats["account_type"] != "N/A":
                break
        if stats["account_type"] != "N/A":
            break

    # ── Stats grid DOM traversal ──
    for el in soup.find_all(["span", "div", "p", "td", "dt"]):
        text = el.get_text(strip=True).lower()
        matched_key = None
        for label, key in LABEL_KEY_MAP.items():
            if text == label or text.startswith(label):
                matched_key = key
                break
        if not matched_key:
            continue

        value = "N/A"

        prev = el.find_previous_sibling()
        if prev:
            v = prev.get_text(strip=True)
            if v and v.lower() not in LABEL_KEY_MAP and len(v) < 60:
                value = v

        if value == "N/A":
            parent = el.find_parent()
            if parent:
                for child in parent.children:
                    if not hasattr(child, "get_text") or child == el:
                        continue
                    v = child.get_text(strip=True)
                    if v and v.lower() not in LABEL_KEY_MAP and len(v) < 60:
                        value = v
                        break

        if value == "N/A":
            nxt = el.find_next_sibling()
            if nxt:
                v = nxt.get_text(strip=True)
                if v and v.lower() not in LABEL_KEY_MAP and len(v) < 60:
                    value = v

        if value != "N/A":
            stats[matched_key] = value

    # ── Line-by-line fallback ──
    full_text = soup.get_text(separator="\n")
    lines     = [l.strip() for l in full_text.splitlines() if l.strip()]

    for i, line in enumerate(lines):
        ll = line.lower()

        if stats["rank"] == "N/A" and any(r in ll for r in RANK_NAMES):
            if "current" not in ll and "last" not in ll and "previous" not in ll:
                stats["rank"] = line

        if stats["last_rank"] == "N/A":
            if i > 0 and "last rank" in lines[i - 1].lower():
                stats["last_rank"] = line
            elif i < len(lines) - 1 and "last rank" in lines[i + 1].lower():
                stats["last_rank"] = line

        if stats["prev_season"] == "N/A":
            if i > 0 and "previous season" in lines[i - 1].lower():
                stats["prev_season"] = line
            elif i < len(lines) - 1 and "previous season" in lines[i + 1].lower():
                stats["prev_season"] = line

        if stats["level"] == "N/A":
            if i > 0 and lines[i - 1].strip().lower() == "level":
                if re.match(r"^\d+$", line):
                    stats["level"] = line

        if stats["vp"] == "N/A":
            if i > 0 and any(k in lines[i - 1].lower() for k in ["valorant points", "inventory value"]):
                if re.match(r"^[\d,\.]+$", line.replace(" ", "")):
                    stats["vp"] = line

        if stats["radiant_pts"] == "N/A":
            if i > 0 and "radiant points" in lines[i - 1].lower():
                if re.match(r"^[\d,\.]+$", line.replace(" ", "")):
                    stats["radiant_pts"] = line

        if stats["locale"] == "N/A":
            if i > 0 and lines[i - 1].strip().lower() == "locale":
                if len(line) < 30:
                    stats["locale"] = line

        if stats["knives"] == "N/A":
            if i > 0 and lines[i - 1].strip().lower() == "knives":
                if re.match(r"^\d+$", line):
                    stats["knives"] = line

        if stats["free_agents"] == "N/A":
            if i > 0 and "free agent" in lines[i - 1].lower():
                if re.match(r"^\d+$", line):
                    stats["free_agents"] = line

        if stats["price"] == "N/A":
            if i > 0 and any(k in lines[i - 1].lower() for k in ["price", "cost", "buy"]):
                if re.search(r"\d", line) and len(line) < 20:
                    stats["price"] = line

        if stats["account_type"] == "N/A":
            for kw in TYPE_KEYWORDS:
                if kw in ll and len(line) < 60:
                    stats["account_type"] = line
                    break

    return stats

RELIABLE_LABEL_MAP = {
    "last activity":  "last_activity",
    "email linked":   "email_linked",
    "country":        "country",
    "phone linked":   "phone_linked",
    "email domain":   "email_domain",
    "account origin": "account_origin",
}

def parse_reliable_info(html: str) -> dict:
    """
    Scrapes the 'Reliable information (parsed automatically)' block using
    the confirmed DOM structure:

      div.marketItemView--counters
        > div.counter
            > div.label   (the value,  e.g. "Resale (Stealer)")
            > div.muted   (the field name, e.g. "Account origin")

    No API / paid Market access required.
    """
    soup   = BeautifulSoup(html, "html.parser")
    result = {v: "N/A" for v in RELIABLE_LABEL_MAP.values()}

    # The page has TWO separate blocks sharing this class — one for game
    # stats (VP, rank, level...) and one for reliable info (origin,
    # country, email...). We need to scan every matching container, not
    # just the first, or the reliable-info one gets missed entirely.
    containers   = soup.select(".marketItemView--counters")
    search_roots = containers if containers else [soup]

    for search_root in search_roots:
        for counter in search_root.select(".counter"):
            label_el = counter.select_one(".label")
            muted_el = counter.select_one(".muted")
            if not label_el or not muted_el:
                continue

            # Normalize whitespace (nbsp, double spaces, etc.) before
            # matching, and match by substring rather than exact equality —
            # some counters (like account origin with a linked sub-term)
            # render extra whitespace or nested tags around the label text.
            field_name = re.sub(r"\s+", " ", muted_el.get_text(strip=True)).strip().lower()

            key = RELIABLE_LABEL_MAP.get(field_name)
            if not key:
                # substring fallback: catches cases with trailing icons/notes
                for label, mapped_key in RELIABLE_LABEL_MAP.items():
                    if label in field_name:
                        key = mapped_key
                        break
            if not key:
                continue

            value = re.sub(r"\s+", " ", label_el.get_text(strip=True)).strip()
            if value:
                result[key] = value
            else:
                log.warning(
                    f"Reliable-info: matched field '{field_name}' but label "
                    f"text was empty. Raw counter HTML: {str(counter)[:400]}"
                )

    missing = [k for k, v in result.items() if v == "N/A"]
    if missing:
        log.warning(f"Reliable-info: still N/A for {missing} — dumping raw counters blocks")
        if containers:
            for i, c in enumerate(containers):
                log.warning(f"Raw counters block #{i}: {str(c)[:2000]}")
        else:
            log.warning("No .marketItemView--counters element found on this page at all.")

    if all(v == "N/A" for v in result.values()):
        return _parse_reliable_info_fallback(soup)

    log.info(
        f"Reliable-info scrape → origin={result['account_origin']} "
        f"country={result['country']} email_domain={result['email_domain']}"
    )
    return result

def _parse_reliable_info_fallback(soup: BeautifulSoup) -> dict:
    """Old label/sibling heuristic, kept as a backup if markup changes."""
    result = {v: "N/A" for v in RELIABLE_LABEL_MAP.values()}

    for el in soup.find_all(["div", "span", "p", "dt", "dd", "li"]):
        label_text = el.get_text(strip=True).lower()
        key = RELIABLE_LABEL_MAP.get(label_text)
        if not key or result[key] != "N/A":
            continue

        prev = el.find_previous_sibling()
        if prev:
            v = prev.get_text(strip=True)
            if v and v.lower() not in RELIABLE_LABEL_MAP and len(v) < 80:
                result[key] = v

    full_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        key = RELIABLE_LABEL_MAP.get(line.lower())
        if key and result[key] == "N/A" and i > 0:
            candidate = lines[i - 1]
            if candidate.lower() not in RELIABLE_LABEL_MAP and len(candidate) < 80:
                result[key] = candidate

    return result

# ─── API FETCH ────────────────────────────────────────────────────────────────

async def fetch_account_api(session: aiohttp.ClientSession, acc_id: str) -> dict:
    """
    Hit lzt's market API directly to get reliable info block:
    account origin, last activity, email, phone, country, email domain.
    """
    api_url = f"https://api.lzt.market/{acc_id}"
    hdrs = {
        "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":            "application/json, text/javascript, */*; q=0.01",
        "Referer":           f"https://lzt.market/{acc_id}/",
        "X-Requested-With":  "XMLHttpRequest",
    }
    if LZT_API_TOKEN:
        hdrs["Authorization"] = f"Bearer {LZT_API_TOKEN}"

    result = {}
    try:
        async with session.get(
            api_url, headers=hdrs,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            raw_body = await r.text()

            if r.status != 200:
                # This is almost certainly why everything showed N/A before.
                log.error(f"API {acc_id} → HTTP {r.status}: {raw_body[:300]}")
                return result

            try:
                data = json.loads(raw_body)
            except Exception:
                log.error(f"API {acc_id} → non-JSON response: {raw_body[:300]}")
                return result

            if "errors" in data:
                log.error(f"API {acc_id} → error payload: {data['errors']}")
                return result

            item = data.get("item", {})
            if not item:
                # Log the FULL top-level response once so real key names
                # can be confirmed instead of guessed.
                log.warning(f"API {acc_id} → no 'item' key, raw keys: {list(data.keys())}")
                log.warning(f"API {acc_id} → full body: {raw_body[:1000]}")
            else:
                log.info(f"API keys → {list(item.keys())}")

                def pick(*keys):
                    for k in keys:
                        v = item.get(k)
                        if v is not None and str(v).strip():
                            return str(v).strip()
                    return "N/A"

                result["account_origin"] = pick(
                    "account_origin_title", "accountOriginTitle",
                    "origin_title", "originTitle",
                    "account_origin", "accountOrigin",
                    "origin",
                )
                result["last_activity"] = pick(
                    "last_activity_date", "lastActivityDate",
                    "last_activity", "lastActivity",
                    "updated_at", "updatedAt",
                )
                result["email_linked"] = pick(
                    "email_login_data", "emailLoginData",
                    "email_linked", "emailLinked",
                    "has_email", "hasEmail",
                )
                result["phone_linked"] = pick(
                    "phone_verified", "phoneVerified",
                    "phone_linked", "phoneLinked",
                    "has_phone", "hasPhone",
                )
                result["email_domain"] = pick(
                    "email_type", "emailType",
                    "email_domain", "emailDomain",
                )
                result["country"] = pick(
                    "item_origin", "itemOrigin",
                    "country", "region",
                    "locale",
                )
                price_raw = item.get("price") or item.get("priceUsd")
                if price_raw:
                    result["price"] = str(price_raw)

                log.info(
                    f"API → origin={result.get('account_origin')} "
                    f"country={result.get('country')} "
                    f"email_domain={result.get('email_domain')}"
                )
    except Exception as e:
        log.error(f"API fetch error {acc_id}: {e}")
    return result

# ─── IMAGE DOWNLOAD ───────────────────────────────────────────────────────────
#
# https://lzt.market/{id}/image?type=weapons returns ONE server-generated
# composite image containing every weapon skin tiled together (confirmed:
# a 61-skin account returns a single image with all 61 tiles). The earlier
# "gallery scraping" approach was solving the wrong problem — there is no
# per-weapon gallery to scrape, just this one endpoint that needs to be
# captured reliably.
#
# The likely reason it was unreliable: a plain aiohttp GET has no browser
# session/cookies and may get an incomplete or auth-gated response, and the
# old Selenium fallback grabbed driver.find_element(By.TAG_NAME, "img") too
# early, sometimes before the tiled image had finished rendering — or it
# picked up some other <img> on the page. Fix: load the URL in the real
# browser session, explicitly wait for the image to finish loading, then
# capture that exact element as a screenshot — guaranteed to match what a
# human sees when clicking the link manually.

def download_composite_weapon_image(
    driver: webdriver.Chrome,
    acc_id: str,
) -> Optional[str]:
    dest = IMAGE_DIR / f"{acc_id}_weapons.png"
    if dest.exists():
        return str(dest)

    image_url = f"https://lzt.market/{acc_id}/image?type=weapons"

    try:
        driver.get(image_url)

        # Wait for an <img> element to exist on the page at all.
        img_el = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "img"))
        )

        # Wait for it to actually finish loading (naturalWidth > 0), not
        # just be present in the DOM — this is the timing gap that likely
        # caused broken/partial captures before.
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script(
                "var i = arguments[0]; return i.complete && i.naturalWidth > 0;",
                img_el,
            )
        )

        # A short extra buffer in case the tiled image is drawn onto a
        # canvas or updated asynchronously right after the load event.
        time.sleep(1.0)

        png_bytes = img_el.screenshot_as_png
        if png_bytes and len(png_bytes) > 1024:
            dest.write_bytes(png_bytes)
            log.info(f"Weapon composite image saved (element screenshot) → {dest}")
            return str(dest)

    except Exception as e:
        log.error(f"Composite weapon image capture failed for {acc_id}: {e}")

    log.error(f"All image strategies failed for {acc_id}")
    return None

# ─── GRID-BASED WEAPON ICON TILING ─────────────────────────────────────────────
# The listing page renders each owned weapon skin as its own <li data-id="...">
# node inside <ul class="body" data-key="WeaponSkins">. Rather than
# screenshotting the whole composite-image endpoint, we can pull each skin
# icon's URL directly out of that grid, download them individually, and
# stitch them into one image ourselves — more reliable since it doesn't
# depend on a second page load finishing/rendering correctly.

_BG_URL_RE = re.compile(r"url\((['\"]?)(.*?)\1\)")

def extract_weapon_skin_icon_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one('ul.body[data-key="WeaponSkins"]') \
        or soup.select_one('ul[data-key="WeaponSkins"]')
    if not container:
        return []

    urls: list[str] = []
    for li in container.select("li[data-id]"):
        url = None

        img = li.select_one("img")
        if img:
            url = img.get("src") or img.get("data-src")

        if not url:
            bg_el = li.select_one("[style*='background-image']") or li
            style = bg_el.get("style", "")
            m = _BG_URL_RE.search(style)
            if m:
                url = m.group(2)

        if not url:
            continue

        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://lzt.market" + url

        urls.append(url)

    return urls

async def _fetch_icon_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.read()
            log.warning(f"Icon fetch {url} → HTTP {r.status}")
    except Exception as e:
        log.warning(f"Icon fetch failed {url}: {e}")
    return None

def build_tiled_image(
    icon_bytes_list: list[bytes],
    acc_id:          str,
    cols:            int = 6,
    tile_size:       int = 128,
    padding:         int = 6,
    bg_color:        tuple = (24, 24, 24),
) -> Optional[str]:
    tiles = []
    for b in icon_bytes_list:
        try:
            im = Image.open(io.BytesIO(b)).convert("RGBA")
            im.thumbnail((tile_size, tile_size))
            tiles.append(im)
        except Exception as e:
            log.warning(f"Skipping unreadable icon for {acc_id}: {e}")

    if not tiles:
        return None

    n    = len(tiles)
    cols = min(cols, n)
    rows = (n + cols - 1) // cols

    W = cols * tile_size + (cols + 1) * padding
    H = rows * tile_size + (rows + 1) * padding

    canvas = Image.new("RGB", (W, H), bg_color)
    for i, im in enumerate(tiles):
        r, c = divmod(i, cols)
        x = padding + c * (tile_size + padding) + (tile_size - im.width) // 2
        y = padding + r * (tile_size + padding) + (tile_size - im.height) // 2
        canvas.paste(im, (x, y), im)

    dest = IMAGE_DIR / f"{acc_id}_weapons_tiled.png"
    canvas.save(dest, "PNG")
    log.info(f"Tiled {n} weapon icon(s) → {dest}")
    return str(dest)

async def download_and_tile_weapon_icons(
    session: aiohttp.ClientSession,
    acc_id:  str,
    html:    str,
) -> Optional[str]:
    urls = extract_weapon_skin_icon_urls(html)
    if not urls:
        return None

    results = await asyncio.gather(*[_fetch_icon_bytes(session, u) for u in urls])
    icon_bytes = [b for b in results if b]
    if not icon_bytes:
        return None

    return await asyncio.to_thread(build_tiled_image, icon_bytes, acc_id)

async def download_weapon_images(
    session: aiohttp.ClientSession,
    acc_id:  str,
    driver:  webdriver.Chrome,
    html:    str,
) -> list[str]:
    """
    Returns a list (0 or 1 items) containing the path to a single composite
    weapon-skins image, if one could be produced. Kept as a list, rather
    than a single Optional[str], so callers that already branch on
    len(img_paths) for album-vs-single-photo sending don't need changes.

    Tries, in order:
      1. Scrape each skin icon from the WeaponSkins grid on the listing
         page and tile them into one image ourselves (fast, no extra
         page load).
      2. Fall back to screenshotting the site's own composite-image
         endpoint if the grid wasn't found or none of its icons could
         be downloaded.
    """
    tiled = await download_and_tile_weapon_icons(session, acc_id, html)
    if tiled:
        return [tiled]

    path = await asyncio.to_thread(download_composite_weapon_image, driver, acc_id)
    return [path] if path else []

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

async def send_photo(session: aiohttp.ClientSession, path: str, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("chat_id",    TELEGRAM_CHAT_ID)
            form.add_field("caption",    caption[:1024])
            form.add_field("parse_mode", "HTML")
            form.add_field("photo", f, filename="account.jpg", content_type="image/jpeg")
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json()
                if not data.get("ok"):
                    log.error(f"Telegram photo error: {data}")
                return data.get("ok", False)
    except Exception as e:
        log.error(f"send_photo exception: {e}")
    return False

async def send_photo_album(session: aiohttp.ClientSession, paths: list[str], caption: str) -> bool:
    """
    Sends multiple images as a single Telegram album (sendMediaGroup).
    Telegram allows 2-10 items per group and only shows the caption on
    the first item. Caller should use send_photo() instead for exactly 1.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    paths = paths[:10]  # Telegram hard limit per album

    form  = aiohttp.FormData()
    form.add_field("chat_id", TELEGRAM_CHAT_ID)

    media = []
    open_files = []
    try:
        for i, path in enumerate(paths):
            attach_name = f"photo{i}"
            f = open(path, "rb")
            open_files.append(f)
            form.add_field(attach_name, f, filename=f"account_{i}.jpg", content_type="image/jpeg")
            item = {"type": "photo", "media": f"attach://{attach_name}"}
            if i == 0:
                item["caption"]    = caption[:1024]
                item["parse_mode"] = "HTML"
            media.append(item)

        form.add_field("media", json.dumps(media))

        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=45)) as r:
            data = await r.json()
            if not data.get("ok"):
                log.error(f"Telegram album error: {data}")
            return data.get("ok", False)
    except Exception as e:
        log.error(f"send_photo_album exception: {e}")
        return False
    finally:
        for f in open_files:
            f.close()

async def send_message(session: aiohttp.ClientSession, text: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            if not data.get("ok"):
                log.error(f"Telegram msg error: {data}")
            return data.get("ok", False)
    except Exception as e:
        log.error(f"send_message exception: {e}")
    return False

def build_caption(acc: AccountInfo) -> str:
    TYPE_EMOJI = {
        "resale":   "🔄", "phishing": "🎣",
        "stealer":  "☠️", "original": "✅",
        "brute":    "💀",
    }
    type_emoji = "❓"
    if acc.account_type != "N/A":
        for k, v in TYPE_EMOJI.items():
            if k in acc.account_type.lower():
                type_emoji = v
                break

    ORIGIN_EMOJI = {
        "resale":   "🔄", "phishing": "🎣",
        "stealer":  "☠️", "original": "✅",
        "brute":    "💀", "self":     "👤",
    }
    origin_emoji = "🔍"
    if acc.account_origin != "N/A":
        for k, v in ORIGIN_EMOJI.items():
            if k in acc.account_origin.lower():
                origin_emoji = v
                break

    return (
        f"🎯 <b>New Valorant Account</b>\n\n"
        f"🆔 <code>{acc.account_id}</code>\n"
        f"💰 Price: <b>{acc.price}</b>\n"
        f"{type_emoji} Type: <b>{acc.account_type}</b>\n"
        f"🏆 Rank: <b>{acc.rank}</b>\n"
        f"📅 Last Rank: {acc.last_rank}\n"
        f"📆 Prev Season: {acc.prev_season}\n"
        f"⚡ Level: {acc.level}\n"
        f"💎 VP: {acc.vp}\n"
        f"🌟 Radiant Pts: {acc.radiant_pts}\n"
        f"🔪 Knives: {acc.knives}\n"
        f"🌍 Locale: {acc.locale}\n"
        f"🤖 Free Agents: {acc.free_agents}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Reliable Info</b>\n"
        f"🕐 Last Activity: {acc.last_activity}\n"
        f"📧 Email Linked: {acc.email_linked}\n"
        f"🌐 Country: {acc.country}\n"
        f"📱 Phone Linked: {acc.phone_linked}\n"
        f"📨 Email Domain: {acc.email_domain}\n"
        f"{origin_emoji} Account Origin: <b>{acc.account_origin}</b>\n\n"
        f"🔗 <a href='https://lzt.market/{acc.account_id}/'>View Listing</a>\n"
        f"🕐 {acc.seen_at}"
    )

# ─── CORE ─────────────────────────────────────────────────────────────────────

async def process_account(
    driver:  webdriver.Chrome,
    session: aiohttp.ClientSession,
    raw:     dict,
    seen:    set,
) -> Optional[AccountInfo]:
    acc_id = raw["id"]
    if acc_id in seen:
        return None

    log.info(f"Processing → {acc_id}")

    # parallel: HTML scrape (stats) + HTML scrape (reliable info block)
    acc_html = await asyncio.to_thread(
        selenium_get_html, driver, f"https://lzt.market/{acc_id}/"
    )

    stats         = parse_stats(acc_html)
    reliable_info = parse_reliable_info(acc_html)

    # reliable-info scrape wins over the general stats scrape for any
    # field it actually found
    for key, val in reliable_info.items():
        if val and val != "N/A":
            stats[key] = val

    # resolve price — listing card > API > page scrape
    listing_price = raw.get("price", "N/A")
    page_price    = stats.pop("price", "N/A")
    final_price   = listing_price if listing_price != "N/A" else page_price

    img_paths = await download_weapon_images(session, acc_id, driver, acc_html)

    acc = AccountInfo(
        account_id  = acc_id,
        url         = f"https://lzt.market/{acc_id}/",
        price       = final_price,
        filter_url  = raw.get("filter_url", ""),
        image_path  = img_paths[0] if img_paths else None,
        image_paths = img_paths,
        **stats,
    )

    # ── Origin gate ──
    # Skip anything that isn't confirmed clean. Unknown origin ("N/A",
    # because the API call failed or the field wasn't found) is treated
    # as NOT safe and is skipped too, rather than assumed fine.
    origin_lower = acc.account_origin.lower()
    is_allowed   = any(o in origin_lower for o in ALLOWED_ORIGINS)
    if not is_allowed:
        log.warning(
            f"⛔ Skipped {acc_id} — origin '{acc.account_origin}' not in "
            f"ALLOWED_ORIGINS {ALLOWED_ORIGINS}"
        )
        seen.add(acc_id)   # don't keep re-checking it every cycle
        save_seen(seen)
        return None

    caption = build_caption(acc)

    if len(img_paths) >= 2:
        ok = await send_photo_album(session, img_paths, caption)
    elif len(img_paths) == 1:
        ok = await send_photo(session, img_paths[0], caption)
    else:
        ok = await send_message(session, caption + "\n\n⚠️ <i>Weapon image(s) unavailable</i>")

    if ok:
        log.info(f"✅ Alert sent → {acc_id}")
        seen.add(acc_id)
        save_seen(seen)
    else:
        log.error(f"❌ Alert failed → {acc_id}")

    return acc if ok else None

async def scan_filter(
    driver:     webdriver.Chrome,
    session:    aiohttp.ClientSession,
    filter_url: str,
    seen:       set,
) -> int:
    log.info(f"Scanning → {filter_url}")
    html     = selenium_get_html(driver, filter_url)
    listings = parse_listings(html, filter_url)

    new = 0
    for raw in listings:
        await asyncio.sleep(1.5)
        acc = await process_account(driver, session, raw, seen)
        if acc:
            new += 1
    return new

async def main():
    seen   = load_seen()
    driver = build_driver()
    log.info(f"Seen cache: {len(seen)} | Filters: {len(FILTERS)} | Interval: {CHECK_INTERVAL}s")

    connector   = aiohttp.TCPConnector(ssl=False)
    cycle_count = 0
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            while True:
                log.info("=" * 55)
                log.info(f"Cycle → {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                # Preventive restart: headless Chrome reliably leaks memory
                # and eventually crashes on long unattended runs, so refresh
                # it on a schedule rather than waiting for that to happen.
                if cycle_count and cycle_count % DRIVER_RESTART_EVERY_N_CYCLES == 0:
                    log.info(f"Scheduled driver refresh (every {DRIVER_RESTART_EVERY_N_CYCLES} cycles)")
                    driver = restart_driver(driver)
                elif not driver_is_alive(driver):
                    log.warning("Driver found dead at cycle start")
                    driver = restart_driver(driver)

                total = 0
                for furl in FILTERS:
                    try:
                        total += await scan_filter(driver, session, furl, seen)
                    except (InvalidSessionIdException, WebDriverException) as e:
                        # Chrome died mid-scan. Recover instead of crashing
                        # the whole script — skip this filter for the
                        # current cycle and pick it up next time around.
                        log.error(f"Driver crashed while scanning {furl}: {e}")
                        driver = restart_driver(driver)
                        continue
                    except Exception as e:
                        # Any other per-filter failure shouldn't take down
                        # the rest of the cycle either.
                        log.error(f"Unexpected error scanning {furl}: {e}")
                        continue
                    await asyncio.sleep(2)

                cycle_count += 1
                log.info(f"Cycle done — {total} new account(s)")
                await asyncio.sleep(CHECK_INTERVAL)
        finally:
            try:
                driver.quit()
            except Exception:
                pass

async def test_single_listing(acc_id: str):
    """
    Debug helper: scrape ONE listing and print exactly what the parser
    extracts. No Telegram send, no origin filter, no 'seen' tracking —
    this never sends an alert, so it's safe to run against anything
    while you're checking whether the scrape logic works.
    """
    driver = build_driver()
    try:
        url  = f"https://lzt.market/{acc_id}/"
        html = selenium_get_html(driver, url)

        stats         = parse_stats(html)
        reliable_info = parse_reliable_info(html)

        print(f"\n=== {url} ===")
        print("-- parse_stats --")
        for k, v in stats.items():
            print(f"  {k:15s}: {v}")
        print("-- parse_reliable_info --")
        for k, v in reliable_info.items():
            print(f"  {k:15s}: {v}")
        print()
    finally:
        driver.quit()

async def debug_type(acc_id: str):
    """
    Debug helper: isolates ONLY the type/origin detection logic and shows
    exactly which path (if any) catches it. Three checks:
      1. account_type via TYPE_SELECTORS + TYPE_KEYWORDS scan (parse_stats)
      2. account_type via line-by-line fallback scan (parse_stats)
      3. account_origin via the 'Reliable information' counters block
         (parse_reliable_info)
    No Telegram send, no filtering, no 'seen' tracking.
    """
    driver = build_driver()
    try:
        url  = f"https://lzt.market/{acc_id}/"
        html = selenium_get_html(driver, url)
        soup = BeautifulSoup(html, "html.parser")

        print(f"\n=== DEBUG TYPE/ORIGIN — {url} ===\n")

        # ── Check 1: TYPE_SELECTORS + TYPE_KEYWORDS scan ──
        print("[1] TYPE_SELECTORS + TYPE_KEYWORDS scan:")
        found_via_selector = False
        for sel in TYPE_SELECTORS:
            for el in soup.select(sel):
                txt = el.get_text(strip=True).lower()
                for kw in TYPE_KEYWORDS:
                    if kw in txt and len(el.get_text(strip=True)) < 60:
                        print(f"    MATCH  selector='{sel}'  keyword='{kw}'  text='{el.get_text(strip=True)}'")
                        found_via_selector = True
        if not found_via_selector:
            print("    no match")

        # ── Check 2: line-by-line fallback scan ──
        print("\n[2] Line-by-line TYPE_KEYWORDS scan:")
        full_text = soup.get_text(separator="\n")
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        found_via_line = False
        for line in lines:
            ll = line.lower()
            for kw in TYPE_KEYWORDS:
                if kw in ll and len(line) < 60:
                    print(f"    MATCH  keyword='{kw}'  line='{line}'")
                    found_via_line = True
        if not found_via_line:
            print("    no match")

        # ── Check 3: reliable-info counters block (account_origin) ──
        print("\n[3] Reliable-info 'Account origin' counter:")
        containers = soup.select(".marketItemView--counters")
        found_via_counter = False
        for i, c in enumerate(containers):
            for counter in c.select(".counter"):
                muted_el = counter.select_one(".muted")
                label_el = counter.select_one(".label")
                if not muted_el:
                    continue
                field_name = re.sub(r"\s+", " ", muted_el.get_text(strip=True)).strip().lower()
                if "origin" in field_name:
                    val = label_el.get_text(strip=True) if label_el else "(no .label found)"
                    print(f"    MATCH  container=#{i}  muted='{muted_el.get_text(strip=True)}'  label='{val}'")
                    found_via_counter = True
        if not found_via_counter:
            print(f"    no 'origin' counter found across {len(containers)} counters block(s) on this page")

        print(f"\nfinal parse_stats.account_type   = {parse_stats(html)['account_type']}")
        print(f"final parse_reliable_info.origin = {parse_reliable_info(html)['account_origin']}")
        print()
    finally:
        driver.quit()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Usage: python culc.py test <listing_id>
        # e.g.:  python culc.py test 257260886
        asyncio.run(test_single_listing(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "debugtype":
        # Usage: python culc.py debugtype <listing_id>
        # e.g.:  python culc.py debugtype 257279296
        asyncio.run(debug_type(sys.argv[2]))
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Stopped.")
