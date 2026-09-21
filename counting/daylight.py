"""Daylight window for the camera location.

Port of the ``daylightWindow`` sunrise/sunset math in ``ui/index.html`` so the
pipeline filters night clips exactly the way the UI does. Clip timestamps are
UTC (as encoded in the filename), and the returned bounds are UTC epoch seconds.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Camera location (Muskoka), matching the UI constants.
LATITUDE = 45.0
LONGITUDE = -79.5


def _solar_time(day: str, hour: int, sunrise: bool) -> float | None:
    year, month, date = (int(part) for part in day.split("-"))
    day_of_year = (
        datetime(year, month, date, tzinfo=timezone.utc)
        - datetime(year, 1, 1, tzinfo=timezone.utc)
    ).days + 1

    longitude_hour = LONGITUDE / 15
    t = day_of_year + (hour - longitude_hour) / 24
    mean_anomaly = 0.9856 * t - 3.289
    longitude = (
        mean_anomaly
        + 1.916 * math.sin(math.radians(mean_anomaly))
        + 0.02 * math.sin(math.radians(2 * mean_anomaly))
        + 282.634
    ) % 360
    right_ascension = math.degrees(
        math.atan(0.91764 * math.tan(math.radians(longitude)))
    ) % 360
    right_ascension += (
        math.floor(longitude / 90) - math.floor(right_ascension / 90)
    ) * 90
    right_ascension /= 15

    sin_declination = 0.39782 * math.sin(math.radians(longitude))
    cos_declination = math.cos(math.asin(sin_declination))
    cos_hour_angle = (
        math.cos(math.radians(90.833))
        - sin_declination * math.sin(math.radians(LATITUDE))
    ) / (cos_declination * math.cos(math.radians(LATITUDE)))
    if cos_hour_angle > 1 or cos_hour_angle < -1:
        return None

    hour_angle = math.degrees(math.acos(cos_hour_angle))
    if sunrise:
        hour_angle = 360 - hour_angle
    hour_angle /= 15

    local_mean_time = hour_angle + right_ascension - 0.06571 * t - 6.622
    utc_hour = local_mean_time - longitude_hour
    if sunrise and utc_hour < 0:
        utc_hour += 24
    if not sunrise and utc_hour < 12:
        utc_hour += 24

    midnight = datetime(year, month, date, tzinfo=timezone.utc).timestamp()
    return midnight + utc_hour * 3600


def daylight_window(day: str) -> tuple[float | None, float | None]:
    """Return ``(sunrise, sunset)`` epoch seconds (UTC) for a ``YYYY-MM-DD`` day."""
    return _solar_time(day, 6, True), _solar_time(day, 18, False)


def is_daytime(start_epoch: float, day: str) -> bool:
    """True if a clip's start time falls between sunrise and sunset for ``day``."""
    sunrise, sunset = daylight_window(day)
    if sunrise is None or sunset is None:
        return False
    return sunrise <= start_epoch <= sunset
