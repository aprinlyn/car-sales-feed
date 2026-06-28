"""Indonesian city geolocation data and OLX location filter slugs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CityLocation:
    """City geolocation and OLX filter metadata."""

    name: str
    latitude: float
    longitude: float
    olx_location_slug: str  # Used in OLX URL filter (e.g., /jakarta_g2000001)


# Supported cities with their coordinates and OLX slugs
LOCATIONS: dict[str, CityLocation] = {
    "jakarta": CityLocation(
        name="Jakarta",
        latitude=-6.2088,
        longitude=106.8456,
        olx_location_slug="jakarta_g2000001",
    ),
    "bandung": CityLocation(
        name="Bandung",
        latitude=-6.9175,
        longitude=107.6191,
        olx_location_slug="bandung_g2000007",
    ),
    "yogyakarta": CityLocation(
        name="Yogyakarta",
        latitude=-7.7956,
        longitude=110.3695,
        olx_location_slug="yogyakarta_g2000005",
    ),
}


def get_location(city: str) -> CityLocation:
    """Get location data for a city. Falls back to Jakarta if not found."""
    return LOCATIONS.get(city.lower(), LOCATIONS["jakarta"])


def list_available_cities() -> list[str]:
    """Return list of available city names."""
    return list(LOCATIONS.keys())
