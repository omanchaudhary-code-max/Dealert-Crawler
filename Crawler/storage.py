import logging
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger(__name__)


class SPTDASStorage:
    """
    Handles all database operations for the crawler.

    Usage:
        db = SPTDASStorage(uri="mongodb://localhost:27017", db_name="sptdas")
        db.connect()
        db.save_products(products)
        db.close()
    """

    def __init__(self, uri: str, db_name: str):
        self.uri = uri
        self.db_name = db_name
        self._client: Optional[MongoClient] = None
        self._db = None

    # ──────────────────────────── Connection ────────────────────────────────────

    def connect(self):
        logger.info(f"Connecting to MongoDB: {self.uri} / {self.db_name}")
        self._client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
        self._client.admin.command("ping")
        self._db = self._client[self.db_name]
        self._ensure_indexes()
        logger.info("MongoDB connected.")

    def close(self):
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed.")

    # ──────────────────────────── Index setup ───────────────────────────────────

    def _ensure_indexes(self):
        """
        Create indexes on first run. Safe to call repeatedly — MongoDB
        ignores index creation if it already exists.
        """
        self._db.products.create_index(
            [("item_id", ASCENDING)], unique=True, name="idx_item_id"
        )
        self._db.price_history.create_index(
            [("item_id", ASCENDING), ("scraped_at", DESCENDING)],
            name="idx_item_scraped",
        )
        self._db.price_history.create_index(
            [("item_id", ASCENDING), ("current_price", ASCENDING)],
            name="idx_item_price",
        )
        self._db.crawl_logs.create_index(
            [("started_at", DESCENDING)], name="idx_crawl_time"
        )
        self._db.errors.create_index(
            [("crawl_run_id", ASCENDING), ("category", ASCENDING)],
            name="idx_error_run",
        )
        logger.info("Indexes ensured.")

    # ──────────────────────────── Crawl run management ──────────────────────────

    def start_crawl_run(self, categories: list[str]) -> str:
        """
        Insert a crawl log document and return its run_id string.
        This ID ties every price_history entry to the run that produced it.
        """
        doc = {
            "started_at": datetime.now(timezone.utc),
            "finished_at": None,
            "categories": categories,
            "total_products": 0,
            "total_new": 0,
            "total_updated": 0,
            "total_errors": 0,
            "status": "running",
        }
        result = self._db.crawl_logs.insert_one(doc)
        run_id = str(result.inserted_id)
        logger.info(f"Crawl run started: {run_id}")
        return run_id

    def finish_crawl_run(self, run_id: str, stats: dict):
        from bson import ObjectId
        self._db.crawl_logs.update_one(
            {"_id": ObjectId(run_id)},
            {
                "$set": {
                    "finished_at": datetime.now(timezone.utc),
                    "status": "completed",
                    **stats,
                }
            },
        )
        logger.info(f"Crawl run {run_id} finished: {stats}")

    def fail_crawl_run(self, run_id: str, reason: str):
        from bson import ObjectId
        self._db.crawl_logs.update_one(
            {"_id": ObjectId(run_id)},
            {
                "$set": {
                    "finished_at": datetime.now(timezone.utc),
                    "status": "failed",
                    "failure_reason": reason,
                }
            },
        )

    # ──────────────────────────── Product upsert ────────────────────────────────

    def save_products(self, products: list[dict], crawl_run_id: str) -> dict:
        """
        Save a batch of scraped products.

        For each product:
          1. Upsert into `products` collection (create or update metadata)
          2. Insert a new timestamped entry into `price_history`
             — but ONLY if no entry for this item_id exists within the same
               crawl run (prevents duplication if the crawler retries)

        Every price_history document is tagged with a `source` field —
        "crawler" for real automated scrapes, or whatever the caller passes
        (e.g. "manual_demo" from the admin dashboard's simulate-price
        endpoint) — so real vs. manually-inserted data is always
        distinguishable in the database.

        Returns stats dict: {new, updated, skipped, errors}
        """
        stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

        for product in products:
            try:
                item_id = product.get("item_id")
                if not item_id:
                    logger.warning(f"Product has no item_id, skipping: {product.get('title')}")
                    stats["errors"] += 1
                    continue

                # ── 1. Upsert into products collection ──────────────────────
                existing = self._db.products.find_one({"item_id": item_id})

                product_doc = {
                    "item_id":       item_id,
                    "title":         product.get("title"),
                    "url":           product.get("url"),
                    "category":      product.get("category"),
                    "seller_name":   product.get("seller_name"),
                    "last_seen":     product.get("scraped_at"),
                    "last_price":    product.get("current_price"),
                    "image_url":     product.get("image_url"),
                    "image_verified": product.get("image_verified", False),
                    "is_delisted":   product.get("is_delisted", False),
                }

                if existing:
                    self._db.products.update_one(
                        {"item_id": item_id},
                        {"$set": product_doc},
                    )
                    stats["updated"] += 1
                else:
                    product_doc["first_seen"] = product.get("scraped_at")
                    self._db.products.insert_one(product_doc)
                    stats["new"] += 1

                # ── 2. Deduplicate within crawl run ──────────────────────────
                already_in_run = self._db.price_history.find_one(
                    {"item_id": item_id, "crawl_run_id": crawl_run_id}
                )
                if already_in_run:
                    logger.debug(f"Duplicate within run, skipping: {item_id}")
                    stats["skipped"] += 1
                    continue

                # ── 3. Insert price history entry ────────────────────────────
                history_doc = {
                    "item_id":        item_id,
                    "crawl_run_id":   crawl_run_id,
                    "scraped_at":     product.get("scraped_at"),
                    "current_price":  product.get("current_price"),
                    "original_price": product.get("original_price"),
                    "is_promotional": product.get("is_promotional", False),
                    "category":       product.get("category"),
                    "is_delisted":    product.get("is_delisted", False),
                    "source":         product.get("source", "crawler"),
                }
                self._db.price_history.insert_one(history_doc)

            except PyMongoError as e:
                logger.error(f"DB error saving {product.get('item_id')}: {e}")
                stats["errors"] += 1

        logger.info(
            f"Saved batch — new: {stats['new']}, updated: {stats['updated']}, "
            f"skipped: {stats['skipped']}, errors: {stats['errors']}"
        )
        return stats

    # ──────────────────────────── Log errors ────────────────────────────────────

    def log_error(self, crawl_run_id: str, category: str, url: str, reason: str):
        try:
            self._db.errors.insert_one(
                {
                    "crawl_run_id": crawl_run_id,
                    "category":     category,
                    "url":          url,
                    "reason":       reason,
                    "logged_at":    datetime.now(timezone.utc),
                }
            )
        except PyMongoError:
            pass

    # ──────────────────────────── Tracking mode read ────────────────────────────

    def get_all_tracked_urls(self) -> list[dict]:
        """
        TRACKING MODE — return url + category for every non-delisted product.
        """
        return list(
            self._db.products.find(
                {"is_delisted": {"$ne": True}},
                {"_id": 0, "item_id": 1, "url": 1, "category": 1},
            )
        )

    # ──────────────────────────── Read helpers (for deal engine) ────────────────

    def get_price_history(self, item_id: str, limit: int = 90) -> list[dict]:
        """Return last `limit` price entries for a product, newest first."""
        return list(
            self._db.price_history.find(
                {"item_id": item_id},
                {"_id": 0, "current_price": 1, "scraped_at": 1, "is_promotional": 1},
            )
            .sort("scraped_at", DESCENDING)
            .limit(limit)
        )

    def get_all_time_low(self, item_id: str) -> Optional[float]:
        """Return the lowest price ever recorded for a product."""
        result = self._db.price_history.find_one(
            {"item_id": item_id},
            sort=[("current_price", ASCENDING)],
        )
        return result["current_price"] if result else None

    def get_products_with_min_history(self, min_entries: int = 7) -> list[dict]:
        """
        Return products that have at least `min_entries` price history records.
        """
        pipeline = [
            {"$group": {"_id": "$item_id", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gte": min_entries}}},
        ]
        item_ids = [doc["_id"] for doc in self._db.price_history.aggregate(pipeline)]
        return list(self._db.products.find({"item_id": {"$in": item_ids}}))