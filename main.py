"""Car Sales Feed — main application entry point.

Starts the scheduler with two independent cron jobs:
- Scrape & Score (default: daily at 07:00)
- Publish to Twitter & Threads (default: daily at 17:00)

Usage:
    python3 main.py
"""

import asyncio
import logging
import signal
import sys

from config.manager import ConfigManager
from orchestration.scheduler import PipelineScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    # Load and validate configuration
    config = ConfigManager(yaml_path="config.yaml")
    try:
        config.validate()
    except Exception as e:
        logger.error("Configuration error: %s", str(e))
        sys.exit(1)

    # Initialize scheduler
    scheduler = PipelineScheduler(config)

    # Handle graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig, frame):
        logger.info("Received %s, shutting down...", signal.Signals(sig).name)
        scheduler.stop()
        loop.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start scheduler
    scheduler.start()

    logger.info("Car Sales Feed is running. Press Ctrl+C to stop.")
    logger.info(
        "Schedules: scrape='%s' (%s), publish='%s' (%s)",
        config.get("scheduling.scrape_cron", "0 7 * * *"),
        "enabled" if config.get("scheduling.enable_scrape", True) else "disabled",
        config.get("scheduling.publish_cron", "0 17 * * *"),
        "enabled" if config.get("scheduling.enable_publish", True) else "disabled",
    )

    # Keep the event loop running
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.stop()
        loop.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
