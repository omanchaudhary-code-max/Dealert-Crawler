import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

DARAZ_BASE = "https://www.daraz.com.np"

IS_CI = bool(os.getenv("CI") or os.getenv("GITHUB_ACTIONS"))

SELECTORS = {
    "product_cards": "div[data-qa-locator='product-item']",
    "card_link": "a",
    "title": "h1.pdp-mod-product-badge-title, span.pdp-name",
    "current_price": (
        "span.pdp-price_type_normal, "
        "span.notranslate.pdp-price, "
        "span.pdp-price_size_xl"
    ),
    "original_price": "span.pdp-price_type_deleted",
    "seller": "a.seller-name__detail-name, span.seller-name__detail",
    "image": "div.gallery-preview-panel__content img, img.pdp-image",
    "item_id_pattern": r"-i(\d+)(?:-s\d+)?\.html",
    "next_page": "li.ant-pagination-next:not(.ant-pagination-disabled) button",
    "middleware_overlay": ".J_MIDDLEWARE_FRAME_WIDGET",
}

DELISTED_PAGE_MARKERS = [
    "We're Sorry, an error has occurred",
    "We seem to have lost this page",
]

MAX_RETRIES   = 3
RETRY_BACKOFF = 5

SEARCH_BASED_CATEGORIES = {
    "books": "book",
    "kitchen-appliances": "kitchen",
    "cameras": "camera",
}


def build_listing_url(category: str) -> str:
    if category in SEARCH_BASED_CATEGORIES:
        query = SEARCH_BASED_CATEGORIES[category]
        return f"{DARAZ_BASE}/catalog/?q={query}"
    return f"{DARAZ_BASE}/{category}/"


def _build_proxy_auth_extension(proxy_url: str) -> str:
    """
    Chrome's --proxy-server flag does NOT support embedded user:pass@
    credentials in the URL — Chrome silently strips them, leaving every
    request unauthenticated against the proxy. The proxy then rejects
    the connection, and Selenium just hangs until timeout
    (manifests as ERR_NO_SUPPORTED_PROXIES in Chrome).

    This builds a small temporary Chrome extension that supplies proxy
    credentials via the chrome.webRequest.onAuthRequired API — the
    standard workaround for authenticated proxies with Selenium/Chrome.

    Returns the path to the extension directory (pass to --load-extension).
    Caller is responsible for cleaning up the directory afterward.
    """
    parsed = urlparse(proxy_url)
    host = parsed.hostname
    port = parsed.port
    username = parsed.username
    password = parsed.password

    if not all([host, port, username, password]):
        raise ValueError(
            f"PROXY_URL is missing host/port/username/password: {proxy_url!r}"
        )

    ext_dir = tempfile.mkdtemp(prefix="proxy_auth_ext_")

    manifest = {
        "manifest_version": 2,
        "name": "Proxy Auth",
        "version": "1.0.0",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage",
            "webRequest", "webRequestBlocking",
            "<all_urls>",
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "22.0.0",
    }

    background_js = f"""
    var config = {{
        mode: "fixed_servers",
        rules: {{
            singleProxy: {{
                scheme: "http",
                host: "{host}",
                port: parseInt({port})
            }},
            bypassList: ["localhost"]
        }}
    }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

    chrome.webRequest.onAuthRequired.addListener(
        function(details) {{
            return {{
                authCredentials: {{
                    username: "{username}",
                    password: "{password}"
                }}
            }};
        }},
        {{urls: ["<all_urls>"]}},
        ["blocking"]
    );
    """

    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)

    return ext_dir


