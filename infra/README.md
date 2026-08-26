# infra/ — ClickHouse lake + telemetry sync

The long-term store for this project's telemetry, plus the pipe that
gets data there from the car over a mobile network. Two sides:

```
 CAR LAPTOP / EMBEDDED                     VPS
 ┌────────────┐   ┌───────────┐   HTTPS    ┌────────────┐   ┌────────────┐
 │  live.py   │──▶│  SQLite   │──▶ agent ─▶│  ingest    │──▶│ ClickHouse │
 │ (unchanged)│   │(telemetry)│  (compressed│  server   │   │  (lake)    │
 └────────────┘   └───────────┘   batches)  └────────────┘   └────────────┘
```

- **`clickhouse/init/001_schema.sql`** — the schema (narrow `samples`,
  `sessions`, `vehicles`, `ingest_log`). VIN is the `vehicle_id`;
  per-vehicle partitioning; `channel_raw` preserved, `channel` normalized
  server-side; `ReplacingMergeTree` for effectively-once ingest.
- **`ingest/server.py`** — the *only* writer into ClickHouse. Stdlib.
  Authenticates the bearer token, LZMA-decompresses the batch, normalizes
  raw channel names via `ingest/channel_map.json`, inserts over
  ClickHouse's HTTP interface.
- **`sync/agent.py`** — the fault-tolerant local client. Reads SQLite
  read-only, ships everything past a durable rowid watermark, one batch
  in flight at a time, retrying transient failures without ever
  double-counting. Exposes a control endpoint for the dashboard.
- **`common/wire.py`** — the shared columnar + LZMA batch format
  (~4 bytes/sample on the wire; ~3% of row-JSON).

## Deploy the lake (on the VPS)

```bash
git clone <this repo> && cd f10-dashboard/infra
cp .env.example .env          # set CH_PASS + INGEST_TOKEN (openssl rand -hex 32)
docker compose up -d          # ClickHouse + ingest server
docker compose logs -f ingest
```

ClickHouse is **not** exposed to the host — only the ingest server (on
the compose network) reaches it. The schema is created automatically on
first start.

**TLS is required in production.** The ingest port publishes to
`127.0.0.1` by default; put a TLS reverse proxy (caddy/traefik/nginx) in
front so the bearer token and telemetry never cross a mobile network in
the clear. Example with Caddy: `telemetry.example.com { reverse_proxy
127.0.0.1:8090 }`.

## Run the sync agent (on the car laptop)

```bash
cp infra/sync/config.example.json infra/sync/config.json   # gitignored
# edit: server_url (your https endpoint), token (== INGEST_TOKEN), databases
python3 infra/sync/agent.py --config infra/sync/config.json
```

- **First run uploads the entire backlog** (watermark starts at 0), then
  tails. `live.py` keeps writing SQLite exactly as before — the agent
  only reads.
- Point `databases` at the main `telemetry.db` and the per-drive
  `local/sessions/*.db`; each is tracked independently.
- Control it live: `GET :8091/sync/status`, `POST :8091/sync/pause`,
  `POST :8091/sync/resume` — the live.py dashboard surfaces these so you
  can watch/toggle sync during a drive.

## What survives a bad mobile link

- **Lost mid-upload** → the batch is retried from the same watermark; the
  server's ReplacingMergeTree collapses the replay. No duplicates.
- **Slow link** → one batch in flight at a time; no queue can build up.
- **Watermark is durable** (atomic sidecar file), so a restart resumes
  exactly where it left off — never re-uploading acked data, never
  skipping unacked data.
- **401 / bad request** → the agent pauses and surfaces the error instead
  of spinning.

## Changing the normalization later

`channel_raw` is always stored as the car reported it. To change how a
raw key maps to the vehicle-agnostic `channel`, edit
`ingest/channel_map.json` (restart ingest) and re-derive historical rows
in one query — no client change, no re-upload:

```sql
ALTER TABLE telemetry.samples UPDATE channel = 'new.name'
WHERE channel_raw = 'some_raw_key';
```

## Analytics (per-vehicle by design)

Query one car at a time — cross-vehicle mixing is noise. `ASOF JOIN`
assembles operating points from the narrow table; per-condition quantile
baselines + weekly residual trends give drift detection. Design notes and
recipes will live in `docs/ANALYTICS_DESIGN.md`.
