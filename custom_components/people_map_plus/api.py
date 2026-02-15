"""REST API views for People Map Plus integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import math
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import quote, unquote

from aiohttp import ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEFAULT_PHOTO_DAYS,
    CONF_DEFAULT_PHOTO_LIMIT,
    CONF_DEFAULT_TRACK_DAYS,
    CONF_PHOTO_ROOTS,
    CONF_THUMB_PREFERRED,
    DEFAULT_DEFAULT_PHOTO_DAYS,
    DEFAULT_DEFAULT_PHOTO_LIMIT,
    DEFAULT_DEFAULT_TRACK_DAYS,
    DEFAULT_PHOTO_ROOTS,
    DEFAULT_THUMB_PREFERRED,
    DOMAIN,
)

_PHOTO_PROXY_SECRET_DATA_KEY = "people_map_plus_photo_proxy_secret"
_PHOTO_PROXY_TTL_SECONDS = 60 * 60


class PeopleMapPlusStatusView(HomeAssistantView):
    """Expose integration status and defaults."""

    url = "/api/people_map_plus/status"
    name = "api:people_map_plus:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        entry, runtime = _get_primary_entry_runtime(hass)
        if entry is None or runtime is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "not_ready",
                    "message": "People Map Plus integration is not loaded.",
                },
                status=503,
            )

        options = _effective_options(entry)
        scan_state = await runtime.repository.async_get_scan_state(f"entry:{entry.entry_id}")
        active_count = await runtime.repository.async_count_active_photos()
        return web.json_response(
            {
                "success": True,
                "status": "ok",
                "entryId": entry.entry_id,
                "photos": {
                    "activeCount": active_count,
                },
                "scan": scan_state,
                "defaults": {
                    "photoDays": _clamp_int(
                        options.get(CONF_DEFAULT_PHOTO_DAYS),
                        minimum=1,
                        maximum=30,
                        fallback=DEFAULT_DEFAULT_PHOTO_DAYS,
                    ),
                    "trackDays": _clamp_int(
                        options.get(CONF_DEFAULT_TRACK_DAYS),
                        minimum=1,
                        maximum=30,
                        fallback=DEFAULT_DEFAULT_TRACK_DAYS,
                    ),
                    "photoLimit": _clamp_int(
                        options.get(CONF_DEFAULT_PHOTO_LIMIT),
                        minimum=1,
                        maximum=5000,
                        fallback=DEFAULT_DEFAULT_PHOTO_LIMIT,
                    ),
                    "thumbPreferred": _to_bool(
                        options.get(CONF_THUMB_PREFERRED),
                        fallback=DEFAULT_THUMB_PREFERRED,
                    ),
                },
            }
        )


class PeopleMapPlusPhotosView(HomeAssistantView):
    """Expose indexed photos from SQLite."""

    url = "/api/people_map_plus/photos"
    name = "api:people_map_plus:photos"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        entry, runtime = _get_primary_entry_runtime(hass)
        if entry is None or runtime is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "not_ready",
                    "message": "People Map Plus integration is not loaded.",
                },
                status=503,
            )

        options = _effective_options(entry)
        root_prefixes = _normalize_roots(
            options.get(CONF_PHOTO_ROOTS, DEFAULT_PHOTO_ROOTS)
        )
        default_days = _clamp_int(
            options.get(CONF_DEFAULT_PHOTO_DAYS),
            minimum=1,
            maximum=30,
            fallback=DEFAULT_DEFAULT_PHOTO_DAYS,
        )
        default_limit = _clamp_int(
            options.get(CONF_DEFAULT_PHOTO_LIMIT),
            minimum=0,
            maximum=50000,
            fallback=DEFAULT_DEFAULT_PHOTO_LIMIT,
        )
        thumb_preferred = _to_bool(
            options.get(CONF_THUMB_PREFERRED),
            fallback=DEFAULT_THUMB_PREFERRED,
        )

        from_raw = request.query.get("fromUtc")
        to_raw = request.query.get("toUtc")
        from_utc = _parse_optional_datetime(from_raw)
        to_utc = _parse_optional_datetime(to_raw) or datetime.now(UTC)
        if from_raw and from_utc is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_from_utc",
                    "message": "fromUtc must be an ISO-8601 timestamp.",
                },
                status=400,
            )
        if to_raw and _parse_optional_datetime(to_raw) is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_to_utc",
                    "message": "toUtc must be an ISO-8601 timestamp.",
                },
                status=400,
            )
        if from_utc is None:
            days = _clamp_int(
                request.query.get("days"),
                minimum=1,
                maximum=30,
                fallback=default_days,
            )
            from_utc = to_utc - timedelta(days=days)
        if from_utc >= to_utc:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_range",
                    "message": "fromUtc must be earlier than toUtc.",
                },
                status=400,
            )

        limit_raw = request.query.get("limit")
        limit = _normalize_photo_limit(limit_raw, default_limit)
        with_gps_only = _to_bool(request.query.get("withGps"), fallback=True)
        items = await runtime.repository.async_query_photos(
            root_prefixes=root_prefixes,
            from_utc=from_utc.isoformat(),
            to_utc=to_utc.isoformat(),
            limit=limit,
            with_gps_only=with_gps_only,
        )

        result_items: list[dict[str, Any]] = []
        for item in items:
            media_url = _build_photo_proxy_url(hass, item.get("media_rel_path"))
            thumb_url = _build_photo_proxy_url(hass, item.get("thumb_rel_path"))
            preview_url = thumb_url if thumb_preferred and thumb_url else media_url
            result_items.append(
                {
                    "mediaRelPath": item.get("media_rel_path"),
                    "thumbRelPath": item.get("thumb_rel_path"),
                    "mediaUrl": media_url,
                    "thumbUrl": thumb_url,
                    "previewUrl": preview_url,
                    "source": item.get("source"),
                    "fileSizeBytes": item.get("file_size_bytes"),
                    "mtimeUtc": item.get("mtime_utc"),
                    "widthPx": item.get("width_px"),
                    "heightPx": item.get("height_px"),
                    "capturedAtUtc": item.get("captured_at_utc"),
                    "capturedAtSource": item.get("captured_at_source"),
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "altM": item.get("alt_m"),
                    "gpsAccuracyM": item.get("gps_accuracy_m"),
                    "geohash7": item.get("geohash7"),
                }
            )

        return web.json_response(
            {
                "success": True,
                "status": "ok",
                "fromUtc": from_utc.isoformat(),
                "toUtc": to_utc.isoformat(),
                "limit": limit,
                "isUnlimited": limit is None,
                "withGps": with_gps_only,
                "count": len(result_items),
                "items": result_items,
            }
        )


class PeopleMapPlusTracksView(HomeAssistantView):
    """Expose entity tracks from Home Assistant history."""

    url = "/api/people_map_plus/tracks"
    name = "api:people_map_plus:tracks"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        entry, runtime = _get_primary_entry_runtime(hass)
        if entry is None or runtime is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "not_ready",
                    "message": "People Map Plus integration is not loaded.",
                },
                status=503,
            )

        entities_raw = str(request.query.get("entities", "")).strip()
        entities = sorted(
            {
                entity.strip()
                for entity in entities_raw.split(",")
                if entity and entity.strip()
            }
        )
        if not entities:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_entities",
                    "message": "Provide comma-separated entities, for example entities=person.max,person.maria",
                },
                status=400,
            )

        options = _effective_options(entry)
        default_days = _clamp_int(
            options.get(CONF_DEFAULT_TRACK_DAYS),
            minimum=1,
            maximum=30,
            fallback=DEFAULT_DEFAULT_TRACK_DAYS,
        )
        from_raw = request.query.get("fromUtc")
        to_raw = request.query.get("toUtc")
        from_utc = _parse_optional_datetime(from_raw)
        to_utc = _parse_optional_datetime(to_raw) or datetime.now(UTC)
        if from_raw and from_utc is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_from_utc",
                    "message": "fromUtc must be an ISO-8601 timestamp.",
                },
                status=400,
            )
        if to_raw and _parse_optional_datetime(to_raw) is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_to_utc",
                    "message": "toUtc must be an ISO-8601 timestamp.",
                },
                status=400,
            )
        if from_utc is None:
            days = _clamp_int(
                request.query.get("days"),
                minimum=1,
                maximum=30,
                fallback=default_days,
            )
            from_utc = to_utc - timedelta(days=days)
        if from_utc >= to_utc:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_range",
                    "message": "fromUtc must be earlier than toUtc.",
                },
                status=400,
            )

        max_points = _clamp_int(
            request.query.get("maxPoints"),
            minimum=50,
            maximum=5000,
            fallback=500,
        )
        min_distance = _clamp_float(
            request.query.get("minDistanceMeters"),
            minimum=0.0,
            maximum=2000.0,
            fallback=0.0,
        )

        history_payload, status_code, error_text = await _fetch_history_payload(
            hass=hass,
            request=request,
            entities=entities,
            from_utc=from_utc,
            to_utc=to_utc,
        )
        if history_payload is None:
            return web.json_response(
                {
                    "success": False,
                    "status": "history_error",
                    "message": f"History API request failed with status {status_code}: {error_text}",
                    "fromUtc": from_utc.isoformat(),
                    "toUtc": to_utc.isoformat(),
                    "tracks": [],
                    "totalPoints": 0,
                },
                status=500,
            )

        tracks = _parse_tracks(
            payload=history_payload,
            max_points_per_entity=max_points,
            min_distance_meters=min_distance,
        )
        total_points = sum(len(track["points"]) for track in tracks)
        return web.json_response(
            {
                "success": True,
                "status": "ok",
                "message": f"Loaded tracks for {len(tracks)} entities.",
                "fromUtc": from_utc.isoformat(),
                "toUtc": to_utc.isoformat(),
                "tracks": tracks,
                "totalPoints": total_points,
            }
        )


class PeopleMapPlusPhotoProxyView(HomeAssistantView):
    """Serve indexed photo files via authenticated integration endpoint."""

    url = "/api/people_map_plus/photo_proxy"
    name = "api:people_map_plus:photo_proxy"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass = request.app["hass"]
        raw_path = str(request.query.get("path", "")).strip()
        raw_exp = str(request.query.get("exp", "")).strip()
        raw_sig = str(request.query.get("sig", "")).strip()
        if not raw_path:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_path",
                    "message": "Query param 'path' is required.",
                },
                status=400,
            )

        if not _is_valid_photo_proxy_signature(hass, raw_path, raw_exp, raw_sig):
            return web.json_response(
                {
                    "success": False,
                    "status": "unauthorized",
                    "message": "Invalid or expired photo proxy signature.",
                },
                status=401,
            )

        normalized = unquote(raw_path).replace("\\", "/").strip("/")
        if not normalized:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_path",
                    "message": "Invalid path.",
                },
                status=400,
            )

        media_root = Path("/media").resolve()
        target = (media_root / normalized).resolve()
        try:
            target.relative_to(media_root)
        except ValueError:
            return web.json_response(
                {
                    "success": False,
                    "status": "invalid_path",
                    "message": "Path escapes media root.",
                },
                status=400,
            )

        if not target.exists() or not target.is_file():
            return web.json_response(
                {
                    "success": False,
                    "status": "not_found",
                    "message": "File not found.",
                },
                status=404,
            )

        return web.FileResponse(path=target)


def _get_primary_entry_runtime(hass: Any) -> tuple[Any | None, Any | None]:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None, None

    entry = entries[0]
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return entry, runtime


def _effective_options(entry: Any) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def _parse_optional_datetime(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    text = raw_value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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

    return sorted(set(normalized)) or DEFAULT_PHOTO_ROOTS.copy()


def _build_photo_proxy_url(hass: Any, media_rel_path: Any) -> str | None:
    if not isinstance(media_rel_path, str):
        return None

    normalized = media_rel_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return None

    exp = int(time.time()) + _PHOTO_PROXY_TTL_SECONDS
    sig = _build_photo_proxy_signature(hass, normalized, exp)
    return f"/api/people_map_plus/photo_proxy?path={quote(normalized, safe='/')}&exp={exp}&sig={sig}"


def _build_photo_proxy_signature(hass: Any, media_rel_path: str, exp: int) -> str:
    secret = _get_photo_proxy_secret(hass)
    payload = f"{media_rel_path}\n{exp}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _is_valid_photo_proxy_signature(hass: Any, raw_path: str, raw_exp: str, raw_sig: str) -> bool:
    if not raw_sig or not raw_exp:
        return False

    try:
        exp = int(raw_exp)
    except ValueError:
        return False

    now = int(time.time())
    if exp < now:
        return False
    if exp > now + (24 * 60 * 60):
        return False

    normalized = unquote(raw_path).replace("\\", "/").strip("/")
    if not normalized:
        return False

    expected = _build_photo_proxy_signature(hass, normalized, exp)
    return hmac.compare_digest(expected, raw_sig)


def _get_photo_proxy_secret(hass: Any) -> bytes:
    current = hass.data.get(_PHOTO_PROXY_SECRET_DATA_KEY)
    if isinstance(current, bytes) and current:
        return current

    generated = secrets.token_bytes(32)
    hass.data[_PHOTO_PROXY_SECRET_DATA_KEY] = generated
    return generated


def _to_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return fallback


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _normalize_photo_limit(raw: Any, default_limit: int) -> int | None:
    if raw is None:
        parsed = default_limit
    else:
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            parsed = default_limit

    if parsed <= 0:
        return None

    # Hard guard against accidental huge payloads.
    return min(parsed, 200000)


async def _fetch_history_payload(
    hass: Any,
    request: web.Request,
    entities: list[str],
    from_utc: datetime,
    to_utc: datetime,
) -> tuple[list[Any] | None, int, str]:
    start_part = quote(from_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), safe="")
    history_url = f"http://127.0.0.1:8123/api/history/period/{start_part}"

    headers: dict[str, str] = {}
    for header_name in ("Authorization", "X-HA-Access", "Cookie"):
        header_value = request.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    params = {
        "end_time": to_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "filter_entity_id": ",".join(entities),
        "significant_changes_only": "0",
    }

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            history_url,
            params=params,
            headers=headers,
            timeout=ClientTimeout(total=45),
        ) as response:
            if response.status != 200:
                return None, response.status, (await response.text())[:400]
            payload = await response.json()
            if not isinstance(payload, list):
                return None, 500, "History payload has unexpected shape."
            return payload, 200, ""
    except Exception as err:  # noqa: BLE001
        return None, 500, str(err)


def _parse_tracks(
    payload: list[Any],
    max_points_per_entity: int,
    min_distance_meters: float,
) -> list[dict[str, Any]]:
    by_entity: dict[str, list[dict[str, Any]]] = {}

    for entity_bucket in payload:
        if not isinstance(entity_bucket, list):
            continue

        for state in entity_bucket:
            if not isinstance(state, dict):
                continue

            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                continue

            attributes = state.get("attributes")
            if not isinstance(attributes, dict):
                continue

            lat = _to_float(attributes.get("latitude"))
            lon = _to_float(attributes.get("longitude"))
            if lat is None or lon is None:
                continue

            ts = _parse_optional_datetime(state.get("last_updated")) or _parse_optional_datetime(
                state.get("last_changed")
            )
            if ts is None:
                continue

            accuracy = _to_int_or_none(attributes.get("gps_accuracy"))
            state_name = state.get("state") if isinstance(state.get("state"), str) else None

            by_entity.setdefault(entity_id, []).append(
                {
                    "lat": lat,
                    "lon": lon,
                    "ts": ts,
                    "accuracy": accuracy,
                    "state": state_name,
                }
            )

    tracks: list[dict[str, Any]] = []
    for entity_id in sorted(by_entity):
        simplified = _simplify_points(
            raw_points=by_entity[entity_id],
            max_points_per_entity=max_points_per_entity,
            min_distance_meters=min_distance_meters,
        )
        if not simplified:
            continue

        tracks.append(
            {
                "entityId": entity_id,
                "points": [
                    {
                        "lat": point["lat"],
                        "lon": point["lon"],
                        "ts": point["ts"].isoformat(),
                        "accuracy": point["accuracy"],
                        "state": point["state"],
                    }
                    for point in simplified
                ],
            }
        )

    return tracks


def _simplify_points(
    raw_points: list[dict[str, Any]],
    max_points_per_entity: int,
    min_distance_meters: float,
) -> list[dict[str, Any]]:
    if not raw_points:
        return []

    ordered = sorted(raw_points, key=lambda point: point["ts"])
    deduped: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None

    for point in ordered:
        if last is None:
            deduped.append(point)
            last = point
            continue

        distance = _haversine_meters(last["lat"], last["lon"], point["lat"], point["lon"])
        same_coordinates = distance < 0.5
        same_time = abs((point["ts"] - last["ts"]).total_seconds()) < 1
        if same_coordinates and same_time:
            continue
        if distance < min_distance_meters:
            continue

        deduped.append(point)
        last = point

    if len(deduped) <= max_points_per_entity or max_points_per_entity <= 1:
        return deduped

    sampled: list[dict[str, Any]] = []
    used_indexes: set[int] = set()
    step = (len(deduped) - 1) / (max_points_per_entity - 1)
    for index in range(max_points_per_entity):
        sampled_index = int(round(index * step))
        sampled_index = max(0, min(len(deduped) - 1, sampled_index))
        if sampled_index in used_indexes:
            continue
        used_indexes.add(sampled_index)
        sampled.append(deduped[sampled_index])

    if sampled and sampled[-1]["ts"] != deduped[-1]["ts"]:
        sampled.append(deduped[-1])

    return sorted(sampled, key=lambda point: point["ts"])


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_m * c


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            numerator = getattr(value, "numerator", None)
            denominator = getattr(value, "denominator", None)
            if numerator is None or denominator in (None, 0):
                return None
            return float(numerator) / float(denominator)
        except (TypeError, ValueError):
            return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
