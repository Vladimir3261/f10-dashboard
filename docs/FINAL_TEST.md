# The final test — issue #15, from the driver's seat

One page, in order. Everything below is read-only on the car; nothing
here writes to an ECU. Tick as you go. Commands run on the Pi over SSH
from `~/f10-dashboard` unless marked *(laptop)*. The per-domain
reasoning, scales and failure signatures are in
[`TELEMETRY_CANDIDATES.md`](TELEMETRY_CANDIDATES.md); this page is the
what-to-type.

Prepared offline on 2026-09-10 (`tests/test_final_test_readiness.py`
loads every file below together: 34 `dde_dyn`, 15 `dde_slow`, 2 `egs`,
1 `egs_slow`; `config/modes.yaml` v3 treats the two candidate classes
like `slow`). Nothing below has run on the car yet.

## (a) Before leaving — the rebuilt dashboard (PR #44)

Do these at home, with the Pi on Wi-Fi and the car off.

1. **VPS** *(laptop, `infra/`)*: in `infra/.env` set
   `DASHBOARD_AUTH_PASSWORD` to the panel's password (the one
   `install.sh` printed on the Pi; `config.json` → `password` if you
   lost it) and either `PI_DASHBOARD_PORT=8088` or delete that line —
   `make deploy` now refuses `8080`. Then `make deploy`.
2. **Pi**: `git pull`, then
   `cd hardware/raspberry-pi/admin && sudo ./install.sh` (idempotent;
   re-prints the credentials). If `config.json` already existed, open
   it and make sure `"trusted_proxies": ["10.77.0.1"]` — an older file
   has `[]` — then `sudo systemctl restart f10-admin`.
3. **Pi**: `hardware/raspberry-pi/f10pi/scripts/verify.sh` — all OK,
   in particular `:8088` listening and `:8080` on the loopback only.
4. **Phone, off Wi-Fi**: `https://<DASHBOARD_DOMAIN>/` → the login
   prompt → all six tabs render (Drive · Detail · All data · System ·
   Car link · Claude). System → Services → **restart `f10-dashboard`**
   and watch it come back: the management surface works through the
   public name.
5. **Share link**: on the public page tap the **share** chip and mint
   a link (`/s/?t=…`); open it in a private window — telemetry only,
   **no login**, and nothing of the panel under it (`/s/api/status`,
   `/s/api/action/…` are 404).
6. **Pi off** (or `sudo systemctl stop wg-quick@wg0` on it): `/` →
   the login prompt, then the "car is unreachable" page; the share
   link (`/s/?t=…`) → the same page with no login. (`/` without a
   login is 401 either way — the unauthenticated offline page lives
   only under `/s/`.) Start it again before you leave.
7. Write down the cluster's **range** and **average consumption**
   before you start, and keep the pump receipt — item 6 below needs
   all three.

## (b) The validation drives — one file at a time, in this order

The ZGW serves **one** HSFZ client. Before every tool run:

```bash
sudo systemctl stop f10-dashboard          # or System → Services → stop
python3 tools/validate_candidate.py identify
```

`identify` first, every time — the artifact records the same ECU and
profile outcome the candidate is judged against. Every `run`/`sweep`
writes `validation-runs/<UTC>-<cmd>/` (tracked, VIN-redacted) and
`local/validation-runs-raw/<UTC>-<cmd>/` (raw, gitignored).

Where a domain has a *drive half*, launch it by hand — the unit stays
stopped — in `tmux` so an SSH drop does not end the run, bound to the
loopback so the panel's Drive tab keeps working:

```bash
tmux new -s drive
./run_car.sh --candidate <name> --host 127.0.0.1     # exactly one --candidate
```

Ctrl-C ends it. `sudo systemctl start f10-dashboard` restores the
normal launcher (no candidate) whenever you are done for the day.

