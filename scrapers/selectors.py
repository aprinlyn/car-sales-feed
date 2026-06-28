"""CSS/XPath selectors for scrapers.

Centralized selector definitions so they can be updated in one place
when a platform changes its HTML structure.
"""


class OLXSelectors:
    """Selectors for OLX Indonesia (olx.co.id)."""

    # --- Listing results page ---
    LISTING_CARD = "[data-aut-id='itemBox']"
    LISTING_LINK = "a"
    ITEM_TITLE = "[data-aut-id='itemTitle']"
    ITEM_PRICE = "[data-aut-id='itemPrice']"
    ITEM_LOCATION = "[data-aut-id='item-location']"
    ITEM_DATE = "[data-aut-id='item-date']"

    # --- Pagination ---
    NEXT_PAGE = (
        "[data-aut-id='btnNext'], "
        "a[aria-label='Next'], "
        "button[aria-label='Next'], "
        ".pagination a:last-child"
    )

    # --- Listing detail page ---
    DESCRIPTION = "[data-aut-id='itemDescriptionContent']"
    DESCRIPTION_EXPAND = (
        "button:has-text('Selengkapnya'), "
        "a:has-text('Selengkapnya'), "
        "[data-aut-id='btnLoadMore'], "
        "span:has-text('Selengkapnya')"
    )
    SELLER_PROFILE_LINK = "[data-aut-id='profileCard'] a"
    PHONE_BUTTON = "[data-aut-id='btnCall']"
    GALLERY_IMAGES = "[data-aut-id='gallery'] img, [data-aut-id='image-gallery'] img"
    VEHICLE_DETAILS = "[data-aut-id='itemDetails'] li, .detail-item"
    DETAIL_LOCATION = "[data-aut-id='itemLocation']"

    # --- Seller profile page ---
    SELLER_JOIN_DATE = [
        "[data-aut-id='memberSince']",
        "[class*='member-since']",
        "[class*='joinDate']",
        "span:has-text('Member since')",
        "span:has-text('Bergabung')",
    ]
    SELLER_PROFILE_AREA = (
        "[data-aut-id='profileCard'], [class*='profile'], [class*='seller']"
    )

    # --- Captcha detection ---
    CAPTCHA_INDICATORS = [
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "[class*='captcha']",
        "[id*='captcha']",
        "#challenge-running",
        ".cf-browser-verification",
    ]
