"""Constants for People Map Plus integration."""

from __future__ import annotations

DOMAIN = "people_map_plus"
PLATFORMS: list[str] = []

DEFAULT_PHOTO_ROOTS = ["people_map_plus/onedrive"]
DEFAULT_INDEX_INTERVAL_MINUTES = 15
DEFAULT_DEFAULT_PHOTO_DAYS = 5
DEFAULT_DEFAULT_TRACK_DAYS = 3
DEFAULT_DEFAULT_PHOTO_LIMIT = 200
DEFAULT_THUMB_PREFERRED = True
DEFAULT_MAX_SCAN_FILES_PER_RUN = 5000

CONF_PHOTO_ROOTS = "photo_roots"
CONF_INDEX_INTERVAL_MINUTES = "index_interval_minutes"
CONF_DEFAULT_PHOTO_DAYS = "default_photo_days"
CONF_DEFAULT_TRACK_DAYS = "default_track_days"
CONF_DEFAULT_PHOTO_LIMIT = "default_photo_limit"
CONF_THUMB_PREFERRED = "thumb_preferred"
CONF_MAX_SCAN_FILES_PER_RUN = "max_scan_files_per_run"

SCAN_SERVICE = "scan_now"

DB_FILE_NAME = "people_map_plus.db"
STORAGE_SUBDIR = ".storage"
MEDIA_ROOT = "/media"

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
