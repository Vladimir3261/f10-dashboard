#!/usr/bin/env python3
"""
ingest/server.py - the single write path into ClickHouse.

Runs on the VPS (in a container). Local sync agents POST compressed
telemetry batches here; this server authenticates them, decompresses,
normalizes raw channel names to the vehicle-agnostic taxonomy, and
INSERTs into ClickHouse over its HTTP interface. Clients never touch
ClickHouse directly - all credentials and the normalization map live
here, so the map can change centrally without touching any client.

Stdlib only (http.server + urllib + lzma). Configuration via env:

    INGEST_TOKEN        shared bearer token (required)
    INGEST_PORT         listen port (default 8090)
    CH_URL              ClickHouse HTTP endpoint (default http://clickhouse:8123)
    CH_USER / CH_PASS   ClickHouse credentials
    CH_DATABASE         database (default telemetry)
    CHANNEL_MAP         path to channel_map.json (default beside this file)
    MAX_BODY_BYTES      reject bodies larger than this (default 32 MiB)

Idempotency: the samples table is a ReplacingMergeTree, so a batch
re-sent after a lost ack collapses to the same rows. The client only
advances its watermark on a 2xx, giving effectively-once delivery over a
lossy mobile link.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import wire  # noqa: E402

TOKEN = os.environ.get("INGEST_TOKEN", "")
PORT = int(os.environ.get("INGEST_PORT", "8090"))
CH_URL = os.environ.get("CH_URL", "http://clickhouse:8123").rstrip("/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASS", "")
CH_DB = os.environ.get("CH_DATABASE", "telemetry")
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", str(32 * 1024 * 1024)))
MAP_PATH = os.environ.get(
    "CHANNEL_MAP", os.path.join(os.path.dirname(__file__), "channel_map.json")
)


# ------------------------------------------------------- normalization


class ChannelMap:
    """Raw -> normalized channel names, with per-VIN overrides."""

    def __init__(self, path: str):
        self.path = path
        self.default: Dict[str, str] = {}
        self.by_vehicle: Dict[str, Dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}

        self.default = {k: v for k, v in data.get("default", {}).items()
                        if not k.startswith("_")}
        self.by_vehicle = {
            k: v for k, v in data.items()
            if k not in ("default", "_comment") and isinstance(v, dict)
        }

    def normalize(self, vehicle_id: str, raw: str) -> str:
        override = self.by_vehicle.get(vehicle_id, {})

        if raw in override:
            return override[raw]

        #: Unknown channels pass through unchanged - stored raw and
        #: mappable later, never lost.
        return self.default.get(raw, raw)


CHANNEL_MAP = ChannelMap(MAP_PATH)


# ------------------------------------------------------- clickhouse i/o


def ch_insert(table: str, rows: List[Dict[str, Any]]) -> None:
    """INSERT rows into ClickHouse via the HTTP interface, JSONEachRow."""
    if not rows:
        return

    body = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows)
    query = f"INSERT INTO {CH_DB}.{table} FORMAT JSONEachRow"
    url = f"{CH_URL}/?query={urllib.parse.quote(query)}"

    request = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST"
    )

    if CH_USER:
        import base64
        cred = base64.b64encode(f"{CH_USER}:{CH_PASS}".encode()).decode()
        request.add_header("Authorization", f"Basic {cred}")

    with urllib.request.urlopen(request, timeout=30) as resp:
        resp.read()


def _ts(value: Any) -> str:
    """Epoch seconds (or a string) -> ClickHouse DateTime64 string, UTC."""
    if value is None:
        return "1970-01-01 00:00:00.000"

    if isinstance(value, str):
        return value

    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)

    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def build_samples(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = wire.rows_of(batch)
    meta = batch.get("meta", {})
    out = []

    for r in rows:
        vehicle = r.get("vehicle_id") or ""
        raw = r.get("channel_raw") or ""
        out.append({
            "vehicle_id": vehicle,
            "session_id": int(r.get("session_id") or 0),
            "ts": _ts(r.get("ts")),
            "channel_raw": raw,
            "channel": CHANNEL_MAP.normalize(vehicle, raw),
            "value": float(r.get("value") or 0.0),
            "unit": r.get("unit") or "",
            "quality": r.get("quality") or "ok",
            # Per-row (per-channel) version is authoritative - it is the
            # version of the mapping file that decoded THIS channel. The
            # batch-level meta value is only a coarse fallback for older
            # clients that ship no per-row version.
            "mapping_ver": r.get("mapping_ver") or meta.get("mapping_ver", ""),
        })

    return out


def build_sessions(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = wire.rows_of(batch)
    meta = batch.get("meta", {})
    out = []

    for r in rows:
        out.append({
            "vehicle_id": r.get("vehicle_id") or "",
            "session_id": int(r.get("session_id") or 0),
            "started": _ts(r.get("started")),
            "ended": _ts(r["ended"]) if r.get("ended") else None,
            "ecu": r.get("ecu") or "",
            "ecu_addr": r.get("ecu_addr"),
            "gateway": r.get("gateway") or "",
            "source_db": meta.get("db", ""),
            # "id@version,..." fingerprint of the mapping set for this run.
            "mappings": r.get("mappings") or "",
        })

    return out


BUILDERS = {"samples": build_samples, "sessions": build_sessions}


# ------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            ok = True
            detail = "ok"

            try:
                req = urllib.request.Request(f"{CH_URL}/ping")
                with urllib.request.urlopen(req, timeout=5) as r:
                    r.read()
            except Exception as exc:            # ClickHouse unreachable
                ok, detail = False, f"clickhouse: {exc}"

            self._json(200 if ok else 503, {"ok": ok, "detail": detail})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ingest":
            self._json(404, {"error": "not found"})
            return

        # -- auth ---------------------------------------------------
        auth = self.headers.get("Authorization", "")

        if not TOKEN or auth != f"Bearer {TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return

        # -- read body (bounded) ------------------------------------
        length = int(self.headers.get("Content-Length", "0"))

        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": f"body length {length} out of range"})
            return

        try:
            blob = self._read_exactly(length)
        except Exception as exc:
            self._json(400, {"error": f"short read: {exc}"})
            return

        # -- decode + insert ----------------------------------------
        try:
            batch = wire.decode(blob)
        except ValueError as exc:
            self._json(400, {"error": f"bad batch: {exc}"})
            return

        table = batch.get("table", "")
        builder = BUILDERS.get(table)

        if builder is None:
            self._json(400, {"error": f"unknown table {table!r}"})
            return

        try:
            rows = builder(batch)
            ch_insert(table, rows)
            self._log(batch, len(blob))
        except urllib.error.URLError as exc:
            #
            # ClickHouse unreachable/erroring - retryable. 503 tells the
            # client to back off and resend; the watermark stays put.
            #
            self._json(503, {"error": f"clickhouse: {exc}"})
            return
        except Exception as exc:                # defensive
            self._json(500, {"error": f"ingest failed: {exc}"})
            return

        self._json(200, {
            "ok": True, "table": table, "rows": batch.get("rows", 0),
            "cursor": batch.get("cursor", 0),
        })

    def _read_exactly(self, n: int) -> bytes:
        chunks = []
        remaining = n

        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))

            if not chunk:
                raise IOError("connection closed mid-body")

            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)

    def _log(self, batch: Dict[str, Any], nbytes: int) -> None:
        meta = batch.get("meta", {})
        cols = batch.get("cols", {})
        vehicle = (cols.get("vehicle_id") or [""])[0] if cols else ""

        try:
            ch_insert("ingest_log", [{
                "vehicle_id": vehicle,
                "source_db": meta.get("db", ""),
                "table_name": batch.get("table", ""),
                "rows": batch.get("rows", 0),
                "cursor": batch.get("cursor", 0),
                "bytes_in": nbytes,
            }])
        except Exception:
            pass                                # audit is best-effort


def main() -> int:
    if not TOKEN:
        print("[!] INGEST_TOKEN is not set - refusing to start without auth",
              file=sys.stderr)
        return 2

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"[+] ingest server on :{PORT} -> {CH_URL} (db {CH_DB})", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
