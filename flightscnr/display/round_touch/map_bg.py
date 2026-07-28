"""Cached map background for the radar screen.

Styles (settings map_style, fallback RADAR_MAP_PROVIDER):
  dark — CARTO Dark Matter, no labels (default)
  light — CARTO Positron light, no labels
  vfr  — FAA VFR sectional charts (US coverage, public domain)
  osm  — OpenStreetMap tiles remapped to dark radar palette (legacy env)
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pygame
import requests

from display.round_touch import scale, theme

logger = logging.getLogger("flightscnr.display")


def _as_display_surface(surface: pygame.Surface, *, alpha: bool = False) -> pygame.Surface:
    """Convert pixel format only when a display exists.

    Map builds run on a worker thread where pygame.display is often not
    initialized yet; bare .convert()/.convert_alpha() raises there.
    """
    try:
        if not pygame.display.get_init():
            return surface
        return surface.convert_alpha() if alpha else surface.convert()
    except pygame.error:
        return surface


DATA_DIR = os.environ.get("FLIGHTSCNR_DATA_DIR", "/var/lib/flightscnr")
CACHE_DIR = os.path.join(DATA_DIR, "maps", "radar_bg")
MANIFEST_PATH = os.path.join(CACHE_DIR, "manifest.json")

TILE_SIZE = 256
EARTH_RADIUS_M = 6378137.0

# UI-facing styles (Options / portal cycle). Legacy "osm" remains via env.
MAP_STYLES = ("dark", "light", "vfr")

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CARTO_SUBDOMAINS = "abcd"
CARTO_TILE_URL = "https://{sub}.basemaps.cartocdn.com/{style}/{z}/{x}/{y}.png"
# ArcGIS MapServer tiles use {z}/{y}/{x} (row/col), not OSM {z}/{x}/{y}.
VFR_TILE_URL = (
    "https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "VFR_Sectional/MapServer/tile/{z}/{y}/{x}"
)
VFR_ZOOM_MIN = 8
VFR_ZOOM_MAX = 12

USER_AGENT = "FlightScnrPi/1.0"
OSM_TILE_DELAY_S = 0.55  # OSM tile usage policy: max ~2 requests/second
CARTO_TILE_WORKERS = 4
OSM_TILE_WORKERS = 2
VFR_TILE_WORKERS = 4
CACHE_TTL_S = 7 * 24 * 3600
CACHE_STYLE_VERSION = 19  # v19 stores an unmasked, rectangular-display-ready map


_lock = threading.Lock()
_surfaces: dict[tuple, pygame.Surface] = {}
# Keys already promoted with convert_alpha(). pygame's convert_alpha() always
# allocates a new Surface, so calling it every get_background() churned ids and
# invalidated the radar backdrop cache (~every frame).
_display_converted: set[tuple] = set()
_fetch_threads: dict[tuple, threading.Thread] = {}
_display_backgrounds: dict[tuple, pygame.Surface] = {}


def normalize_map_style(raw: str | None) -> str:
    """Map UI / env aliases to a canonical style id."""
    provider = (raw or "dark").strip().lower() or "dark"
    if provider in ("dark", "carto", "cartodb", "carto_dark", "dark_matter"):
        return "dark"
    if provider in ("light", "carto_light", "positron"):
        return "light"
    if provider in ("vfr", "sectional", "faa", "faa_vfr"):
        return "vfr"
    if provider in ("osm", "openstreetmap"):
        return "osm"
    logger.warning("Unknown map style %r — using dark", raw)
    return "dark"


def _enabled() -> bool:
    raw = os.environ.get("RADAR_MAP_ENABLED", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _fill_display_enabled() -> bool:
    """Whether rectangular displays extend the basemap outside the radar dial."""
    raw = os.environ.get("RADAR_MAP_FILL_DISPLAY", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _outside_dim_alpha() -> int:
    """Black overlay alpha applied to the map outside the central radar dial."""
    raw = os.environ.get("RADAR_MAP_OUTSIDE_DIM_ALPHA", "55")
    try:
        return max(0, min(255, int(raw)))
    except (TypeError, ValueError):
        return 55


def _physical_display_size() -> tuple[int, int]:
    """Return the active SDL size, falling back to configured dimensions."""
    display = pygame.display.get_surface()
    if display is not None:
        return display.get_size()
    try:
        from config import DISPLAY_HEIGHT, DISPLAY_WIDTH

        return int(DISPLAY_WIDTH), int(DISPLAY_HEIGHT)
    except (ImportError, TypeError, ValueError):
        try:
            width = int(os.environ.get("DISPLAY_WIDTH", str(theme.SIZE)))
            height = int(os.environ.get("DISPLAY_HEIGHT", str(theme.SIZE)))
            return width, height
        except (TypeError, ValueError):
            return theme.SIZE, theme.SIZE


def _background_canvas_side(
    display_size: tuple[int, int] | None = None,
) -> int:
    """Square source-map side large enough for a rotated rectangular crop."""
    base_side = theme.VISIBLE_RADIUS * 2 + TILE_SIZE
    width, height = display_size or _physical_display_size()
    width = max(1, int(width))
    height = max(1, int(height))
    if (width, height) == (theme.SIZE, theme.SIZE):
        return base_side
    required = int(math.ceil(math.hypot(width, height))) + 8
    side = max(base_side, required)
    # Keep an even side so the home point lands exactly on an integer centre.
    return side + (side % 2)


def _env_map_style() -> str:
    return normalize_map_style(os.environ.get("RADAR_MAP_PROVIDER", "dark"))


def _resolved_style() -> str:
    """Settings map_style first; env RADAR_MAP_PROVIDER as fallback."""
    try:
        from display.round_touch import settings

        return normalize_map_style(settings.map_style())
    except Exception:
        return _env_map_style()


def _resolved_provider() -> str:
    """Backward-compatible alias used by older call sites / logs."""
    return _resolved_style()


def _tile_url(z: int, x: int, y: int, style: str | None = None) -> str:
    style = normalize_map_style(style) if style else _resolved_style()
    if style == "dark":
        sub = CARTO_SUBDOMAINS[(x + y) % len(CARTO_SUBDOMAINS)]
        return CARTO_TILE_URL.format(sub=sub, style="dark_nolabels", z=z, x=x, y=y)
    if style == "light":
        sub = CARTO_SUBDOMAINS[(x + y) % len(CARTO_SUBDOMAINS)]
        return CARTO_TILE_URL.format(sub=sub, style="light_nolabels", z=z, x=x, y=y)
    if style == "vfr":
        # FAA ArcGIS: level / row / col
        return VFR_TILE_URL.format(z=z, y=y, x=x)
    return OSM_TILE_URL.format(z=z, x=x, y=y)


def _tile_workers(style: str | None = None) -> int:
    style = normalize_map_style(style) if style else _resolved_style()
    if style == "osm":
        return OSM_TILE_WORKERS
    if style == "vfr":
        return VFR_TILE_WORKERS
    return CARTO_TILE_WORKERS


def _cache_key() -> tuple | None:
    return _cache_key_for_scale(scale.active_index())


def _cache_key_for_scale(scale_index: int) -> tuple | None:
    try:
        from config import LOCATION_HOME, location_configured
    except ImportError:
        return None
    if not location_configured():
        return None
    return (
        round(LOCATION_HOME[0], 5),
        round(LOCATION_HOME[1], 5),
        scale_index,
        _resolved_style(),
        _background_canvas_side(),
    )


def _cache_path_for_key(key: tuple) -> str:
    lat, lon, scale_idx, style, canvas_side = key[:5]
    return os.path.join(
        CACHE_DIR,
        f"bg_{style}_{lat}_{lon}_{scale_idx}_{canvas_side}px.png",
    )


def _manifest_path_for_key(key: tuple) -> str:
    return _cache_path_for_key(key).replace(".png", ".meta.json")


def _meters_per_pixel(lat_deg: float, zoom: int) -> float:
    return math.cos(math.radians(lat_deg)) * 2 * math.pi * EARTH_RADIUS_M / (
        TILE_SIZE * (2 ** zoom)
    )


def _zoom_for_scale(home_lat: float, px_per_km: float, style: str | None = None) -> int:
    """Pick the zoom level whose ground resolution best matches the radar scale."""
    target_km_per_px = 1.0 / px_per_km
    style = normalize_map_style(style) if style else _resolved_style()
    if style == "vfr":
        z_min, z_max = VFR_ZOOM_MIN, VFR_ZOOM_MAX
    else:
        z_min, z_max = 9, 17
    best_z = min(max(11, z_min), z_max)
    best_err = float("inf")
    for z in range(z_min, z_max + 1):
        km_per_px = _meters_per_pixel(home_lat, z) / 1000.0
        err = abs(km_per_px - target_km_per_px)
        if err < best_err:
            best_err = err
            best_z = z
    return best_z


def _basemap_render_scale(
    home_lat: float,
    scale_index: int,
    zoom: int,
    style: str | None = None,
) -> float:
    """Resize factor so tile imagery matches the selected radar range.

    FAA VFR sectionals cap at zoom 12, so at tight ranges the raw tiles are far
    coarser than the chosen band (e.g. ~30 m/px vs ~9 m/px at 2 mi). Scaling the
    chart — and the matching aircraft/overlay placement — by this factor keeps a
    "2 mi" selection meaning 2 mi on both the rings and the chart.

    Only VFR is scaled; dark/light have enough zoom levels to match closely and
    should stay pixel-crisp.
    """
    style = normalize_map_style(style) if style else _resolved_style()
    if style != "vfr":
        return 1.0
    if scale_index < 0 or scale_index >= len(scale.SCALE_BANDS):
        return 1.0
    outer_km = scale.SCALE_BANDS[scale_index]["label_km"]
    target_m_per_px = outer_km * 1000.0 / theme.GRID_OUTER_RADIUS
    if target_m_per_px <= 0:
        return 1.0
    tile_m_per_px = _meters_per_pixel(home_lat, zoom)
    factor = tile_m_per_px / target_m_per_px
    # Guard against pathological values; keep within a sane resize range.
    if not (0.05 < factor < 20.0):
        return 1.0
    return factor


def _lon_to_tile_x(lon: float, zoom: int) -> int:
    return int((lon + 180.0) / 360.0 * (2 ** zoom))


def _lat_to_tile_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    return int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)


def _mercator_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """World pixel coordinates for a lat/lon at the given zoom (tile-aligned)."""
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def _tile_nw_lat_lon(z: int, x: int, y: int) -> tuple[float, float]:
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _fetch_tile(
    z: int,
    x: int,
    y: int,
    session: requests.Session,
    style: str,
) -> pygame.Surface | None:
    url = _tile_url(z, x, y, style)
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return pygame.image.load(io.BytesIO(resp.content))
        except (OSError, requests.RequestException, pygame.error) as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("Map tile fetch failed %s: %s", url, exc)
    return None


def _fetch_tile_coords(
    zoom: int,
    coords: list[tuple[int, int]],
    style: str,
) -> dict[tuple[int, int], pygame.Surface]:
    """Download tiles in parallel (CARTO/FAA tolerate concurrent requests)."""
    if not coords:
        return {}

    style = normalize_map_style(style)
    workers = min(_tile_workers(style), len(coords))
    results: dict[tuple[int, int], pygame.Surface] = {}

    def _download(tx: int, ty: int) -> tuple[int, int, pygame.Surface | None]:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        if style == "osm":
            time.sleep(OSM_TILE_DELAY_S)
        return tx, ty, _fetch_tile(zoom, tx, ty, session, style)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download, tx, ty) for tx, ty in coords]
        for future in as_completed(futures):
            tx, ty, tile = future.result()
            if tile is not None:
                results[(tx, ty)] = tile
    return results


def _luminance_curve(value: int) -> int:
    """Map OSM tile brightness to a dark radar palette."""
    if value >= 215:
        return min(175, 90 + (value - 215))
    if value >= 165:
        return 50 + (value - 165) // 2
    if value >= 110:
        return 28 + (value - 110) // 5
    return max(16, 12 + value // 8)


def _style_carto(surface: pygame.Surface) -> pygame.Surface:
    """Brighten CARTO dark_nolabels so roads and coastline read under the radar grid."""
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        Image = None

    if Image is not None:
        tobytes = getattr(pygame.image, "tobytes", pygame.image.tostring)
        img = Image.frombytes("RGB", surface.get_size(), tobytes(surface, "RGB"))
        # Lift shadows — CARTO dark tiles are very low-luminance out of the box.
        lum = img.convert("L").point(lambda v: min(255, int(v * 1.35 + 28)))
        img = Image.merge(
            "RGB",
            (
                lum,
                lum.point(lambda v: min(255, v + 10)),
                lum.point(lambda v: min(255, v + 6)),
            ),
        )
        img = ImageEnhance.Brightness(img).enhance(1.12)
        img = ImageEnhance.Contrast(img).enhance(1.22)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return _as_display_surface(pygame.image.load(buf))

    return _as_display_surface(surface)


def _style_light(surface: pygame.Surface) -> pygame.Surface:
    """Mild contrast on CARTO light tiles — keep readable under radar chrome."""
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        Image = None

    if Image is not None:
        tobytes = getattr(pygame.image, "tobytes", pygame.image.tostring)
        img = Image.frombytes("RGB", surface.get_size(), tobytes(surface, "RGB"))
        img = ImageEnhance.Contrast(img).enhance(1.08)
        img = ImageEnhance.Brightness(img).enhance(0.92)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return _as_display_surface(pygame.image.load(buf))

    return _as_display_surface(surface)


def _style_vfr(surface: pygame.Surface) -> pygame.Surface:
    """Light readability pass on FAA sectionals (opacity is applied at draw time)."""
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        Image = None

    if Image is not None:
        tobytes = getattr(pygame.image, "tobytes", pygame.image.tostring)
        img = Image.frombytes("RGB", surface.get_size(), tobytes(surface, "RGB"))
        # Keep chart mostly intact — dial pale/strong with vfr_map_opacity at blit.
        img = ImageEnhance.Color(img).enhance(0.92)
        img = ImageEnhance.Contrast(img).enhance(0.95)
        img = ImageEnhance.Brightness(img).enhance(1.02)
        wash = Image.new("RGB", img.size, (242, 244, 238))
        img = Image.blend(img, wash, alpha=0.08)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return _as_display_surface(pygame.image.load(buf))

    return _as_display_surface(surface)


_vfr_opacity_blit_cache: tuple | None = None  # (id(bg), pct, surface)


def _vfr_with_draw_opacity(bg: pygame.Surface) -> pygame.Surface:
    """Fade VFR chart toward parchment by settings opacity (preserves alpha).

    Applied at draw time so changing the slider never clears/rebuilds the tile cache
    (which previously left the dark radar BG showing — looked like a black map).
    """
    global _vfr_opacity_blit_cache
    try:
        from display.round_touch import settings

        pct = int(settings.vfr_map_opacity())
    except Exception:
        pct = 45
    pct = max(0, min(100, pct))
    if pct >= 100:
        return bg

    cached = _vfr_opacity_blit_cache
    if cached is not None and cached[0] == id(bg) and cached[1] == pct:
        return cached[2]

    t = pct / 100.0
    inv = 1.0 - t
    out = bg.copy()
    # Blend RGB toward parchment; leave alpha untouched.
    rgb = pygame.surfarray.pixels3d(out)
    rgb[:, :, 0] = (rgb[:, :, 0].astype("float32") * t + 242.0 * inv).astype("uint8")
    rgb[:, :, 1] = (rgb[:, :, 1].astype("float32") * t + 244.0 * inv).astype("uint8")
    rgb[:, :, 2] = (rgb[:, :, 2].astype("float32") * t + 238.0 * inv).astype("uint8")
    del rgb
    _vfr_opacity_blit_cache = (id(bg), pct, out)
    return out


def _style_osm(surface: pygame.Surface) -> pygame.Surface:
    """Render standard OSM tiles as dark mode — dark land/water, visible roads."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        Image = None

    if Image is not None:
        tobytes = getattr(pygame.image, "tobytes", pygame.image.tostring)
        raw = tobytes(surface, "RGB")
        src = Image.frombytes("RGB", surface.get_size(), raw)
        lum = src.convert("L").point(_luminance_curve)

        r = lum
        g = lum.point(lambda v: min(255, v + 18))
        b = lum.point(lambda v: min(255, v + 8))
        styled = Image.merge("RGB", (r, g, b))

        # OSM water is light blue — tint those pixels dark blue-grey.
        red, green, blue = src.split()
        water_bias = ImageChops.subtract(blue, red)
        water_mask = water_bias.point(lambda d: 255 if d > 22 else 0)
        water = Image.new("RGB", src.size, (22, 38, 58))
        styled = Image.composite(water, styled, water_mask)

        # Light radar-green wash — keeps dark mode without crushing detail.
        wash = Image.new("RGB", src.size, theme.BG)
        styled = Image.blend(styled, wash, alpha=0.07)

        buf = io.BytesIO()
        styled.save(buf, format="PNG")
        buf.seek(0)
        return _as_display_surface(pygame.image.load(buf))

    tinted = _as_display_surface(surface.copy())
    shade = pygame.Surface(tinted.get_size())
    shade.fill((40, 48, 38))
    tinted.blit(shade, (0, 0), special_flags=pygame.BLEND_MULT)
    return tinted


