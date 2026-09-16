import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from daraz_crawler import DarazCrawler
from storage import SPTDASStorage

load_dotenv()

logger = logging.getLogger(__name__)


def run_crawl() -> bool:
    """
    Execute one full crawl cycle across all configured categories.

    MODE is controlled by the CRAWL_MODE env var:
      discovery — walk category listing pages, find new product URLs, scrape.
      tracking  — re-scrape every known product URL from MongoDB.

    Returns True if the run completed without a fatal error,
    False only if MongoDB is unreachable or the run catastrophically fails.
    """

    mongo_uri    = os.getenv("MONGO_URI",    "mongodb://localhost:27017")
    db_name      = os.getenv("MONGO_DB",     "daraz_db")
    delay_min    = int(os.getenv("DELAY_MIN",    "10"))
    delay_max    = int(os.getenv("DELAY_MAX",    "20"))
    max_per_cat  = int(os.getenv("MAX_PRODUCTS_PER_CATEGORY", "50"))
    crawl_mode   = os.getenv("CRAWL_MODE", "discovery").strip().lower()

    categories_raw = os.getenv(
        "CATEGORIES",
        "laptops,smartphones,televisions,headphones,shoes-men",
    )
    categories = [c.strip() for c in categories_raw.split(",") if c.strip()]

    logger.info("=" * 60)
    logger.info(f"SPTDAS Crawl started at {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Mode       : {crawl_mode.upper()}")
    logger.info(f"Categories : {categories}")
    logger.info(f"Max/cat    : {max_per_cat}  (discovery mode only)")
    logger.info(f"Delay      : {delay_min}–{delay_max}s")
    logger.info("=" * 60)

    if crawl_mode not in ("discovery", "tracking"):
        logger.error(f"Unknown CRAWL_MODE '{crawl_mode}'. Must be 'discovery' or 'tracking'.")
        return False

    storage = SPTDASStorage(uri=mongo_uri, db_name=db_name)
    try:
        storage.connect()
    except Exception as e:
        logger.critical(f"Cannot connect to MongoDB: {e}")
        return False

    run_id = storage.start_crawl_run(categories)

    total_stats = {
        "total_products": 0,
        "total_new":      0,
        "total_updated":  0,
        "total_errors":   0,
    }
    category_results = {}

    try:
        with DarazCrawler(delay_min=delay_min, delay_max=delay_max) as crawler:

            if crawl_mode == "tracking":
                logger.info("TRACKING MODE — loading known products from MongoDB...")
                all_known = storage.get_all_tracked_urls()
                logger.info(f"Found {len(all_known)} products to re-scrape.")

                if not all_known:
                    logger.warning(
                        "No products in DB to track. "
                        "Run in discovery mode first to populate the basket."
                    )
                    storage.finish_crawl_run(run_id, total_stats)
                    return True

                for cat in categories:
                    category_results[cat] = {"scraped": 0, "new": 0, "updated": 0, "errors": 0}
                category_results["_other"] = {"scraped": 0, "new": 0, "updated": 0, "errors": 0}

                def make_tracking_save_callback(c_stats_map):
                    def save_one(product: dict):
                        cat = product.get("category", "_other")
                        c_stats = c_stats_map.get(cat, c_stats_map["_other"])
                        try:
                            result = storage.save_products([product], run_id)
                            c_stats["new"]     += result.get("new", 0)
                            c_stats["updated"] += result.get("updated", 0)
                            c_stats["errors"]  += result.get("errors", 0)
                            c_stats["scraped"] += 1
                            logger.debug(
                                f"  Saved {product.get('item_id')} "
                                f"({product.get('current_price')} NPR)"
                            )
                        except Exception as e:
                            logger.error(
                                f"  Save failed for {product.get('url', '?')}: {e}"
                            )
                            c_stats["errors"] += 1
                    return save_one

                save_cb = make_tracking_save_callback(category_results)
                crawler.crawl_known_products(all_known, save_callback=save_cb)

                for cat, stats in category_results.items():
                    total_stats["total_products"] += stats["scraped"]
                    total_stats["total_new"]      += stats["new"]
                    total_stats["total_updated"]  += stats["updated"]
                    total_stats["total_errors"]   += stats["errors"]

            else:
                for category in categories:
                    logger.info(f"\n>>> Starting category: {category}")

                    cat_stats = {"scraped": 0, "new": 0, "updated": 0, "errors": 0}

                    def make_save_callback(cat_name, c_stats):
                        def save_one(product: dict):
                            try:
                                result = storage.save_products([product], run_id)
                                c_stats["new"]     += result.get("new", 0)
                                c_stats["updated"] += result.get("updated", 0)
                                c_stats["errors"]  += result.get("errors", 0)
                                c_stats["scraped"] += 1
                                logger.debug(
                                    f"  Saved {product.get('item_id')} "
                                    f"({product.get('current_price')} NPR)"
                                )
                            except Exception as e:
                                logger.error(
                                    f"  Immediate save failed for "
                                    f"{product.get('url', '?')}: {e}"
                                )
                                c_stats["errors"] += 1
                        return save_one

                    save_cb = make_save_callback(category, cat_stats)

                    try:
                        products = crawler.crawl_category(
                            category,
                            max_products=max_per_cat,
                            save_callback=save_cb,
                        )

                        if not products:
                            logger.warning(f"No products returned for: {category}")
                            storage.log_error(run_id, category, "", "Zero products returned")
                            cat_stats["errors"] += 1

                    except Exception as e:
                        logger.error(f"Category {category} failed: {e}", exc_info=True)
                        storage.log_error(run_id, category, "", str(e))
                        cat_stats["errors"] += 1

                    finally:
                        category_results[category] = cat_stats
                        total_stats["total_products"] += cat_stats["scraped"]
                        total_stats["total_new"]      += cat_stats["new"]
                        total_stats["total_updated"]  += cat_stats["updated"]
                        total_stats["total_errors"]   += cat_stats["errors"]

                        logger.info(
                            f"  Category summary — {category}: "
                            f"scraped={cat_stats['scraped']:3d}, "
                            f"new={cat_stats['new']:3d}, "
                            f"updated={cat_stats['updated']:3d}, "
                            f"errors={cat_stats['errors']:3d}"
                        )

        storage.finish_crawl_run(run_id, total_stats)

        logger.info("\n" + "=" * 60)
        logger.info(f"Crawl complete [{crawl_mode.upper()}]. Overall stats: {total_stats}")
        logger.info("Per-category breakdown:")
        for cat, stats in category_results.items():
            if cat == "_other" and stats["scraped"] == 0:
                continue
            status = "✓" if stats["errors"] == 0 else "⚠"
            logger.info(
                f"  {status} {cat:30s} scraped={stats['scraped']:3d}  "
                f"new={stats['new']:3d}  errors={stats['errors']:3d}"
            )
        logger.info("=" * 60 + "\n")

        return True

    except Exception as e:
        logger.critical(f"Crawl run failed catastrophically: {e}", exc_info=True)
        try:
            storage.fail_crawl_run(run_id, str(e))
        except Exception:
            pass
        return False

    finally:
        storage.close()