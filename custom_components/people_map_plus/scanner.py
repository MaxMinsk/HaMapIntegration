"""Background media scanner for People Map Plus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from PIL import ExifTags, Image

from .const import (
    CONF_INDEX_INTERVAL_MINUTES,
    CONF_MAX_SCAN_FILES_PER_RUN,
    CONF_PHOTO_ROOTS,
    DEFAULT_INDEX_INTERVAL_MINUTES,
    DEFAULT_MAX_SCAN_FILES_PER_RUN,
    DEFAULT_PHOTO_ROOTS,
    MEDIA_ROOT,
    SUPPORTED_EXTENSIONS,
)
from .storage import PhotoIndexRecord, PhotoIndexRepository

_LOGGER = logging.getLogger(__name__)

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(slots=True)
class ScanResult:
    """Scan execution metrics."""

    scanned: int
    updated: int
    unchanged: int
    deleted: int
    errors: int


class PhotoIndexScanner:
    """Periodic scanner that indexes media photo metadata."""

    def __init__(
        self,
        hass: HomeAssistant,
        repository: PhotoIndexRepository,
        entry_id: str,
        options: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._repository = repository
        self._entry_id = entry_id
        self._options = options
        self._lock = asyncio.Lock()
        self._unsub_interval = None

    async def async_start(self) -> None:
        """Start scanner scheduling and run initial scan."""
        self._schedule()
        await self.async_scan_once("startup")

    async def async_stop(self) -> None:
        """Stop scanner scheduling."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    async def async_update_options(self, options: dict[str, Any]) -> None:
        """Update scanner options and reschedule interval."""
        self._options = options
        self._schedule()

    async def async_scan_once(self, reason: str) -> ScanResult:
        """Execute one scan cycle."""
        if self._lock.locked():
            _LOGGER.debug("Skipping scan (%s): previous scan still running.", reason)
            return ScanResult(scanned=0, updated=0, unchanged=0, deleted=0, errors=0)

        async with self._lock:
            roots = _normalize_roots(self._options.get(CONF_PHOTO_ROOTS, DEFAULT_PHOTO_ROOTS))
            max_files = _to_int(
                self._options.get(CONF_MAX_SCAN_FILES_PER_RUN),
                DEFAULT_MAX_SCAN_FILES_PER_RUN,
                minimum=100,
                maximum=50000,
            )

            if not roots:
                _LOGGER.debug("No photo roots configured, skipping scan.")
                return ScanResult(scanned=0, updated=0, unchanged=0, deleted=0, errors=0)

            scan_id = f"entry:{self._entry_id}"
            await self._repository.async_set_scan_state(scan_id, "running", None)

            scanned = 0
            updated = 0
            unchanged = 0
            errors = 0
            seen_paths: set[str] = set()

            try:
                snapshot = await self._repository.async_get_active_snapshot(roots)

                for root in roots:
                    absolute_root = (Path(MEDIA_ROOT) / root).resolve()
                    if not absolute_root.exists() or not absolute_root.is_dir():
                        continue

                    for path in absolute_root.rglob("*"):
                        if scanned >= max_files:
                            break
                        if not path.is_file():
                            continue
                        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                            continue
                        if path.name.lower().startswith("thumb_"):
                            continue

                        rel_path = _to_media_relative(path)
                        if not rel_path:
                            continue

                        seen_paths.add(rel_path)
                        stat = path.stat()
                        mtime_utc = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                        size = int(stat.st_size)

                        existing = snapshot.get(rel_path)
                        if existing == (mtime_utc, size):
                            unchanged += 1
                            continue

                        source = _guess_source(rel_path)
                        try:
                            record = await self._hass.async_add_executor_job(
                                _build_record_from_file,
                                path,
                                rel_path,
                                source,
                                mtime_utc,
                                size,
                            )
                            await self._repository.async_upsert_photo(record)
                            updated += 1
                        except Exception as err:  # noqa: BLE001
                            errors += 1
                            _LOGGER.warning("Failed indexing photo %s: %s", rel_path, err)

                        scanned += 1

                    if scanned >= max_files:
                        _LOGGER.info(
                            "Scan file limit reached (%s). Increase %s if needed.",
                            max_files,
                            CONF_MAX_SCAN_FILES_PER_RUN,
                        )
                        break

                deleted = await self._repository.async_mark_deleted_missing(roots, seen_paths)
                total_active = await self._repository.async_count_active_photos()
                await self._repository.async_set_scan_state(scan_id, "ok", None)

                _LOGGER.info(
                    "Photo scan done (%s). updated=%s unchanged=%s deleted=%s errors=%s active=%s",
                    reason,
                    updated,
                    unchanged,
                    deleted,
                    errors,
                    total_active,
                )

                return ScanResult(
                    scanned=scanned,
                    updated=updated,
                    unchanged=unchanged,
                    deleted=deleted,
                    errors=errors,
                )
            except Exception as err:  # noqa: BLE001
                await self._repository.async_set_scan_state(scan_id, "error", str(err))
                _LOGGER.exception("Photo scan failed (%s): %s", reason, err)
                return ScanResult(scanned=scanned, updated=updated, unchanged=unchanged, deleted=0, errors=errors + 1)

    def _schedule(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

        interval_minutes = _to_int(
            self._options.get(CONF_INDEX_INTERVAL_MINUTES),
            DEFAULT_INDEX_INTERVAL_MINUTES,
            minimum=1,
            maximum=1440,
        )

        self._unsub_interval = async_track_time_interval(
            self._hass,
            self._async_interval_tick,
            timedelta(minutes=interval_minutes),
        )

    async def _async_interval_tick(self, _now: datetime) -> None:
        await self.async_scan_once("interval")


def _build_record_from_file(
    path: Path,
    media_rel_path: str,
    source: str,
    mtime_utc: str,
    size: int,
) -> PhotoIndexRecord:
    thumb_rel_path = _resolve_thumb_rel_path(path)
    width = None
    height = None
    captured_at_utc = None
    captured_source = None
    lat = None
    lon = None
    alt_m = None
    gps_accuracy_m = None

    with Image.open(path) as image:
        width, height = image.size
        exif = image.getexif() if hasattr(image, "getexif") else None
        if exif:
            captured_at_utc = _extract_exif_datetime(exif)
            if captured_at_utc:
                captured_source = "exif"

            gps = _extract_gps(exif)
            if gps is not None:
                lat, lon, alt_m = gps

    if not captured_at_utc:
        captured_at_utc = mtime_utc
        captured_source = "mtime"

    has_gps = bool(lat is not None and lon is not None and not (abs(lat) < 0.00001 and abs(lon) < 0.00001))
    geohash7 = _encode_geohash(lat, lon, precision=7) if has_gps else None

    return PhotoIndexRecord(
        media_rel_path=media_rel_path,
        thumb_rel_path=thumb_rel_path,
        source=source,
        file_size_bytes=size,
        mtime_utc=mtime_utc,
        sha256=None,
        width_px=width,
        height_px=height,
        captured_at_utc=captured_at_utc,
        captured_at_source=captured_source,
        lat=lat,
        lon=lon,
        alt_m=alt_m,
        gps_accuracy_m=gps_accuracy_m,
        geohash7=geohash7,
        has_gps=has_gps,
    )


def _extract_exif_datetime(exif: Any) -> str | None:
    tag_map = {ExifTags.TAGS.get(tag, tag): value for tag, value in exif.items()}

    date_text = (
        _as_text(tag_map.get("DateTimeOriginal"))
        or _as_text(tag_map.get("DateTimeDigitized"))
        or _as_text(tag_map.get("DateTime"))
    )
    if not date_text:
        return None

    dt = _parse_exif_datetime(date_text)
    if dt is None:
        return None

    offset_text = (
        _as_text(tag_map.get("OffsetTimeOriginal"))
        or _as_text(tag_map.get("OffsetTimeDigitized"))
        or _as_text(tag_map.get("OffsetTime"))
    )
    if offset_text:
        try:
            sign = -1 if offset_text.startswith("-") else 1
            hh = int(offset_text[1:3])
            mm = int(offset_text[4:6])
            offset = timedelta(hours=hh, minutes=mm) * sign
            dt = dt.replace(tzinfo=UTC) - offset
            return dt.astimezone(UTC).isoformat()
        except Exception:  # noqa: BLE001
            pass

    return dt.replace(tzinfo=UTC).isoformat()


def _parse_exif_datetime(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _extract_gps(exif: Any) -> tuple[float, float, float | None] | None:
    gps_map = _normalize_gps_map(exif)
    if not gps_map:
        return None

    lat_raw = gps_map.get("GPSLatitude")
    lat_ref = _as_text(gps_map.get("GPSLatitudeRef"))
    lon_raw = gps_map.get("GPSLongitude")
    lon_ref = _as_text(gps_map.get("GPSLongitudeRef"))
    if not lat_raw or not lon_raw or not lat_ref or not lon_ref:
        return None

    lat = _dms_to_decimal(lat_raw, lat_ref)
    lon = _dms_to_decimal(lon_raw, lon_ref)
    if lat is None or lon is None:
        return None

    alt = None
    alt_raw = gps_map.get("GPSAltitude")
    if alt_raw is not None:
        alt = _to_float(alt_raw)

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    return lat, lon, alt


def _normalize_gps_map(exif: Any) -> dict[str, Any]:
    gps_tag_id = 34853  # GPSInfo
    gps_info: Any = None

    # Preferred path for modern Pillow Exif object.
    get_ifd = getattr(exif, "get_ifd", None)
    if callable(get_ifd):
        try:
            gps_info = get_ifd(gps_tag_id)
        except Exception:  # noqa: BLE001
            gps_info = None

    # Fallback path for classic mapping-like EXIF payloads.
    if gps_info is None:
        try:
            tag_map = {ExifTags.TAGS.get(tag, tag): value for tag, value in exif.items()}
            candidate = tag_map.get("GPSInfo")
            if isinstance(candidate, dict):
                gps_info = candidate
        except Exception:  # noqa: BLE001
            gps_info = None

    if not isinstance(gps_info, dict):
        return {}

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in gps_info.items():
        if isinstance(raw_key, int):
            key = str(ExifTags.GPSTAGS.get(raw_key, raw_key))
        else:
            key = str(raw_key)
        normalized[key] = raw_value

    return normalized


def _dms_to_decimal(values: Any, ref: str) -> float | None:
    if not isinstance(values, (tuple, list)) or len(values) < 3:
        return None

    deg = _to_float(values[0])
    mins = _to_float(values[1])
    secs = _to_float(values[2])
    if deg is None or mins is None or secs is None:
        return None

    decimal = deg + mins / 60 + secs / 3600
    ref_normalized = ref.strip().upper()
    if ref_normalized in {"S", "W"}:
        decimal *= -1
    return decimal


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        try:
            numerator = getattr(value, "numerator", None)
            denominator = getattr(value, "denominator", None)
            if numerator is None or denominator in (None, 0):
                return None
            return float(numerator) / float(denominator)
        except Exception:  # noqa: BLE001
            return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _resolve_thumb_rel_path(path: Path) -> str | None:
    thumb_path = path.parent / f"thumb_{path.name}"
    if not thumb_path.exists() or not thumb_path.is_file():
        return None
    return _to_media_relative(thumb_path)


def _to_media_relative(path: Path) -> str | None:
    try:
        return path.resolve().relative_to(Path(MEDIA_ROOT)).as_posix()
    except ValueError:
        return None


def _guess_source(media_rel_path: str) -> str:
    lowered = media_rel_path.lower()
    if "/onedrive/" in lowered or lowered.startswith("people_map_plus/onedrive/"):
        return "onedrive"
    return "local"


def _normalize_roots(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(item).strip() for item in raw]
    else:
        parts = DEFAULT_PHOTO_ROOTS

    normalized: list[str] = []
    for part in parts:
        if not part:
            continue
        cleaned = part.strip().replace("\\", "/").strip("/")
        if cleaned:
            normalized.append(cleaned)

    if not normalized:
        return DEFAULT_PHOTO_ROOTS.copy()
    return sorted(set(normalized))


def _to_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:  # noqa: BLE001
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _encode_geohash(lat: float | None, lon: float | None, precision: int = 7) -> str | None:
    if lat is None or lon is None:
        return None

    lat_interval = [-90.0, 90.0]
    lon_interval = [-180.0, 180.0]
    geohash = []
    is_even = True
    bit = 0
    ch = 0

    while len(geohash) < precision:
        if is_even:
            mid = (lon_interval[0] + lon_interval[1]) / 2
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_interval[0] = mid
            else:
                lon_interval[1] = mid
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_interval[0] = mid
            else:
                lat_interval[1] = mid

        is_even = not is_even
        if bit < 4:
            bit += 1
        else:
            geohash.append(_BASE32[ch])
            bit = 0
            ch = 0

    return "".join(geohash)