def _style_for_radar(surface: pygame.Surface, style: str | None = None) -> pygame.Surface:
    style = normalize_map_style(style) if style else _resolved_style()
    if style == "dark":
        return _style_carto(surface)
    if style == "light":
        return _style_light(surface)
    if style == "vfr":
        return _style_vfr(surface)
    return _style_osm(surface)


def _build_background(
    scale_index: int,
    style: str | None = None,
    canvas_side: int | None = None,
) -> pygame.Surface | None:
    try:
        from config import LOCATION_HOME, location_configured
    except ImportError:
        return None
    if not location_configured():
        return None
    if scale_index < 0 or scale_index >= len(scale.SCALE_BANDS):
        return None

    # Pin style for the whole build — never re-read settings mid-fetch.
    # Otherwise switching Map while prewarm runs can save light tiles under a dark key.
    provider = normalize_map_style(style) if style else _resolved_style()
    home_lat, home_lon = LOCATION_HOME[0], LOCATION_HOME[1]
    outer_km = scale.SCALE_BANDS[scale_index]["label_km"]
    px_per_km = theme.GRID_OUTER_RADIUS / outer_km
    zoom = _zoom_for_scale(home_lat, px_per_km, provider)
    render_scale = _basemap_render_scale(home_lat, scale_index, zoom, provider)

    diameter = max(
        theme.VISIBLE_RADIUS * 2 + TILE_SIZE,
        int(canvas_side or _background_canvas_side()),
    )
    if diameter % 2:
        diameter += 1
    # Fetch enough imagery to cover the full source square. This lets the same
    # north-up map feed both the circular radar and a rotated 480×320 backdrop.
    span_km = (diameter / 2.0) / px_per_km
    lat_delta = span_km / 110.574
    cos_lat = max(0.01, math.cos(math.radians(home_lat)))
    lon_delta = span_km / (111.320 * cos_lat)

    x_min = _lon_to_tile_x(home_lon - lon_delta, zoom) - 1
    x_max = _lon_to_tile_x(home_lon + lon_delta, zoom) + 1
    y_min = _lat_to_tile_y(home_lat + lat_delta, zoom) - 1
    y_max = _lat_to_tile_y(home_lat - lat_delta, zoom) + 1

    center = diameter // 2
    home_px, home_py = _mercator_pixel(home_lat, home_lon, zoom)

    coords = [
        (tx, ty)
        for ty in range(y_min, y_max + 1)
        for tx in range(x_min, x_max + 1)
    ]
    tiles = _fetch_tile_coords(zoom, coords, provider)

    scaled = abs(render_scale - 1.0) > 1e-3
    # +1px overlap so scaled tile seams do not leave gaps after rounding.
    scaled_side = max(1, int(math.ceil(TILE_SIZE * render_scale)) + 1) if scaled else TILE_SIZE

    canvas = pygame.Surface((diameter, diameter))
    canvas.fill(theme.BG)
    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            tile = tiles.get((tx, ty))
            if tile is None:
                continue
            nw_lat, nw_lon = _tile_nw_lat_lon(zoom, tx, ty)
            tile_px, tile_py = _mercator_pixel(nw_lat, nw_lon, zoom)
            px = center + int(round((tile_px - home_px) * render_scale))
            py = center + int(round((tile_py - home_py) * render_scale))
            if scaled:
                tile = pygame.transform.smoothscale(tile, (scaled_side, scaled_side))
            canvas.blit(tile, (px, py))

    logger.info(
        "Built radar map background (%s, scale %d, %d tiles, zoom %d, "
        "~%.1f km half-span, %dpx canvas, render_scale %.2f)",
        provider,
        scale_index,
        len(coords),
        zoom,
        span_km,
        diameter,
        render_scale,
    )
    # Keep the cached source unmasked. The round bezel is applied by the radar
    # layer, while rectangular displays can crop this same map to their full size.
    return _style_for_radar(canvas, provider)


