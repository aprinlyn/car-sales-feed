"""Run the Facebook Marketplace scraper: scrape → score → save to database.

Usage:
    python3 run_facebook_scraper.py

Prerequisites:
    1. Set headless: false in config.yaml
    2. Run this script once to log in manually to Facebook in the browser
    3. After login, future runs will reuse the saved session
"""

import asyncio
import logging

from config.manager import ConfigManager
from scrapers.facebook import FacebookScraper
from scoring.scorer import TrustScorer
from persistence.database import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    # Load config
    config = ConfigManager(yaml_path="config.yaml")
    config.validate()

    # Initialize components
    scraper = FacebookScraper(config)
    scorer = TrustScorer(config)
    db = DatabaseManager(config)

    # Step 1: Scrape
    logger.info("Starting Facebook Marketplace scraping...")
    listings = await scraper.scrape()
    logger.info("Scraped %d listings from Facebook", len(listings))

    # Step 2: Score and persist
    scored_count = {"safe": 0, "suspicious": 0, "unsafe": 0}
    for listing in listings:
        scored = scorer.score(listing)
        db.store_listing(scored)
        scored_count[scored.trust_level.value] += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"Facebook pipeline complete: {len(listings)} listings scraped and scored")
    print(f"  Safe:       {scored_count['safe']}")
    print(f"  Suspicious: {scored_count['suspicious']}")
    print(f"  Unsafe:     {scored_count['unsafe']}")
    print(f"{'='*60}")

    # Show top 5
    safe_listings = db.get_unpublished(platform="twitter", trust_level="safe", limit=5)
    if safe_listings:
        print(f"\nTop safe listings:")
        for i, stored in enumerate(safe_listings, 1):
            print(f"  [{i}] {stored.title} — Rp {stored.price:,.0f} (score: {stored.score})")
            print(f"      {stored.source_url}")


if __name__ == "__main__":
    asyncio.run(main())
