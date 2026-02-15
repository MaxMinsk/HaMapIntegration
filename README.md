# HaMapIntegration

Custom Home Assistant integration for People Map Plus.

## Scope (current)

1. Config entry + options flow.
2. SQLite index in `/config/.storage/people_map_plus.db`.
3. Background media scanner for photo metadata (EXIF GPS + capture time).
4. Service `people_map_plus.scan_now` for manual reindex.
5. REST API:
   - `GET /api/people_map_plus/status`
   - `GET /api/people_map_plus/photos`
   - `GET /api/people_map_plus/tracks`
   - `GET /api/people_map_plus/photo_proxy?path=...`

## Install (HACS Custom Repository)

1. Add this repository in HACS as **Integration**.
2. Install `People Map Plus`.
3. Restart Home Assistant.
4. Add integration from `Settings -> Devices & Services`.

## Configurable options

1. `photo_roots`
2. `index_interval_minutes`
3. `max_scan_files_per_run`
4. `default_photo_days`
5. `default_track_days`
6. `default_photo_limit`
7. `thumb_preferred`

## Development status

Integration API is now available and intended to be consumed directly from Lovelace card through Home Assistant auth/session.
