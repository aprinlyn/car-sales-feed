"""Run the full pipeline: scrape OLX → score listings → save to database."""

import asyncio
import logging

from config.manager import ConfigManager
from scrapers.olx import OLXScraper
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
    scraper = OLXScraper(config)
    scorer = TrustScorer(config)
    db = DatabaseManager(config)

    # Step 1: Scrape
    logger.info("Starting OLX scraping...")
    listings = await scraper.scrape()
    logger.info("Scraped %d listings", len(listings))

    # Step 2: Score and persist
    scored_count = {"safe": 0, "suspicious": 0, "unsafe": 0}
    for listing in listings:
        scored = scorer.score(listing)
        db.store_listing(scored)
        scored_count[scored.trust_level.value] += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"Pipeline complete: {len(listings)} listings scraped and scored")
    print(f"  Safe:       {scored_count['safe']}")
    print(f"  Suspicious: {scored_count['suspicious']}")
    print(f"  Unsafe:     {scored_count['unsafe']}")
    print(f"{'='*60}")

    # Show top 5 safe listings
    safe_listings = db.get_unpublished(platform="twitter", trust_level="safe", limit=5)
    if safe_listings:
        print(f"\nTop safe listings:")
        for i, stored in enumerate(safe_listings, 1):
            print(f"  [{i}] {stored.title} — Rp {stored.price:,.0f} (score: {stored.score})")
            print(f"      {stored.source_url}")


if __name__ == "__main__":
    asyncio.run(main())
