"""Quick script to run the OLX scraper directly."""

import asyncio
import logging

from config.manager import ConfigManager
from scrapers.olx import OLXScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    config = ConfigManager(yaml_path="config.yaml")
    config.validate()

    scraper = OLXScraper(config)
    listings = await scraper.scrape()

    print(f"\n{'='*60}")
    print(f"Scraped {len(listings)} listings from OLX")
    print(f"{'='*60}")

    for i, listing in enumerate(listings[:5], 1):
        print(f"\n[{i}] {listing.title}")
        print(f"    Price: Rp {listing.price:,.0f}")
        print(f"    Location: {listing.location}")
        print(f"    URL: {listing.source_url}")


if __name__ == "__main__":
    asyncio.run(main())
