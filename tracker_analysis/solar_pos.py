"""Solar position calculations — pure Python, no external deps.

Implements a simplified NOAA Solar Position Algorithm (NREL SPA simplified)
that gives sun azimuth, elevation, and a basic clear-sky GHI to within
~0.1° / 5% W/m² — fine for dashboard purposes.

Also computes the tilt angle of a north-south horizontal-axis single-axis
tracker with backtracking, which is what the Arctech 1P and 2P trackers
implement.

Usage:
    from solar_pos import sun_position, tracker_tilt_ns, clear_sky_ghi
    elev, azim = sun_position(now_utc, lat_deg, lon_deg)
    tilt = tracker_tilt_ns(elev, azim, max_tilt_deg=55)
    ghi  = clear_sky_ghi(elev)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Sun position
# ---------------------------------------------------------------------------
def sun_position(dt: datetime, lat_deg: float, lon_deg: float):
    """Return (elevation_deg, azimuth_deg) for the given UTC datetime + site.

    azimuth is 0° = north, 90° = east, 180° = south, 270° = west.
    elevation is 90° at zenith, 0° at horizon, negative below.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)

    # Julian day
    y, m = dt_utc.year, dt_utc.month
    d = (dt_utc.day
         + dt_utc.hour / 24.0
         + dt_utc.minute / 1440.0
         + dt_utc.second / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5

    # Julian century since J2000.0
    t = (jd - 2451545.0) / 36525.0

    # Geometric mean longitude (deg)
    L0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    # Geometric mean anomaly (deg)
    M = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    # Eccentricity of Earth's orbit
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    # Sun's equation of center
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * Mr) * 0.000289)
    # True longitude
    true_long = L0 + C
    # Apparent longitude (corrected for nutation/aberration)
    omega = 125.04 - 1934.136 * t
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    # Mean obliquity of the ecliptic
    eps0 = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))) / 60.0) / 60.0)
    # Corrected obliquity
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))

    # Right ascension, declination
    lam_r = math.radians(lam)
    eps_r = math.radians(eps)
    decl = math.degrees(math.asin(math.sin(eps_r) * math.sin(lam_r)))

    # Greenwich Mean Sidereal Time → Local Hour Angle
    # Equation of time (minutes)
    y_ = math.tan(eps_r / 2.0) ** 2
    L0r = math.radians(L0)
    eqtime = 4 * math.degrees(
        y_ * math.sin(2 * L0r)
        - 2 * e * math.sin(Mr)
        + 4 * e * y_ * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * y_ * y_ * math.sin(4 * L0r)
        - 1.25 * e * e * math.sin(2 * Mr)
    )

    # True solar time (minutes since midnight)
    tst = (dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60.0
           + eqtime + 4 * lon_deg) % 1440.0
    # Hour angle (degrees) — 0 at solar noon, +east of meridian negative
    ha = tst / 4.0 - 180.0

    # Solar zenith / elevation / azimuth
    lat_r = math.radians(lat_deg)
    decl_r = math.radians(decl)
    ha_r = math.radians(ha)
    cos_zen = (math.sin(lat_r) * math.sin(decl_r)
               + math.cos(lat_r) * math.cos(decl_r) * math.cos(ha_r))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zen = math.degrees(math.acos(cos_zen))
    elev = 90.0 - zen

    # Azimuth — 0 = north, increasing clockwise
    sin_az = -math.cos(decl_r) * math.sin(ha_r) / max(math.sin(math.radians(zen)), 1e-9)
    cos_az = ((math.sin(decl_r) - math.sin(lat_r) * cos_zen)
              / max(math.cos(lat_r) * math.sin(math.radians(zen)), 1e-9))
    az = math.degrees(math.atan2(sin_az, cos_az)) % 360.0

    return elev, az


# ---------------------------------------------------------------------------
# Tracker tilt — NS horizontal axis with backtracking
# ---------------------------------------------------------------------------
def tracker_tilt_ns(elev_deg: float, azim_deg: float,
                    max_tilt_deg: float = 55.0,
                    gcr: float = 0.4) -> float:
    """Tilt angle (deg) of a north-south single-axis tracker.

    Positive = tilted toward the west (afternoon).
    Negative = tilted toward the east (morning).

    elev_deg : sun elevation above horizon.
    azim_deg : sun azimuth (0 = N, 90 = E, 180 = S, 270 = W).
    max_tilt_deg : tracker mechanical limit.
    gcr : ground cover ratio (row width / row pitch). Used for backtracking
          to prevent inter-row shading near sunrise/sunset.

    Algorithm:
      • Project sun vector onto the east-west plane to get the
        "astronomical tracking angle" β_a = atan(sin(az_from_south) / tan(elev)).
      • If shading would occur (cos(β_a) < gcr), apply backtracking
        per the standard NREL formula:
            β = atan(cos(β_a − β_back) / gcr)  — adjusted to avoid shading.
      • Clamp to ±max_tilt_deg.
    """
    if elev_deg <= 0:
        return 0.0

    # Azimuth measured from south, positive west (so afternoon = +)
    az_from_south = azim_deg - 180.0
    elev_r = math.radians(elev_deg)
    az_r = math.radians(az_from_south)

    # Astronomical tracking angle
    if abs(math.tan(elev_r)) < 1e-9:
        beta_a = math.copysign(90.0, az_from_south)
    else:
        beta_a = math.degrees(math.atan2(math.sin(az_r), math.tan(elev_r)))

    # Backtracking: if the projected row-on-row shadow would touch the
    # next row, reduce the tilt magnitude.
    cos_beta_a = math.cos(math.radians(beta_a))
    if cos_beta_a < gcr and cos_beta_a > 0:
        # Shading would occur — backtrack
        try:
            beta = math.degrees(
                math.copysign(
                    math.acos(min(1.0, cos_beta_a / gcr)),
                    beta_a,
                )
            )
            # Reduce tilt by the backtrack amount
            beta = beta_a - beta if beta_a > 0 else beta_a + beta
        except ValueError:
            beta = 0.0
    else:
        beta = beta_a

    # Clamp to mechanical limits
    return max(-max_tilt_deg, min(max_tilt_deg, beta))


# ---------------------------------------------------------------------------
# Clear-sky model — simple Haurwitz formula
# ---------------------------------------------------------------------------
def clear_sky_ghi(elev_deg: float) -> float:
    """Rough clear-sky GHI estimate (W/m²) from sun elevation.

    Uses the Haurwitz 1945 model — a one-line formula that's surprisingly
    decent for clearness-index work. Returns 0 when the sun is below
    the horizon.
    """
    if elev_deg <= 0:
        return 0.0
    cos_zen = math.cos(math.radians(90.0 - elev_deg))
    if cos_zen <= 0:
        return 0.0
    return 1098.0 * cos_zen * math.exp(-0.059 / max(cos_zen, 0.05))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Quick sanity check: midday at Moora Moora
    test_dt = datetime(2024, 12, 21, 2, 0, tzinfo=timezone.utc)  # ~13:00 AEDT
    lat, lon = -37.4, 144.9
    elev, az = sun_position(test_dt, lat, lon)
    print(f"  Sun position at {test_dt} (UTC) at ({lat}, {lon}):")
    print(f"    elevation = {elev:.2f}°")
    print(f"    azimuth   = {az:.2f}° (0=N, 180=S)")
    tilt = tracker_tilt_ns(elev, az)
    print(f"    tracker tilt (NS-axis) = {tilt:.2f}°")
    ghi = clear_sky_ghi(elev)
    print(f"    clear-sky GHI = {ghi:.0f} W/m²")
