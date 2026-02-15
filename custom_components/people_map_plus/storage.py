"""SQLite storage for People Map Plus photo index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any

from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class PhotoIndexRecord:
    """Normalized photo record for storage."""

    media_rel_path: str
    thumb_rel_path: str | None
    source: str
    file_size_bytes: int | None
    mtime_utc: str
    sha256: str | None
    width_px: int | None
    height_px: int | None
    captured_at_utc: str | None
    captured_at_source: str | None
    lat: float | None
    lon: float | None
    alt_m: float | None
    gps_accuracy_m: float | None
    geohash7: str | None
    has_gps: bool


class PhotoIndexRepository:
    """Repository for photo metadata index."""

    def __init__(self, hass: HomeAssistant, db_path: Path) -> None:
        self._hass = hass
        self._db_path = db_path

    async def async_initialize(self) -> None:
        """Create database schema if required."""
        await self._hass.async_add_executor_job(self._initialize)

    async def async_get_active_snapshot(
        self, root_prefixes: list[str]
    ) -> dict[str, tuple[str, int | None]]:
        """Return active indexed files mapped by path => (mtime_utc, file_size_bytes)."""
        return await self._hass.async_add_executor_job(self._get_active_snapshot, root_prefixes)

    async def async_upsert_photo(self, record: PhotoIndexRecord) -> None:
        """Insert or update single photo row."""
        await self._hass.async_add_executor_job(self._upsert_photo, record)

    async def async_mark_deleted_missing(
        self, root_prefixes: list[str], seen_paths: set[str]
    ) -> int:
        """Mark indexed files as deleted when they are no longer present on disk."""
        return await self._hass.async_add_executor_job(
            self._mark_deleted_missing, root_prefixes, seen_paths
        )

    async def async_set_scan_state(self, scan_id: str, status: str, error: str | None) -> None:
        """Persist scanner state."""
        await self._hass.async_add_executor_job(self._set_scan_state, scan_id, status, error)

    async def async_count_active_photos(self) -> int:
        """Count active photos in index."""
        return await self._hass.async_add_executor_job(self._count_active_photos)

    async def async_query_photos(
        self,
        root_prefixes: list[str],
        from_utc: str,
        to_utc: str,
        limit: int,
        with_gps_only: bool,
    ) -> list[dict[str, Any]]:
        """Query recent indexed photos."""
        return await self._hass.async_add_executor_job(
            self._query_photos, root_prefixes, from_utc, to_utc, limit, with_gps_only
        )

    async def async_get_scan_state(self, scan_id: str) -> dict[str, Any] | None:
        """Return latest persisted scan state for scan_id."""
        return await self._hass.async_add_executor_job(self._get_scan_state, scan_id)

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
                  id INTEGER PRIMARY KEY,
                  media_rel_path TEXT UNIQUE NOT NULL,
                  thumb_rel_path TEXT NULL,
                  source TEXT NOT NULL,
                  file_size_bytes INTEGER NULL,
                  mtime_utc TEXT NOT NULL,
                  sha256 TEXT NULL,
                  width_px INTEGER NULL,
                  height_px INTEGER NULL,
                  captured_at_utc TEXT NULL,
                  captured_at_source TEXT NULL,
                  lat REAL NULL,
                  lon REAL NULL,
                  alt_m REAL NULL,
                  gps_accuracy_m REAL NULL,
                  geohash7 TEXT NULL,
                  has_gps INTEGER NOT NULL,
                  indexed_at_utc TEXT NOT NULL,
                  updated_at_utc TEXT NOT NULL,
                  is_deleted INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_photos_has_gps_captured
                ON photos(has_gps, captured_at_utc DESC);

                CREATE INDEX IF NOT EXISTS idx_photos_geohash7
                ON photos(geohash7);

                CREATE INDEX IF NOT EXISTS idx_photos_path
                ON photos(media_rel_path);

                CREATE TABLE IF NOT EXISTS scan_state (
                  scan_id TEXT PRIMARY KEY,
                  last_scan_started_utc TEXT NULL,
                  last_scan_finished_utc TEXT NULL,
                  last_scan_status TEXT NULL,
                  last_error TEXT NULL
                );

                CREATE TABLE IF NOT EXISTS track_cache (
                  cache_key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  expires_at_utc TEXT NOT NULL,
                  updated_at_utc TEXT NOT NULL
                );
                """
            )

    def _get_active_snapshot(self, root_prefixes: list[str]) -> dict[str, tuple[str, int | None]]:
        if not root_prefixes:
            return {}

        query, params = _build_roots_filter_query(
            """
            SELECT media_rel_path, mtime_utc, file_size_bytes
            FROM photos
            WHERE is_deleted = 0
            """,
            root_prefixes,
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        snapshot: dict[str, tuple[str, int | None]] = {}
        for row in rows:
            snapshot[str(row["media_rel_path"])] = (
                str(row["mtime_utc"]),
                int(row["file_size_bytes"]) if row["file_size_bytes"] is not None else None,
            )
        return snapshot

    def _upsert_photo(self, record: PhotoIndexRecord) -> None:
        now_utc = _utc_now_iso()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO photos (
                  media_rel_path, thumb_rel_path, source, file_size_bytes, mtime_utc,
                  sha256, width_px, height_px, captured_at_utc, captured_at_source,
                  lat, lon, alt_m, gps_accuracy_m, geohash7, has_gps,
                  indexed_at_utc, updated_at_utc, is_deleted
                ) VALUES (
                  ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?,
                  ?, ?, 0
                )
                ON CONFLICT(media_rel_path) DO UPDATE SET
                  thumb_rel_path = excluded.thumb_rel_path,
                  source = excluded.source,
                  file_size_bytes = excluded.file_size_bytes,
                  mtime_utc = excluded.mtime_utc,
                  sha256 = excluded.sha256,
                  width_px = excluded.width_px,
                  height_px = excluded.height_px,
                  captured_at_utc = excluded.captured_at_utc,
                  captured_at_source = excluded.captured_at_source,
                  lat = excluded.lat,
                  lon = excluded.lon,
                  alt_m = excluded.alt_m,
                  gps_accuracy_m = excluded.gps_accuracy_m,
                  geohash7 = excluded.geohash7,
                  has_gps = excluded.has_gps,
                  indexed_at_utc = excluded.indexed_at_utc,
                  updated_at_utc = excluded.updated_at_utc,
                  is_deleted = 0;
                """,
                (
                    record.media_rel_path,
                    record.thumb_rel_path,
                    record.source,
                    record.file_size_bytes,
                    record.mtime_utc,
                    record.sha256,
                    record.width_px,
                    record.height_px,
                    record.captured_at_utc,
                    record.captured_at_source,
                    record.lat,
                    record.lon,
                    record.alt_m,
                    record.gps_accuracy_m,
                    record.geohash7,
                    1 if record.has_gps else 0,
                    now_utc,
                    now_utc,
                ),
            )

    def _mark_deleted_missing(self, root_prefixes: list[str], seen_paths: set[str]) -> int:
        if not root_prefixes:
            return 0

        query, params = _build_roots_filter_query(
            """
            SELECT media_rel_path
            FROM photos
            WHERE is_deleted = 0
            """,
            root_prefixes,
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

            to_delete = [
                str(row["media_rel_path"])
                for row in rows
                if str(row["media_rel_path"]) not in seen_paths
            ]

            if not to_delete:
                return 0

            now_utc = _utc_now_iso()
            conn.executemany(
                """
                UPDATE photos
                SET is_deleted = 1, updated_at_utc = ?
                WHERE media_rel_path = ?
                """,
                [(now_utc, path) for path in to_delete],
            )

        return len(to_delete)

    def _set_scan_state(self, scan_id: str, status: str, error: str | None) -> None:
        now_utc = _utc_now_iso()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO scan_state (
                  scan_id, last_scan_started_utc, last_scan_finished_utc, last_scan_status, last_error
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scan_id) DO UPDATE SET
                  last_scan_started_utc = excluded.last_scan_started_utc,
                  last_scan_finished_utc = excluded.last_scan_finished_utc,
                  last_scan_status = excluded.last_scan_status,
                  last_error = excluded.last_error
                """,
                (scan_id, now_utc, now_utc, status, error),
            )

    def _count_active_photos(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(1) FROM photos WHERE is_deleted = 0"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _query_photos(
        self,
        root_prefixes: list[str],
        from_utc: str,
        to_utc: str,
        limit: int,
        with_gps_only: bool,
    ) -> list[dict[str, Any]]:
        base_query = """
            SELECT
              media_rel_path,
              thumb_rel_path,
              source,
              file_size_bytes,
              mtime_utc,
              width_px,
              height_px,
              captured_at_utc,
              captured_at_source,
              lat,
              lon,
              alt_m,
              gps_accuracy_m,
              geohash7,
              indexed_at_utc,
              updated_at_utc
            FROM photos
            WHERE is_deleted = 0
              AND captured_at_utc IS NOT NULL
              AND captured_at_utc >= ?
              AND captured_at_utc <= ?
        """

        if with_gps_only:
            base_query += " AND has_gps = 1 "

        query, root_params = _build_roots_filter_query(base_query, root_prefixes)
        query += " ORDER BY captured_at_utc DESC LIMIT ?"

        params: tuple[Any, ...] = (from_utc, to_utc, *root_params, limit)
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "media_rel_path": str(row["media_rel_path"]),
                    "thumb_rel_path": str(row["thumb_rel_path"]) if row["thumb_rel_path"] is not None else None,
                    "source": str(row["source"]),
                    "file_size_bytes": int(row["file_size_bytes"]) if row["file_size_bytes"] is not None else None,
                    "mtime_utc": str(row["mtime_utc"]),
                    "width_px": int(row["width_px"]) if row["width_px"] is not None else None,
                    "height_px": int(row["height_px"]) if row["height_px"] is not None else None,
                    "captured_at_utc": str(row["captured_at_utc"]) if row["captured_at_utc"] is not None else None,
                    "captured_at_source": str(row["captured_at_source"]) if row["captured_at_source"] is not None else None,
                    "lat": float(row["lat"]) if row["lat"] is not None else None,
                    "lon": float(row["lon"]) if row["lon"] is not None else None,
                    "alt_m": float(row["alt_m"]) if row["alt_m"] is not None else None,
                    "gps_accuracy_m": float(row["gps_accuracy_m"]) if row["gps_accuracy_m"] is not None else None,
                    "geohash7": str(row["geohash7"]) if row["geohash7"] is not None else None,
                    "indexed_at_utc": str(row["indexed_at_utc"]),
                    "updated_at_utc": str(row["updated_at_utc"]),
                }
            )

        return items

    def _get_scan_state(self, scan_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT scan_id, last_scan_started_utc, last_scan_finished_utc, last_scan_status, last_error
                FROM scan_state
                WHERE scan_id = ?
                """,
                (scan_id,),
            ).fetchone()

        if row is None:
            return None

        return {
            "scan_id": str(row["scan_id"]),
            "last_scan_started_utc": str(row["last_scan_started_utc"]) if row["last_scan_started_utc"] is not None else None,
            "last_scan_finished_utc": str(row["last_scan_finished_utc"]) if row["last_scan_finished_utc"] is not None else None,
            "last_scan_status": str(row["last_scan_status"]) if row["last_scan_status"] is not None else None,
            "last_error": str(row["last_error"]) if row["last_error"] is not None else None,
        }


def _build_roots_filter_query(base_query: str, root_prefixes: list[str]) -> tuple[str, tuple[Any, ...]]:
    filters = []
    params: list[Any] = []
    for prefix in root_prefixes:
        filters.append("media_rel_path LIKE ?")
        params.append(prefix.rstrip("/") + "/%")

    final_query = base_query.strip()
    if filters:
        final_query += " AND (" + " OR ".join(filters) + ")"

    return final_query, tuple(params)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
