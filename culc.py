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

# ─── CONFIG ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHAT_IDS = [
    str(v).strip()
    for v in [
        TELEGRAM_CHAT_ID,
        os.environ.get("TELEGRAM_CHAT_IDS", ""),
        "5487976872",
    ]
    if str(v).strip()
]
TELEGRAM_CHAT_IDS = list(dict.fromkeys(TELEGRAM_CHAT_IDS))

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    raise SystemExit(
        "Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID environment "
        "variables. Set them as Fly secrets (fly secrets set "
        "TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...) or in your local "
        "environment before running."
    )

if not TELEGRAM_CHAT_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_CHAT_IDS[0]

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "120"))
RATE_LIMIT_DELAY = float(os.environ.get("RATE_LIMIT_DELAY", "1.0"))
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", str(4 * 3600)))
DRIVER_RESTART_EVERY_N_CYCLES = 15
LZT_API_TOKEN = os.environ.get("LZT_API_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lzt_sniper")

# Filters
FILTERS = [
    "https://lzt.market/riot?pmax=11.5&weaponSkin[]=d8d5d7a1-4d81-8560-54bc-0692ab40f69b&order_by=pdate_to_down_upload",
    "https://lzt.market/riot?pmax=11.5&weaponSkin[]=3f6410af-4fd7-74fb-c0f4-6ab61d30022c&order_by=pdate_to_down_upload",
    "https://lzt.market/riot?pmax=11.5&weaponSkin[]=4f5ee03a-4204-5526-6941-bca4f911a768&order_by=pdate_to_down_upload",
    "https://lzt.market/riot?pmax=11.5&weaponSkin[]=000ad7b1-44b0-9345-ea47-9cbd7dcdbb38&order_by=pdate_to_down_upload",
    "https://lzt.market/riot?pmax=11.5&weaponSkin[]=e37229ed-4ddf-5e7e-e744-8fba60fa2c37&order_by=pdate_to_down_upload",
]

_filters_override = DATA_DIR / "filters.json"
if _filters_override.exists():
    try:
        loaded = json.loads(_filters_override.read_text())
        if isinstance(loaded, list) and loaded:
            FILTERS = loaded
            log.info(f"Loaded {len(FILTERS)} filter(s) from {_filters_override}")
    except Exception as e:
        log.error(f"Failed to load {_filters_override}, using built-in FILTERS: {e}")

SEEN_FILE = DATA_DIR / "seen_accounts.json"
IMAGE_DIR = DATA_DIR / "account_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_ORIGINS = {
    "original",
    "self-registered",
    "N/A",
    "n/a",
    "Stealer",
    "stealer",
    "phishing",
    "Phishing",
    "Resale(Stealer)",
    "resale(stealer)",
    "Resale(Phishing)",
    "resale(phishing)",
}

# ─── DATA ───────────────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    account_id: str
    url: str
    price: str = "N/A"
    account_type: str = "N/A"
    rank: str = "N/A"
    last_rank: str = "N/A"
    prev_season: str = "N/A"
    level: str = "N/A"
    vp: str = "N/A"
    radiant_pts: str = "N/A"
    knives: str = "N/A"
    locale: str = "N/A"
    free_agents: str = "N/A"
    last_activity: str = "N/A"
    email_linked: str = "N/A"
    country: str = "N/A"
    phone_linked: str = "N/A"
    email_domain: str = "N/A"
    account_origin: str = "N/A"
    image_path: Optional[str] = None
    image_paths: list = field(default_factory=list)
    filter_url: str = ""
    seen_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

# ─── PERSISTENCE ─────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── SELENIUM ───────────────────────────────────────────────────────────

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
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def restart_driver(old_driver: webdriver.Chrome) -> webdriver.Chrome:
    try:
        old_driver.quit()
    except Exception:
        pass
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

