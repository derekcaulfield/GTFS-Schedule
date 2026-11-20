#!/usr/bin/env python3
"""Convert the national schedule export into a compact, stop-indexed Dublin pack.

Requires ijson (`python -m pip install ijson`). The output ZIP is designed for
random access: Android can read one `stops/<stop_id>.json` entry without
extracting or parsing the full national timetable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import ijson


DUBLIN_BOUNDS = (-6.60, 53.20, -5.90, 53.50)


def items(source: Path, prefix: str):
    with zipfile.ZipFile(source) as archive, archive.open("ireland.json.gz") as stream:
        yield from ijson.items(stream, f"{prefix}.item")


def main() -> None:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "ireland.json.gz.zip")
    output = Path(sys.argv[2] if len(sys.argv) > 2 else "dublin-schedules.zip")
    if not source.exists():
        raise SystemExit(f"Missing source archive: {source}")

    metadata = next(items(source, "metadata"), None)
    # `metadata` is an object, so read it separately using kvitems.
    with zipfile.ZipFile(source) as archive, archive.open("ireland.json.gz") as stream:
        metadata = dict(ijson.kvitems(stream, "metadata"))

    routes = {}
    for row in items(source, "routes"):
        route_id, number, name, _headsigns, operator, agency_id, route_type = row
        if int(route_type) == 3:
            routes[str(route_id)] = {
                "n": str(number), "name": str(name), "operator": str(operator), "agency": str(agency_id)
            }

    trips = {}
    for row in items(source, "trips"):
        trip_id, route_id, destination, service_id, direction = row
        if str(route_id) in routes:
            trips[str(trip_id)] = (str(route_id), str(destination), str(service_id), int(direction))

    west, south, east, north = DUBLIN_BOUNDS
    stops = {}
    for row in items(source, "stops"):
        stop_id, code, name, latitude, longitude, _parent, _location_type = row
        lat, lng = float(latitude), float(longitude)
        if south <= lat <= north and west <= lng <= east:
            stops[str(stop_id)] = {"code": str(code), "name": str(name), "lat": lat, "lng": lng}

    calendars = list(items(source, "calendar"))
    exceptions = list(items(source, "calendar_dates"))

    with tempfile.TemporaryDirectory() as temp_dir:
        db = sqlite3.connect(Path(temp_dir) / "departures.sqlite")
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("CREATE TABLE departures(stop_id TEXT, trip_id TEXT, departure INTEGER)")
        batch = []
        kept = 0
        for row in items(source, "stop_times"):
            stop_id, trip_id, _arrival, departure, _sequence, _headsign, _pickup, _dropoff, _timepoint = row
            stop_id, trip_id = str(stop_id), str(trip_id)
            if stop_id not in stops or trip_id not in trips:
                continue
            batch.append((stop_id, trip_id, int(departure)))
            kept += 1
            if len(batch) >= 25_000:
                db.executemany("INSERT INTO departures VALUES(?,?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO departures VALUES(?,?,?)", batch)
        db.execute("CREATE INDEX departures_stop ON departures(stop_id, departure)")
        db.commit()

        pack_manifest = {
            "schema_version": 1,
            "region": "Dublin",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_version": metadata.get("version", ""),
            "valid_from": metadata.get("valid_from", ""),
            "valid_until": metadata.get("valid_until", ""),
            "timezone": "Europe/Dublin",
            "stop_count": len(stops),
            "departure_count": kept,
            "calendar": calendars,
            "calendar_dates": exceptions,
        }

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("manifest.json", json.dumps(pack_manifest, separators=(",", ":")))
            cursor = db.execute("SELECT stop_id, trip_id, departure FROM departures ORDER BY stop_id, departure")
            current_stop = None
            departures = []

            def write_stop(stop_id: str, rows: list) -> None:
                stop = stops[stop_id]
                payload = {"stop": {"id": stop_id, **stop}, "departures": rows}
                archive.writestr(f"stops/{stop_id}.json", json.dumps(payload, separators=(",", ":")))

            for stop_id, trip_id, departure in cursor:
                if current_stop is not None and stop_id != current_stop:
                    write_stop(current_stop, departures)
                    departures = []
                current_stop = stop_id
                route_id, destination, service_id, direction = trips[trip_id]
                route = routes[route_id]
                departures.append([
                    route["n"], destination or route["name"], service_id, departure, trip_id, route_id, direction
                ])
            if current_stop is not None:
                write_stop(current_stop, departures)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    result = {
        "version": metadata.get("version", digest[:16]),
        "generated_at": pack_manifest["generated_at"],
        "valid_from": pack_manifest["valid_from"],
        "valid_until": pack_manifest["valid_until"],
        "size_bytes": output.stat().st_size,
        "sha256": digest,
        "download_url": "https://github.com/derekcaulfield/GTFS-Schedule/releases/latest/download/dublin-schedules.zip",
    }
    output.with_suffix(".manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({**result, "stops": len(stops), "departures": kept}, indent=2))


if __name__ == "__main__":
    main()