| # | domain (`--candidate`) | conditions | the commands | pass / fail (short form) | drive half |
|---|---|---|---|---|---|
| 1 | Injector corrections (`injectors`) | warm idle: coolant > 80 °C, no load, A/C off | `python3 tools/validate_candidate.py run mappings/candidates/bmw/dde/n47/d72n47a0_injectors.yaml --all` then `… sweep mappings/candidates/bmw/dde/n47/d72n47a0_injectors.yaml --all --seconds 180` | **pass**: four corrections within ±2 mg/hub summing to ~0, setpoint a few mg/hub, misfire counters 0/small. **fail**: any outside ±10, all four bit-identical, a one-byte reply, or values clustering at **±20** (that is the d73 scale — note it, do not "fix" it in the car) | none |
| 2 | EGR position pair (`egr`) | warm idle 30 s, then a short drive with two hard accelerations | `python3 tools/validate_candidate.py sweep mappings/candidates/bmw/dde/n47/d72n47a0_egr.yaml --all --seconds 120` | **pass**: both positions 0..100 %, sensed tracks setpoint within ~5 % after a step, both move to one end under acceleration while `0x487E` drops. **fail**: a position outside 0..100, or the sensed value flat while `n47d_egr_deviation` moves | `./run_car.sh --candidate egr --host 127.0.0.1` — one drive, so `n47d_egr_deviation` (verified) is in the same session |
| 3 | VNT / swirl / throttle + governor deviation (`airpath`) | idle, then full-throttle pulls | `python3 tools/validate_candidate.py sweep mappings/candidates/bmw/dde/n47/d72n47a0_airpath.yaml --all --seconds 180` | **pass**: VNT set+act 0..100 and moving with the verified boost setpoint; governor deviation ~0 at steady cruise and = `boost_set − boost_act` in sign, within ~50 hPa; swirl moves at low rpm/load only; throttle near open except idle/overrun/shutdown. If the plain `TrbCh_*` rows are constant: **record it**, try the `*VNT` rows next time, not today | `./run_car.sh --candidate airpath --host 127.0.0.1` — with full-throttle pulls |
| 4 | IBS battery (`ibs`) | engine idling; a known load step: dipped beam (~8–9 A) or rear defroster (~15–20 A) | `python3 tools/validate_candidate.py sweep mappings/candidates/bmw/dde/n47/d72n47a0_ibs.yaml n47.d72.dyn.4286 n47.d72.dyn.428D --seconds 120` (switch the load on/off during it) then `python3 tools/validate_candidate.py run mappings/candidates/bmw/dde/n47/d72n47a0_ibs.yaml --all` | **pass**: the step reproduces within 2× with a small voltage dip; IBS voltage within 0.3 V of PID `0x42`; battery temp within ~10 °C of ambient when cold; SOC 0..100. **fail**: no step, a step wrong by > 2×, a constant | none |
| 5 | EGS speeds / converter slip (`egs-speeds`) | a drive with locked-up cruise (steady gear, steady speed) | `python3 tools/validate_candidate.py sweep mappings/candidates/bmw/egs/f10_transmission_speeds.yaml --all --seconds 300` (no `--ecu`: the file targets `0x18` itself) | **pass**: the two `DA2A` words are assignable — one is the turbine, the other proportional to road speed with a constant ratio per gear — and `DA12` tracks `0x46F0` affinely. **fail**: both track rpm, one does not move with speed, or `DA12` constant across a 20 °C warm-up | `./run_car.sh --candidate egs-speeds --host 127.0.0.1` — the drive is what assigns the words |
| 6 | Tank content (`tank`) | ignition on, engine off; **two** readings across a refuel of a known quantity (keep the receipt) | `python3 tools/validate_candidate.py run mappings/candidates/bmw/dde/n47/d72n47a0_tank.yaml` before and again after the refuel | **pass**: the refuel delta reproduces the pump quantity within ±2 l and the absolute agrees with range ÷ average consumption to ~2 l. **fail**: a constant across the refuel, or > 70 l | none (this one spans the refuel, not a drive) |
| 7 | DTC readout | engine running | `python3 tools/dtc.py --count --detail` then `python3 tools/dtc.py --count --detail --ecu 0x18` | **pass**: a `59 02` reply that parses (an empty list is a pass) and `19 01`'s count equal to the confirmed entries. **fail**: a reply that does not parse (the artifact keeps the bytes — bring them back) | none; the first `validation-runs/*-dtc/` artifact |
| 8 | Remaining SAE PIDs (`sae-extra`) | idle, then key-off during the sweep | `python3 tools/validate_candidate.py sweep mappings/candidates/obd/engine_sae_extra.yaml --all --seconds 60` | **pass**: `dtc_count` equals item 7's confirmed count, `mil` matches the cluster lamp, `throttle_cmd` tracks `0x11` within 10 % (both drop at key-off). **fail**: a count that disagrees with `0x19`, or `0x4C` constant while `0x11` moves | none (promotion = `engine.yaml` v6 + pin re-base, at the desk) |

Two candidates never ride the same drive — the rotation cost and the
cross-checks are per file. If a step fails, write down what you saw
and move on; a rejected row stays in the file as a documented dead end.

## (c) What to bring back

- Every `validation-runs/<UTC>-<cmd>/` directory — commit them all,
  pass or fail (they are VIN-redacted; the tool did that, do not edit
  them). The raw copies stay under `local/validation-runs-raw/` and
  are never committed.
- Every drive database: `local/sessions/drive-<UTC>.db` from each
  `--candidate` launch (the sync agent ships them; check System →
  Drive files says *shipped* before deleting anything).
- For each **pass**: the candidate file goes to
  `verification.status: verified`, `mapping.version: 2`, with the
  artifact directory named in `verification.method`
  (`mappings/candidates/bmw/dde/n47/d72n47a0_<x>.yaml`,
  `mappings/candidates/bmw/egs/f10_transmission_speeds.yaml`,
  `mappings/candidates/obd/engine_sae_extra.yaml`); item 5 also
  re-points the two `DA2A` words with the assignment the drive gave.
  For each **fail**: `rejected`, with the reason in `method`. Neither
  puts a file into `run_car.sh`'s default set — that is a separate
  decision per file (rotation cost).
- Comments to write: on **#15**, per domain: pass/fail, the artifact
  name, the number that decided it (the ±20 cluster, the 2× step, the
  ratio per gear…), and anything the file's `method` did not foresee.
  On **#41**, one line each for steps (a) 4–6: what the public URL,
  the share link and the offline page actually showed.

## (d) What the drive must NOT do

- **One client at a time.** `f10-dashboard` stopped before any
  `validate_candidate.py` / `dtc.py`; never two tools at once; never
  the laptop and the Pi on the car together.
- **Read-only.** Only `0x01`/`0x09`/`0x22`/`0x19`/`0x3E` and the
  `0x2C` define/clear/read subfunctions ever go out (`0x09` is the
  ident read during discovery; `0x3E` is permitted by the allowlist
  but nothing currently sends it); the tools refuse anything else at
  one choke point. No DTC clear (`0x14`
  has no code path), no routine, no write, no adaptation reset — not
  even "to see what happens".
- **No VIN** in anything committed: the tracked artifact is redacted
  by the tool; the drive databases and the raw copies never leave
  `local/`.
- **No edits in the car.** A scale that looks wrong is recorded, not
  corrected; `mapping.version` changes at the desk with the artifact
  beside it.
- **Do not re-run a sweep to make it pass.** One honest artifact per
  step; if conditions were wrong (cold engine, no load step), say so
  in the #15 comment and schedule it again.