def _save_cache(surface: pygame.Surface, key: tuple):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path_for_key(key)
    manifest_path = _manifest_path_for_key(key)
    pygame.image.save(surface, path)
    manifest = {
        "home_lat": key[0],
        "home_lon": key[1],
        "scale_index": key[2],
        "provider": key[3],
        "map_style": key[3],
        "fetched_at": int(time.time()),
        "style_version": CACHE_STYLE_VERSION,
        "canvas_side": key[4],
        "path": os.path.basename(path),
    }
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, manifest_path)


def _load_cache(key: tuple) -> pygame.Surface | None:
    path = _cache_path_for_key(key)
    manifest_path = _manifest_path_for_key(key)
    if not os.path.isfile(path):
        return None
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
            if manifest.get("home_lat") != key[0]:
                return None
            if manifest.get("home_lon") != key[1]:
                return None
            if manifest.get("scale_index") != key[2]:
                return None
            if manifest.get("provider") != key[3]:
                return None
            if manifest.get("canvas_side") != key[4]:
                return None
            if manifest.get("style_version") != CACHE_STYLE_VERSION:
                return None
            fetched_at = int(manifest.get("fetched_at", 0))
            if time.time() - fetched_at > CACHE_TTL_S:
                return None
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Could not read cached radar map manifest: %s", exc)
            return None
    elif os.path.isfile(MANIFEST_PATH):
        # Legacy single-manifest cache from older builds.
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as fh:
                manifest = json.load(fh)
            if manifest.get("path") != os.path.basename(path):
                return None
            if manifest.get("style_version") != CACHE_STYLE_VERSION:
                return None
            fetched_at = int(manifest.get("fetched_at", 0))
            if time.time() - fetched_at > CACHE_TTL_S:
                return None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
    else:
        return None
    try:
        return _as_display_surface(pygame.image.load(path), alpha=True)
    except (OSError, pygame.error) as exc:
        logger.warning("Could not load cached radar map: %s", exc)
        return None


