"""Image downloader for saving listing images to local disk."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_LISTING = 3
DEFAULT_IMAGES_DIR = "./images"


class ImageDownloader:
    """Downloads and saves listing images to local disk.

    Images are saved to: <images_dir>/<listing_id>/1.jpg, 2.jpg, 3.jpg
    Max 3 images per listing.
    """

    def __init__(self, images_dir: str = DEFAULT_IMAGES_DIR):
        self._images_dir = Path(images_dir)
        self._images_dir.mkdir(parents=True, exist_ok=True)

    async def download_listing_images(
        self, listing_id: str, image_urls: list[str]
    ) -> list[str]:
        """Download up to 3 images for a listing.

        Args:
            listing_id: Unique listing identifier (used as folder name)
            image_urls: List of image URLs from the listing

        Returns:
            List of local file paths for successfully downloaded images
        """
        urls_to_download = image_urls[:MAX_IMAGES_PER_LISTING]
        if not urls_to_download:
            return []

        # Create listing directory
        listing_dir = self._images_dir / listing_id
        listing_dir.mkdir(parents=True, exist_ok=True)

        saved_paths: list[str] = []

        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(urls_to_download, 1):
                try:
                    local_path = await self._download_single(
                        session, url, listing_dir, i
                    )
                    if local_path:
                        saved_paths.append(local_path)
                except Exception as e:
                    logger.warning(
                        "Failed to download image %d for listing %s: %s",
                        i, listing_id, str(e),
                    )

        logger.debug(
            "Downloaded %d/%d images for listing %s",
            len(saved_paths), len(urls_to_download), listing_id,
        )
        return saved_paths

    async def _download_single(
        self, session: aiohttp.ClientSession, url: str, listing_dir: Path, index: int
    ) -> str | None:
        """Download a single image and save it.

        Returns the local file path on success, None on failure.
        """
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.debug("Image download returned status %d: %s", resp.status, url)
                    return None

                # Determine file extension from content type
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                ext = self._ext_from_content_type(content_type)

                filename = f"{index}{ext}"
                file_path = listing_dir / filename

                # Read and save
                data = await resp.read()
                if not data:
                    return None

                file_path.write_bytes(data)
                return str(file_path)
        except Exception as e:
            logger.debug("Error downloading %s: %s", url, str(e))
            return None

    @staticmethod
    def _ext_from_content_type(content_type: str) -> str:
        """Map content type to file extension."""
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        # Extract mime type (ignore charset etc.)
        mime = content_type.split(";")[0].strip().lower()
        return mapping.get(mime, ".jpg")

    def get_listing_images(self, listing_id: str) -> list[str]:
        """Get existing local image paths for a listing."""
        listing_dir = self._images_dir / listing_id
        if not listing_dir.exists():
            return []
        return sorted(str(p) for p in listing_dir.iterdir() if p.is_file())