# ─── LISTING PARSER ────────────────────────────────────────────────────

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
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_ids: set = set()

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
        m = _LISTING_ID_RE.match(path) or _LISTING_ID_RE.match(path + "/")
        if not m:
            continue
        acc_id = m.group(1)
        if acc_id in seen_ids:
            continue
        seen_ids.add(acc_id)

        price = "N/A"
        card = a.find_parent(attrs={"class": re.compile(r"item|card|listing", re.I)})
        if card:
            for sel in PRICE_SELECTORS:
                tag = card.select_one(sel)
                if tag:
                    price = tag.get_text(strip=True)
                    break
        items.append({"id": acc_id, "price": price, "filter_url": filter_url})

    log.info(f"Anchor-grep → {len(items)} listing(s)")
    return items

# ─── STATS PARSER ───────────────────────────────────────────────────────

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
    "inventory value": "vp",
    "valorant points": "vp",
    "vp": "vp",
    "radiant points": "radiant_pts",
    "free agents": "free_agents",
    "current rank": "rank",
    "last rank": "last_rank",
    "previous season rank": "prev_season",
    "level": "level",
    "knives": "knives",
    "locale": "locale",
}

RANK_NAMES = [
    "iron", "bronze", "silver", "gold", "platinum",
    "diamond", "ascendant", "immortal", "radiant",
    "ranked ready", "unrated", "no rank",
]


def parse_stats(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    stats = {
        "rank": "N/A", "last_rank": "N/A", "prev_season": "N/A",
        "level": "N/A", "vp": "N/A", "radiant_pts": "N/A",
        "knives": "N/A", "locale": "N/A", "free_agents": "N/A",
        "price": "N/A", "account_type": "N/A",
        "last_activity": "N/A", "email_linked": "N/A",
        "country": "N/A", "phone_linked": "N/A",
        "email_domain": "N/A", "account_origin": "N/A",
    }

    for sel in PRICE_SELECTORS:
        tag = soup.select_one(sel)
        if tag:
            raw = tag.get_text(strip=True)
            if re.search(r"\d", raw):
                stats["price"] = raw
                break

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

    full_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]

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
    "last activity": "last_activity",
    "email linked": "email_linked",
    "country": "country",
    "phone linked": "phone_linked",
    "email domain": "email_domain",
    "account origin": "account_origin",
}


def parse_reliable_info(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result = {v: "N/A" for v in RELIABLE_LABEL_MAP.values()}
    containers = soup.select(".marketItemView--counters")
    search_roots = containers if containers else [soup]

    for search_root in search_roots:
        for counter in search_root.select(".counter"):
            label_el = counter.select_one(".label")
            muted_el = counter.select_one(".muted")
            if not label_el or not muted_el:
                continue
            field_name = re.sub(r"\s+", " ", muted_el.get_text(strip=True)).strip().lower()
            key = RELIABLE_LABEL_MAP.get(field_name)
            if not key:
                for label, mapped_key in RELIABLE_LABEL_MAP.items():
                    if label in field_name:
                        key = mapped_key
                        break
            if not key:
                continue

            value = re.sub(r"\s+", " ", label_el.get_text(strip=True)).strip()
            if value:
                result[key] = value

    if all(v == "N/A" for v in result.values()):
        return _parse_reliable_info_fallback(soup)
    return result


def _parse_reliable_info_fallback(soup: BeautifulSoup) -> dict:
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

# ─── API FETCH ──────────────────────────────────────────────────────────

async def fetch_account_api(session: aiohttp.ClientSession, acc_id: str) -> dict:
    api_url = f"https://api.lzt.market/{acc_id}"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"https://lzt.market/{acc_id}/",
        "X-Requested-With": "XMLHttpRequest",
    }
    if LZT_API_TOKEN:
        hdrs["Authorization"] = f"Bearer {LZT_API_TOKEN}"

    result = {}
    try:
        async with session.get(api_url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=15)) as r:
            raw_body = await r.text()
            if r.status != 200:
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
                log.warning(f"API {acc_id} → no 'item' key, raw keys: {list(data.keys())}")
            else:
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
    except Exception as e:
        log.error(f"API fetch error {acc_id}: {e}")
    return result

# ─── IMAGE DOWNLOAD ─────────────────────────────────────────────────────

_BG_URL_RE = re.compile(r"url\((['\"]?)(.*?)\1\)")