def _remember_surface(key: tuple, surface: pygame.Surface):
    with _lock:
        _surfaces[key] = surface
        _display_converted.discard(key)
        _display_backgrounds.clear()


def _fetch_running(key: tuple) -> bool:
    with _lock:
        thread = _fetch_threads.get(key)
        return thread is not None and thread.is_alive()


def _start_fetch(key: tuple):
    with _lock:
        thread = _fetch_threads.get(key)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=_fetch_worker,
            args=(key,),
            name=f"radar-map-fetch-{key[2]}",
            daemon=True,
        )
        _fetch_threads[key] = thread
        thread.start()


def _fetch_worker(key: tuple):
    try:
        # Pin style and canvas size from the key; never re-read live settings.
        surface = _build_background(key[2], style=key[3], canvas_side=key[4])
        if surface is None:
            return
        _save_cache(surface, key)
        _remember_surface(key, surface)
    except Exception:
        logger.exception("Radar map background fetch failed for scale %s", key[2])
    finally:
        with _lock:
            _fetch_threads.pop(key, None)


def request_background(force: bool = False):
    """Load or start fetching the radar map background for the active scale."""
    if not _enabled():
        return

    key = _cache_key()
    if key is None:
        return

    request_background_for_key(key, force=force)


def request_background_for_key(key: tuple, force: bool = False):
    """Load or start fetching a cached map for a specific scale key."""
    if not _enabled():
        return

    with _lock:
        if not force and key in _surfaces:
            return
    if _fetch_running(key):
        return

    if not force:
        cached = _load_cache(key)
        if cached is not None:
            _remember_surface(key, cached)
            return

    _start_fetch(key)