class DarazCrawler:
    """
    Crawls Daraz Nepal product listing pages and extracts price data.

    TWO MODES controlled by CRAWL_MODE env var:
      discovery  — walk category listing pages, find new products, scrape them.
      tracking   — re-scrape already-known product URLs from MongoDB.

    PROXY: set PROXY_URL env var (format: http://user:pass@host:port) to
    route all traffic through an authenticated proxy. Credentials are
    injected via a temporary Chrome extension since Chrome's --proxy-server
    flag does not support inline auth.

    DELISTED PRODUCTS: Daraz shows a "We're Sorry, an error has occurred"
    page for removed/expired product URLs. This is detected fast (avoids
    wasting the full page-load timeout + 3 retries on a dead URL) and
    returned with is_delisted=True so the caller can mark it in MongoDB
    and skip it on future tracking runs.
    """

    def __init__(self, delay_min: int = 10, delay_max: int = 20):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.driver: Optional[uc.Chrome] = None
        self._proxy_ext_dir: Optional[str] = None

    # ──────────────────────────── Driver lifecycle ──────────────────────────────

    def _build_driver(self) -> uc.Chrome:
        opts = uc.ChromeOptions()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")

        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.managed_default_content_settings.fonts": 2,
        }
        opts.add_experimental_option("prefs", prefs)
        opts.add_argument("--blink-settings=imagesEnabled=false")

        if IS_CI:
            opts.add_argument("--headless=new")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--disable-web-security")
            opts.add_argument("--allow-running-insecure-content")
            opts.add_argument("--start-maximized")
            opts.add_argument("--ignore-certificate-errors")
            opts.add_argument("--disable-popup-blocking")

        proxy_url = os.getenv("PROXY_URL")
        if proxy_url:
            self._proxy_ext_dir = _build_proxy_auth_extension(proxy_url)
            opts.add_argument(f"--load-extension={self._proxy_ext_dir}")
            logger.info("Routing Chrome traffic through configured proxy (via auth extension).")
        elif IS_CI:
            logger.warning(
                "Running in CI with NO PROXY_URL configured. "
                "Daraz is likely to block or CAPTCHA a datacenter IP."
            )

        ua = self._pick_user_agent()
        opts.add_argument(f"--user-agent={ua}")

        version_main_env = os.getenv("CHROME_VER")
        version_main = int(version_main_env) if version_main_env else None

        driver = uc.Chrome(options=opts, version_main=version_main)

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'permissions', {
                    get: () => ({ query: () => Promise.resolve({ state: 'granted' }) })
                });
            """
        })

        return driver

    def _pick_user_agent(self) -> str:
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
        ]
        return random.choice(agents)

    def __enter__(self):
        logger.info(f"Starting Chrome driver (undetected-chromedriver, CI={IS_CI})...")
        self.driver = self._build_driver()
        return self

    def __exit__(self, *args):
        if self.driver:
            self.driver.quit()
            logger.info("Chrome driver closed.")
        if self._proxy_ext_dir:
            shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
            logger.debug("Cleaned up temporary proxy-auth extension directory.")

    # ──────────────────────────── Delay utilities ───────────────────────────────

    def _polite_wait(self, extra: float = 0.0):
        delay = random.uniform(self.delay_min, self.delay_max) + extra
        logger.debug(f"Waiting {delay:.1f}s...")
        time.sleep(delay)

    def _retry_wait(self, attempt: int):
        delay = RETRY_BACKOFF * attempt + random.uniform(2, 5)
        logger.info(f"  Retry backoff: waiting {delay:.1f}s before attempt {attempt + 1}...")
        time.sleep(delay)

    # ──────────────────────────── Page helpers ──────────────────────────────────

    def _wait_for(self, css: str, timeout: int = 15):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css))
        )

    def _safe_text(self, css: str) -> Optional[str]:
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, css)
            return el.text.strip() or None
        except NoSuchElementException:
            return None

    def _is_delisted_page(self) -> bool:
        """
        Detect Daraz's 'We're Sorry, an error has occurred' delisted/404
        page so we fail fast instead of waiting out the full title-wait
        timeout (which never resolves on this page) plus 3 retries.
        """
        try:
            page_text = self.driver.find_element(By.TAG_NAME, "body").text
            return any(marker in page_text for marker in DELISTED_PAGE_MARKERS)
        except WebDriverException:
            return False

    # ──────────────────────────── Debug artifacts on failure ────────────────────

    def _dump_debug_artifacts(self, url: str):
        """
        Save a screenshot + HTML snapshot on failure so a blocked/CAPTCHA
        page is diagnosable from a GitHub Actions artifact instead of
        guessing blind. Never raises — a failure here should never mask
        the original error.
        """
        try:
            os.makedirs("logs/debug", exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", url)[-60:]
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            self.driver.save_screenshot(f"logs/debug/{ts}_{safe_name}.png")
            with open(f"logs/debug/{ts}_{safe_name}.html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
        except Exception as e:
            logger.debug(f"Could not save debug artifacts: {e}")

    # ──────────────────────────── Overlay dismissal ─────────────────────────────

    def _dismiss_overlay(self):
        """
        Dismiss Daraz's J_MIDDLEWARE_FRAME_WIDGET anti-bot overlay.
        """
        if IS_CI:
            time.sleep(3)

        try:
            WebDriverWait(self.driver, 8).until(
                EC.invisibility_of_element_located(
                    (By.CSS_SELECTOR, SELECTORS["middleware_overlay"])
                )
            )
            return
        except TimeoutException:
            pass

        try:
            self.driver.execute_script("""
                var overlay = document.querySelector('.J_MIDDLEWARE_FRAME_WIDGET');
                if (overlay) overlay.parentNode.removeChild(overlay);
                document.body.style.overflow = '';
                document.documentElement.style.overflow = '';
            """)
            time.sleep(1)
            logger.debug("Middleware overlay removed via JS.")
        except WebDriverException as e:
            logger.debug(f"JS overlay removal failed: {e}")

        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except WebDriverException:
            pass

    # ──────────────────────────── Price parsing ─────────────────────────────────

    def _parse_price(self, raw: Optional[str]) -> Optional[float]:
        if not raw:
            return None
        cleaned = re.sub(r"[^\d,]", "", raw).strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(",", "")
        if not cleaned:
            return None
        try:
            value = float(cleaned)
            if value < 1 or value > 10_000_000:
                logger.warning(f"Price out of expected range: {value} (raw: {raw!r})")
                return None
            return value
        except ValueError:
            logger.warning(f"Could not parse price: {raw!r}")
            return None

    # ──────────────────────────── Product detail ────────────────────────────────

    def _scrape_product_detail(self, url: str) -> Optional[dict]:
        match = re.search(SELECTORS["item_id_pattern"], url)
        item_id = match.group(1) if match else None

        try:
            self.driver.get(url)

            # Brief settle, then fast-fail check for a delisted/error page
            # BEFORE burning the full title-wait timeout on a page that
            # will never have a title.
            time.sleep(1.5)
            self._dismiss_overlay()

            if self._is_delisted_page():
                logger.info(f"Product delisted/removed by Daraz: {url}")
                return {
                    "item_id": item_id,
                    "url": url,
                    "is_delisted": True,
                    "scraped_at": datetime.now(timezone.utc),
                }

            self._wait_for(SELECTORS["title"], timeout=20)

            extra = 3.0 if IS_CI else 1.0
            self._polite_wait(extra=extra)

            title        = self._safe_text(SELECTORS["title"])
            raw_current  = self._safe_text(SELECTORS["current_price"])
            raw_original = self._safe_text(SELECTORS["original_price"])
            seller       = self._safe_text(SELECTORS["seller"])

            current_price  = self._parse_price(raw_current)
            original_price = self._parse_price(raw_original)

            if not current_price:
                logger.debug(
                    f"Price debug — raw_current={raw_current!r}, "
                    f"raw_original={raw_original!r}, url={url}"
                )

            is_promotional = original_price is not None and (
                original_price > (current_price or 0)
            )

            image_url = None
            try:
                img_el = self.driver.find_element(By.CSS_SELECTOR, SELECTORS["image"])
                image_url = (
                    img_el.get_attribute("src") or
                    img_el.get_attribute("data-src") or
                    None
                )
                if image_url and (
                    image_url.startswith("data:") or image_url.strip() == ""
                ):
                    image_url = None
            except NoSuchElementException:
                logger.debug(f"No image element found at {url}")

            if not current_price:
                logger.warning(f"No price found at {url}")
                self._dump_debug_artifacts(url)
                return None

            return {
                "item_id":        item_id,
                "title":          title,
                "url":            url,
                "current_price":  current_price,
                "original_price": original_price,
                "is_promotional": is_promotional,
                "seller_name":    seller,
                "image_url":      image_url,
                "image_verified": image_url is not None,
                "scraped_at":     datetime.now(timezone.utc),
                "source":         "crawler",
                "is_delisted":    False,
            }

        except TimeoutException:
            logger.warning(f"Timeout loading product page: {url}")
            self._dump_debug_artifacts(url)
            return None
        except WebDriverException as e:
            logger.error(f"WebDriver error on {url}: {e}")
            self._dump_debug_artifacts(url)
            return None

    def _scrape_with_retry(self, url: str) -> Optional[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            result = self._scrape_product_detail(url)

            if result is not None:
                if result.get("is_delisted"):
                    logger.info(f"  Confirmed delisted, not retrying: {url}")
                    return result

                if attempt > 1:
                    logger.info(f"  ✓ Succeeded on attempt {attempt}: {url}")
                return result

            if attempt < MAX_RETRIES:
                logger.warning(
                    f"  Attempt {attempt}/{MAX_RETRIES} failed for {url} — retrying..."
                )
                self._retry_wait(attempt)
            else:
                logger.error(
                    f"  ✗ All {MAX_RETRIES} attempts failed for {url} — skipping."
                )

        return None

    # ──────────────────────────── Category listing (discovery) ──────────────────

    def _extract_links_from_current_page(self, links: list[str], max_products: int) -> None:
        cards = self.driver.find_elements(By.CSS_SELECTOR, SELECTORS["product_cards"])
        for card in cards:
            if len(links) >= max_products:
                break
            try:
                anchor = card.find_element(By.CSS_SELECTOR, SELECTORS["card_link"])
                href = anchor.get_attribute("href")
                if href and "daraz.com.np/products/" in href:
                    clean = href.split("?")[0].split("#")[0]
                    if clean not in links:
                        links.append(clean)
            except NoSuchElementException:
                continue

    def _collect_product_links(self, category: str, max_products: int) -> list[str]:
        links = []
        page = 1
        listing_url = build_listing_url(category)

        try:
            self.driver.get(listing_url)
            self._wait_for(SELECTORS["product_cards"], timeout=20)
            self._dismiss_overlay()
            self._polite_wait()
        except TimeoutException:
            logger.warning(f"Timeout on listing page 1 for /{category}/, stopping category.")
            self._dump_debug_artifacts(listing_url)
            return links
        except WebDriverException as e:
            logger.error(f"WebDriver error loading {listing_url}: {e}")
            return links

        while len(links) < max_products:
            logger.info(f"Listing page {page} for /{category}/ — {len(links)} links so far")

            cards_before = self.driver.find_elements(By.CSS_SELECTOR, SELECTORS["product_cards"])
            if not cards_before:
                logger.info(f"No products on page {page}, stopping.")
                self._dump_debug_artifacts(listing_url)
                break

            self._extract_links_from_current_page(links, max_products)

            if len(links) >= max_products:
                break

            try:
                next_btn = self.driver.find_element(By.CSS_SELECTOR, SELECTORS["next_page"])
            except NoSuchElementException:
                logger.info("Last page reached.")
                break

            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", next_btn
                )
                self._polite_wait(extra=0.5)
                self._dismiss_overlay()
                next_btn.click()
            except WebDriverException as e:
                logger.warning(f"Click on next-page button failed: {e}")
                break

            page += 1

            try:
                first_card_before = cards_before[0]
                WebDriverWait(self.driver, 15).until(EC.staleness_of(first_card_before))
                self._wait_for(SELECTORS["product_cards"], timeout=20)
            except (TimeoutException, StaleElementReferenceException):
                logger.warning(
                    f"Timeout waiting for page {page} content to load on /{category}/, stopping."
                )
                break
            except WebDriverException as e:
                logger.error(f"WebDriver error advancing to page {page}: {e}")
                break

            self._polite_wait()

        logger.info(f"Collected {len(links)} links from /{category}/")
        return links

    # ──────────────────────────── Public API ────────────────────────────────────

    def crawl_category(
        self,
        category: str,
        max_products: int = 50,
        save_callback=None,
    ) -> list[dict]:
        """
        DISCOVERY MODE — walk listing pages, find new products, scrape them.
        """
        logger.info(f"=== [DISCOVERY] Crawling category: {category} (max {max_products}) ===")
        links = self._collect_product_links(category, max_products)

        results      = []
        saved_count  = 0
        failed_count = 0

        for i, link in enumerate(links, 1):
            logger.info(f"  [{i}/{len(links)}] {link}")

            data = self._scrape_with_retry(link)

            if data:
                data["category"] = category
                results.append(data)

                if save_callback:
                    try:
                        save_callback(data)
                        saved_count += 1
                    except Exception as e:
                        logger.error(f"  Save callback failed for {link}: {e}")
            else:
                failed_count += 1

            self._polite_wait()

        logger.info(
            f"=== Category {category} done: "
            f"{len(results)} scraped, {saved_count} saved immediately, "
            f"{failed_count} failed after {MAX_RETRIES} retries ==="
        )
        return results

    def crawl_known_products(
        self,
        products: list[dict],
        save_callback=None,
    ) -> list[dict]:
        """
        TRACKING MODE — re-scrape a pre-known list of products from MongoDB.
        """
        logger.info(f"=== [TRACKING] Re-scraping {len(products)} known products ===")

        results        = []
        saved_count    = 0
        failed_count   = 0
        delisted_count = 0

        for i, product in enumerate(products, 1):
            url      = product.get("url")
            category = product.get("category", "unknown")

            if not url:
                logger.warning(f"  [{i}] Skipping product with no URL: {product}")
                failed_count += 1
                continue

            logger.info(f"  [{i}/{len(products)}] {url}")

            data = self._scrape_with_retry(url)

            if data:
                data["category"] = category
                results.append(data)

                if data.get("is_delisted"):
                    delisted_count += 1

                if save_callback:
                    try:
                        save_callback(data)
                        saved_count += 1
                    except Exception as e:
                        logger.error(f"  Save callback failed for {url}: {e}")
            else:
                failed_count += 1

            self._polite_wait()

        logger.info(
            f"=== Tracking run done: "
            f"{len(results)} scraped, {saved_count} saved immediately, "
            f"{delisted_count} newly delisted, "
            f"{failed_count} failed after {MAX_RETRIES} retries ==="
        )
        return results