def download_composite_weapon_image(driver: webdriver.Chrome, acc_id: str) -> Optional[str]:
    dest = IMAGE_DIR / f"{acc_id}_weapons.png"
    if dest.exists():
        return str(dest)

    image_url = f"https://lzt.market/{acc_id}/image?type=weapons"
    try:
        driver.get(image_url)
        img_el = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "img"))
        )
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script(
                "var i = arguments[0]; return i.complete && i.naturalWidth > 0;",
                img_el,
            )
        )
        time.sleep(1.0)
        png_bytes = img_el.screenshot_as_png
        if png_bytes and len(png_bytes) > 1024:
            dest.write_bytes(png_bytes)
            log.info(f"Weapon composite image saved → {dest}")
            return str(dest)
    except Exception as e:
        log.error(f"Composite weapon image capture failed for {acc_id}: {e}")
    return None


def extract_weapon_skin_icon_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one('ul.body[data-key="WeaponSkins"]') or soup.select_one('ul[data-key="WeaponSkins"]')
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


def build_tiled_image(icon_bytes_list: list[bytes], acc_id: str, cols: int = 6, tile_size: int = 128, padding: int = 6, bg_color: tuple = (24, 24, 24)) -> Optional[str]:
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

    n = len(tiles)
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


async def download_and_tile_weapon_icons(session: aiohttp.ClientSession, acc_id: str, html: str) -> Optional[str]:
    urls = extract_weapon_skin_icon_urls(html)
    if not urls:
        return None
    results = await asyncio.gather(*[_fetch_icon_bytes(session, u) for u in urls])
    icon_bytes = [b for b in results if b]
    if not icon_bytes:
        return None
    return await asyncio.to_thread(build_tiled_image, icon_bytes, acc_id)


async def download_weapon_images(session: aiohttp.ClientSession, acc_id: str, driver: webdriver.Chrome, html: str) -> list[str]:
    tiled = await download_and_tile_weapon_icons(session, acc_id, html)
    if tiled:
        return [tiled]
    path = await asyncio.to_thread(download_composite_weapon_image, driver, acc_id)
    return [path] if path else []

# ─── TELEGRAM ───────────────────────────────────────────────────────────

def buy_button_markup(acc_id: str) -> str:
    return json.dumps({
        "inline_keyboard": [[
            {"text": "🛒 View / Buy on LZT Market", "url": f"https://lzt.market/{acc_id}/"}
        ]]
    })


async def send_photo(session: aiohttp.ClientSession, path: str, caption: str, reply_markup: Optional[str] = None, chat_ids: Optional[list[str]] = None) -> bool:
    recipients = chat_ids or TELEGRAM_CHAT_IDS
    if not recipients:
        return False

    any_ok = False
    for chat_id in recipients:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        try:
            with open(path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("caption", caption[:1024])
                form.add_field("parse_mode", "HTML")
                if reply_markup:
                    form.add_field("reply_markup", reply_markup)
                form.add_field("photo", f, filename="account.jpg", content_type="image/jpeg")
                async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    data = await r.json()
                    if not data.get("ok"):
                        log.error(f"Telegram photo error for {chat_id}: {data}")
                    else:
                        any_ok = True
        except Exception as e:
            log.error(f"send_photo exception for {chat_id}: {e}")
    return any_ok


async def send_photo_album(session: aiohttp.ClientSession, paths: list[str], caption: str, chat_ids: Optional[list[str]] = None) -> bool:
    recipients = chat_ids or TELEGRAM_CHAT_IDS
    if not recipients:
        return False

    any_ok = False
    for chat_id in recipients:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        image_paths = paths[:10]
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)

        media = []
        open_files = []
        try:
            for i, path in enumerate(image_paths):
                attach_name = f"photo{i}"
                f = open(path, "rb")
                open_files.append(f)
                form.add_field(attach_name, f, filename=f"account_{i}.jpg", content_type="image/jpeg")
                item = {"type": "photo", "media": f"attach://{attach_name}"}
                if i == 0:
                    item["caption"] = caption[:1024]
                    item["parse_mode"] = "HTML"
                media.append(item)
            form.add_field("media", json.dumps(media))
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=45)) as r:
                data = await r.json()
                if not data.get("ok"):
                    log.error(f"Telegram album error for {chat_id}: {data}")
                else:
                    any_ok = True
        except Exception as e:
            log.error(f"send_photo_album exception for {chat_id}: {e}")
        finally:
            for f in open_files:
                f.close()
    return any_ok