def prewarm_all_scales():
    """Load every scale from disk, then fetch any missing maps one at a time."""
    if not _enabled():
        return

    def _worker():
        for scale_index in range(len(scale.SCALE_BANDS)):
            key = _cache_key_for_scale(scale_index)
            if key is None:
                return
            request_background_for_key(key)
            for _ in range(300):
                with _lock:
                    if key in _surfaces:
                        break
                if not _fetch_running(key):
                    with _lock:
                        if key in _surfaces:
                            break
                    break
                time.sleep(0.2)

    threading.Thread(
        target=_worker,
        name="radar-map-prewarm",
        daemon=True,
    ).start()


def clear_vfr_opacity_blit_cache():
    """Drop draw-time VFR opacity and derived full-display surfaces."""
    global _vfr_opacity_blit_cache
    _vfr_opacity_blit_cache = None
    with _lock:
        _display_backgrounds.clear()


def invalidate():
    """Drop in-memory backgrounds so the next request rebuilds or reloads."""
    with _lock:
        _surfaces.clear()
        _fetch_threads.clear()
        _display_converted.clear()
        _display_backgrounds.clear()
    clear_vfr_opacity_blit_cache()


def cache_token() -> object:
    """Stable token for radar backdrop invalidation (changes with map content)."""
    if not _enabled():
        return None
    key = _cache_key()
    if key is None:
        return None
    with _lock:
        return (key, id(_surfaces.get(key)) if key in _surfaces else 0)


