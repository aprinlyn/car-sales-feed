"""Pipeline scheduler with independent cron schedules for scraping and publishing."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.manager import ConfigManager
from models.listing import PublishingStats
from persistence.database import DatabaseManager
from publishers.threads import ThreadsPublisher
from publishers.twitter import TwitterPublisher
from scrapers.olx import OLXScraper
from scoring.scorer import TrustScorer

logger = logging.getLogger(__name__)


class PipelineScheduler:
    """Orchestrates scraping/scoring and publishing on independent schedules.

    Features:
    - Two independent cron schedules (scrape and publish)
    - Overlap guard: skips run if previous same-type run still in progress
    - Run summaries with duration and counts
    - Graceful error handling: persists partial results on failure
    """

    def __init__(self, config: ConfigManager):
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self._scrape_running = False
        self._publish_running = False
        self._db = DatabaseManager(config)
        self._scorer = TrustScorer(config)

    def start(self) -> None:
        """Configure and start the scheduler based on config flags."""
        scrape_cron = self.config.get("scheduling.scrape_cron", "0 7 * * *")
        publish_cron = self.config.get("scheduling.publish_cron", "0 17 * * *")
        enable_scrape = self.config.get("scheduling.enable_scrape", True)
        enable_publish = self.config.get("scheduling.enable_publish", True)

        # Only register the jobs that are enabled in config
        if enable_scrape:
            self.scheduler.add_job(
                self.run_scrape_and_score,
                CronTrigger.from_crontab(scrape_cron),
                id="scrape_and_score",
                name="Scrape and Score",
                replace_existing=True,
            )

        if enable_publish:
            self.scheduler.add_job(
                self.run_publish,
                CronTrigger.from_crontab(publish_cron),
                id="publish",
                name="Publish to Social Media",
                replace_existing=True,
            )

        self.scheduler.start()
        logger.info(
            "Scheduler started — scrape: '%s' (%s), publish: '%s' (%s)",
            scrape_cron,
            "enabled" if enable_scrape else "disabled",
            publish_cron,
            "enabled" if enable_publish else "disabled",
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    async def run_scrape_and_score(self) -> None:
        """Execute scraping pipeline with overlap guard.

        Runs OLX scraper (Facebook skipped for MVP), scores results, persists to DB.
        """
        if self._scrape_running:
            logger.warning("Scrape run skipped — previous run still in progress")
            return

        self._scrape_running = True
        start_time = time.time()
        total_scraped = 0
        total_scored = 0
        total_skipped = 0
        total_failed = 0
        run_complete = False

        try:
            # Reload config for any changes
            self.config.reload()

            # Scrape OLX
            logger.info("Starting scrape run...")
            olx_scraper = OLXScraper(self.config)
            listings = await olx_scraper.scrape()
            total_scraped = len(listings)
            total_skipped = olx_scraper.stats.total_skipped
            total_failed = olx_scraper.stats.total_failed

            # Score and persist
            for listing in listings:
                try:
                    scored = self._scorer.score(listing)
                    self._db.store_listing(scored)
                    total_scored += 1
                except Exception as e:
                    total_failed += 1
                    logger.error("Failed to score/store listing '%s': %s", listing.title, str(e))

            run_complete = True

        except Exception as e:
            logger.error("Scrape run failed mid-execution: %s", str(e))
        finally:
            self._scrape_running = False
            duration = time.time() - start_time

            status = "complete" if run_complete else "incomplete"
            logger.info(
                "Scrape run %s (%.1fs): scraped=%d, scored=%d, skipped=%d, failed=%d",
                status, duration, total_scraped, total_scored, total_skipped, total_failed,
            )

    async def run_publish(self) -> None:
        """Execute publishing pipeline with overlap guard.

        Runs Twitter then Threads publishers sequentially.
        """
        if self._publish_running:
            logger.warning("Publish run skipped — previous run still in progress")
            return

        self._publish_running = True
        start_time = time.time()

        twitter_stats = PublishingStats(platform="twitter")
        threads_stats = PublishingStats(platform="threads")

        try:
            # Reload config for any changes
            self.config.reload()

            # Check if there are any safe unpublished listings
            safe_for_twitter = self._db.get_unpublished("twitter", "safe", limit=1)
            safe_for_threads = self._db.get_unpublished("threads", "safe", limit=1)

            if not safe_for_twitter and not safe_for_threads:
                logger.info("No unpublished safe listings to publish. Run complete.")
                return

            # Publish to Twitter
            if safe_for_twitter:
                logger.info("Starting Twitter publishing...")
                twitter_pub = TwitterPublisher(self.config, self._db)
                twitter_stats = await twitter_pub.publish_batch()

            # Publish to Threads
            if safe_for_threads:
                logger.info("Starting Threads publishing...")
                threads_pub = ThreadsPublisher(self.config, self._db)
                threads_stats = await threads_pub.publish_batch()

        except Exception as e:
            logger.error("Publish run failed: %s", str(e))
        finally:
            self._publish_running = False
            duration = time.time() - start_time

            logger.info(
                "Publish run complete (%.1fs): Twitter=%d published/%d failed, "
                "Threads=%d published/%d failed",
                duration,
                twitter_stats.total_published, twitter_stats.total_failed,
                threads_stats.total_published, threads_stats.total_failed,
            )