async def send_message(session: aiohttp.ClientSession, text: str, reply_markup: Optional[str] = None, chat_ids: Optional[list[str]] = None) -> bool:
    recipients = chat_ids or TELEGRAM_CHAT_IDS
    if not recipients:
        return False

    any_ok = False
    for chat_id in recipients:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                if not data.get("ok"):
                    log.error(f"Telegram msg error for {chat_id}: {data}")
                else:
                    any_ok = True
        except Exception as e:
            log.error(f"send_message exception for {chat_id}: {e}")
    return any_ok


def build_caption(acc: AccountInfo) -> str:
    TYPE_EMOJI = {
        "resale": "🔄",
        "phishing": "🎣",
        "stealer": "☠️",
        "original": "✅",
        "brute": "💀",
    }
    type_emoji = "❓"
    if acc.account_type != "N/A":
        for k, v in TYPE_EMOJI.items():
            if k in acc.account_type.lower():
                type_emoji = v
                break

    ORIGIN_EMOJI = {
        "resale": "🔄",
        "phishing": "🎣",
        "stealer": "☠️",
        "original": "✅",
        "brute": "💀",
        "self": "👤",
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

# ─── CORE ─────────────────────────────────────────────────────────────

async def process_account(driver: webdriver.Chrome, session: aiohttp.ClientSession, raw: dict, seen: set) -> Optional[AccountInfo]:
    acc_id = raw["id"]
    if acc_id in seen:
        return None

    log.info(f"Processing → {acc_id}")
    acc_html = await asyncio.to_thread(selenium_get_html, driver, f"https://lzt.market/{acc_id}/")
    stats = parse_stats(acc_html)
    reliable_info = parse_reliable_info(acc_html)
    for key, val in reliable_info.items():
        if val and val != "N/A":
            stats[key] = val

    listing_price = raw.get("price", "N/A")
    page_price = stats.pop("price", "N/A")
    final_price = listing_price if listing_price != "N/A" else page_price

    img_paths = await download_weapon_images(session, acc_id, driver, acc_html)
    acc = AccountInfo(
        account_id=acc_id,
        url=f"https://lzt.market/{acc_id}/",
        price=final_price,
        filter_url=raw.get("filter_url", ""),
        image_path=img_paths[0] if img_paths else None,
        image_paths=img_paths,
        **stats,
    )

    origin_lower = acc.account_origin.lower()
    is_allowed = any(o in origin_lower for o in ALLOWED_ORIGINS)
    if not is_allowed:
        log.warning(f"⛔ Skipped {acc_id} — origin '{acc.account_origin}' not in ALLOWED_ORIGINS {ALLOWED_ORIGINS}")
        seen.add(acc_id)
        save_seen(seen)
        return None

    caption = build_caption(acc)
    buy_markup = buy_button_markup(acc_id)

    if len(img_paths) >= 2:
        ok = await send_photo_album(session, img_paths, caption)
        if ok:
            await send_message(session, "⬆️ Quick action:", reply_markup=buy_markup)
    elif len(img_paths) == 1:
        ok = await send_photo(session, img_paths[0], caption, reply_markup=buy_markup)
    else:
        ok = await send_message(session, caption + "\n\n⚠️ <i>Weapon image(s) unavailable</i>", reply_markup=buy_markup)

    if ok:
        log.info(f"✅ Alert sent → {acc_id}")
        seen.add(acc_id)
        save_seen(seen)
    else:
        log.error(f"❌ Alert failed → {acc_id}")

    return acc if ok else None


async def scan_filter(driver: webdriver.Chrome, session: aiohttp.ClientSession, filter_url: str, seen: set, queued_this_cycle: set) -> int:
    log.info(f"Scanning → {filter_url}")
    html = await asyncio.to_thread(selenium_get_html, driver, filter_url)
    listings = parse_listings(html, filter_url)
    fresh = [r for r in listings if r["id"] not in seen and r["id"] not in queued_this_cycle]

    if not fresh:
        log.info(f"No new listings ({len(listings)} on page, all already known)")
        return 0

    log.info(f"{len(fresh)} new listing(s) out of {len(listings)} on page → processing")
    new = 0
    for raw in fresh:
        queued_this_cycle.add(raw["id"])
        await asyncio.sleep(RATE_LIMIT_DELAY)
        acc = await process_account(driver, session, raw, seen)
        if acc:
            new += 1
    return new


async def main():
    seen = load_seen()
    driver = build_driver()
    log.info(f"Seen cache: {len(seen)} | Filters: {len(FILTERS)} | Interval: {CHECK_INTERVAL}s")

    connector = aiohttp.TCPConnector(ssl=False)
    cycle_count = 0
    stats = {"cycles": 0, "listings_scanned": 0, "alerts_sent": 0}
    last_heartbeat = time.time()

    async with aiohttp.ClientSession(connector=connector) as session:
        await send_message(
            session,
            f"🟢 <b>lzt-sniper started</b>\n"
            f"Watching {len(FILTERS)} filter(s) · checking every {CHECK_INTERVAL}s\n"
            f"Seen cache: {len(seen)} account(s)",
        )

        try:
            while True:
                cycle_start = time.monotonic()
                log.info("=" * 55)
                log.info(f"Cycle → {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                if cycle_count and cycle_count % DRIVER_RESTART_EVERY_N_CYCLES == 0:
                    log.info(f"Scheduled driver refresh (every {DRIVER_RESTART_EVERY_N_CYCLES} cycles)")
                    driver = restart_driver(driver)
                elif not driver_is_alive(driver):
                    log.warning("Driver found dead at cycle start")
                    driver = restart_driver(driver)

                total = 0
                queued_this_cycle: set = set()
                for furl in FILTERS:
                    try:
                        total += await scan_filter(driver, session, furl, seen, queued_this_cycle)
                        stats["listings_scanned"] += 1
                    except (InvalidSessionIdException, WebDriverException) as e:
                        log.error(f"Driver crashed while scanning {furl}: {e}")
                        driver = restart_driver(driver)
                        continue
                    except Exception as e:
                        log.error(f"Unexpected error scanning {furl}: {e}")
                        continue
                    await asyncio.sleep(2)

                cycle_count += 1
                stats["cycles"] += 1
                stats["alerts_sent"] += total

                elapsed = time.monotonic() - cycle_start
                log.info(f"Cycle done in {elapsed:.1f}s — {total} new account(s)")
                if elapsed > CHECK_INTERVAL:
                    log.warning(
                        f"Cycle took {elapsed:.1f}s, longer than the {CHECK_INTERVAL}s interval — "
                        f"scanning back-to-back with no gap."
                    )

                if HEARTBEAT_INTERVAL_SECONDS and (time.time() - last_heartbeat) >= HEARTBEAT_INTERVAL_SECONDS:
                    await send_message(
                        session,
                        f"💓 <b>Still running</b>\n"
                        f"Cycles: {stats['cycles']} · Alerts sent: {stats['alerts_sent']}\n"
                        f"Seen cache: {len(seen)} account(s)",
                    )
                    last_heartbeat = time.time()

                await asyncio.sleep(max(0, CHECK_INTERVAL - elapsed))
        finally:
            try:
                driver.quit()
            except Exception:
                pass


async def test_single_listing(acc_id: str):
    driver = build_driver()
    try:
        url = f"https://lzt.market/{acc_id}/"
        html = selenium_get_html(driver, url)
        stats = parse_stats(html)
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
    driver = build_driver()
    try:
        url = f"https://lzt.market/{acc_id}/"
        html = selenium_get_html(driver, url)
        soup = BeautifulSoup(html, "html.parser")
        print(f"\n=== DEBUG TYPE/ORIGIN — {url} ===\n")
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
        asyncio.run(test_single_listing(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "debugtype":
        asyncio.run(debug_type(sys.argv[2]))
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Stopped.")