def get_background() -> pygame.Surface | None:
    if not _enabled():
        return None
    key = _cache_key()
    if key is None:
        return None
    with _lock:
        surface = _surfaces.get(key)
        if surface is None:
            return None
        # Promote to display format once on the main thread. Re-converting every
        # call allocates a new Surface and breaks callers that key on id().
        if key not in _display_converted:
            converted = _as_display_surface(surface, alpha=True)
            if converted is not surface:
                _surfaces[key] = converted
                surface = converted
            _display_converted.add(key)
        return surface


def get_display_background(
    display_size: tuple[int, int],
    *,
    display_rotation: int = 0,
) -> pygame.Surface | None:
    """Return a same-scale basemap filling a rectangular physical display.

    The cached map remains north-up. We rotate it by the radar facing minus the
    final UI rotation, crop it around the home point, and darken it slightly so
    the circular radar overlay stays visually dominant. Square displays retain
    the original round-only presentation.
    """
    if not _enabled() or not _fill_display_enabled():
        return None
    width, height = int(display_size[0]), int(display_size[1])
    if width <= 0 or height <= 0:
        return None
    if (width, height) == (theme.SIZE, theme.SIZE):
        return None

    bg = get_background()
    if bg is None:
        return None
    if _resolved_style() == "vfr":
        bg = _vfr_with_draw_opacity(bg)

    try:
        from display.round_touch import settings

        facing = float(settings.effective_facing_deg() or 0.0)
    except Exception:
        facing = 0.0
    angle = (facing - float(display_rotation or 0)) % 360.0
    if angle > 180.0:
        angle -= 360.0
    dim_alpha = _outside_dim_alpha()
    key = (
        cache_token(),
        id(bg),
        (width, height),
        round(angle, 2),
        dim_alpha,
        theme.BG,
    )
    with _lock:
        cached = _display_backgrounds.get(key)
    if cached is not None:
        return cached

    source = bg if abs(angle) < 0.05 else pygame.transform.rotate(bg, angle)
    result = pygame.Surface((width, height))
    result.fill(theme.BG)
    result.blit(source, source.get_rect(center=(width // 2, height // 2)))
    if dim_alpha:
        shade = pygame.Surface((width, height), pygame.SRCALPHA)
        shade.fill((0, 0, 0, dim_alpha))
        result.blit(shade, (0, 0))
    result = _as_display_surface(result)

    with _lock:
        # Facing changes during calibration; cap stale derived surfaces.
        if len(_display_backgrounds) >= 12:
            _display_backgrounds.clear()
        _display_backgrounds[key] = result
    return result


def draw_background(surface: pygame.Surface, pan_offset: tuple[int, int] | None = None):
    bg = get_background()
    if bg is None:
        return
    if _resolved_style() == "vfr":
        bg = _vfr_with_draw_opacity(bg)
    facing = 0.0
    try:
        from display.round_touch import settings

        facing = float(settings.effective_facing_deg() or 0.0)
    except Exception:
        facing = 0.0
    ox = int(pan_offset[0]) if pan_offset else 0
    oy = int(pan_offset[1]) if pan_offset else 0
    if abs(facing) < 0.05:
        rect = bg.get_rect(center=(theme.CENTER_X + ox, theme.CENTER_Y + oy))
        surface.blit(bg, rect)
        return
    # pygame rotates CCW; facing east-up needs the north-up map rotated CCW
    # so east moves to the top (same sense as geo.rotate_offset / the rose).
    rotated = pygame.transform.rotate(bg, facing)
    rect = rotated.get_rect(center=(theme.CENTER_X + ox, theme.CENTER_Y + oy))
    surface.blit(rotated, rect)


def _basemap_zoom_for_home(home_lat: float) -> int | None:
    idx = scale.active_index()
    if idx < 0 or idx >= len(scale.SCALE_BANDS):
        return None
    outer_km = scale.SCALE_BANDS[idx]["label_km"]
    px_per_km = theme.GRID_OUTER_RADIUS / outer_km
    return _zoom_for_scale(home_lat, px_per_km, _resolved_style())


def lat_lon_to_basemap_screen(
    lat: float,
    lon: float,
    *,
    center_lat: float | None = None,
    center_lon: float | None = None,
) -> tuple[int, int] | None:
    """Map WGS84 → radar pixels using the same Mercator math as the basemap.

    Prefer this (via ``geo.lat_lon_to_screen``) whenever the tile basemap is on
    so aircraft and overlays sit on roads/coastline instead of drifting from the
    flat-earth radar projection away from the radar center.
    """
    try:
        from config import LOCATION_HOME, location_configured
        from display.round_touch import settings
    except ImportError:
        return None
    if not location_configured() and center_lat is None:
        return None
    try:
        home_lat = float(LOCATION_HOME[0] if center_lat is None else center_lat)
        home_lon = float(LOCATION_HOME[1] if center_lon is None else center_lon)
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    zoom = _basemap_zoom_for_home(home_lat)
    if zoom is None:
        return None
    render_scale = _basemap_render_scale(home_lat, scale.active_index(), zoom)
    home_px, home_py = _mercator_pixel(home_lat, home_lon, zoom)
    mx, my = _mercator_pixel(lat_f, lon_f, zoom)
    # Image coords: +x east, +y south (Mercator tile space), matching _build_background.
    vx = (mx - home_px) * render_scale
    vy = (my - home_py) * render_scale
    try:
        facing = float(settings.effective_facing_deg() or 0.0)
    except Exception:
        facing = 0.0
    if abs(facing) >= 0.05:
        rad = math.radians(facing)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        # Same CCW rotation pygame applies to the basemap surface.
        vx, vy = vx * cos_a - vy * sin_a, vx * sin_a + vy * cos_a
    return (
        theme.CENTER_X + int(round(vx)),
        theme.CENTER_Y + int(round(vy)),
    )


def basemap_screen_to_lat_lon(
    x: float,
    y: float,
    *,
    center_lat: float | None = None,
    center_lon: float | None = None,
) -> tuple[float, float] | None:
    """Inverse of ``lat_lon_to_basemap_screen`` (facing-aware Mercator)."""
    try:
        from config import LOCATION_HOME, location_configured
        from display.round_touch import settings
    except ImportError:
        return None
    if not location_configured() and center_lat is None:
        return None
    try:
        home_lat = float(LOCATION_HOME[0] if center_lat is None else center_lat)
        home_lon = float(LOCATION_HOME[1] if center_lon is None else center_lon)
    except (TypeError, ValueError):
        return None
    zoom = _basemap_zoom_for_home(home_lat)
    if zoom is None:
        return None
    render_scale = _basemap_render_scale(home_lat, scale.active_index(), zoom)
    vx = float(x) - theme.CENTER_X
    vy = float(y) - theme.CENTER_Y
    try:
        facing = float(settings.effective_facing_deg() or 0.0)
    except Exception:
        facing = 0.0
    if abs(facing) >= 0.05:
        rad = math.radians(facing)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        # Inverse of the CCW facing rotation applied in lat_lon_to_basemap_screen.
        vx, vy = vx * cos_a + vy * sin_a, -vx * sin_a + vy * cos_a
    if render_scale:
        vx /= render_scale
        vy /= render_scale
    home_px, home_py = _mercator_pixel(home_lat, home_lon, zoom)
    return _mercator_pixel_to_lat_lon(home_px + vx, home_py + vy, zoom)


def _mercator_pixel_to_lat_lon(px: float, py: float, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    lon = px / (n * TILE_SIZE) * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * py / (n * TILE_SIZE)))))
    return lat, lon


def attribution_text() -> str | None:
    if not _enabled() or get_background() is None:
        return None
    style = _resolved_style()
    if style == "vfr":
        return "© FAA"
    if style == "osm":
        return "© OpenStreetMap"
    return "© OSM © CARTO"
