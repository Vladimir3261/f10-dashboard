# Security

## Scope

`f10-dashboard` is a read-only diagnostic logger for one car, run by
its owner, on a host the owner controls, over a cable. It is not a
product and has no users to protect other than the owner. In scope, in
the order the owner cares:

1. **Anything that could send a non-observational service to the
   car.** The allowlist is OBD `0x01`/`0x09`, UDS `0x22`, the `0x2C`
   define/clear/read subfunctions, `0x19`, `0x3E` — enforced at one
   choke point in `tools/validate_candidate.py` and by construction in
   `live.py`. A way past it, a decode that could be coerced into a
   write, or a mapping-format feature that executes, is the most
   serious thing that can be reported here.
2. **The ingest path** (`infra/ingest/`): the only writer into the
   lake, bearer-token auth, reachable from the internet. A token
   bypass, an unauthenticated write, or an injection through the
   normalized sample format.
3. **The Pi admin panel** (`hardware/raspberry-pi/admin/`): HTTP Basic
   auth, LAN-only bind, a sudoers allowlist with no wildcards. An
   escape from that allowlist or an unauthenticated action.
4. **Private data in the tracked tree**: a VIN, a token, a raw
   capture. `tools/check_hygiene.py` guards the obvious shapes; a
   report of something it missed is welcome.

Out of scope: BMW's gateway and ECUs themselves (report those to BMW),
ClickHouse / Grafana / Docker as software, and anything that requires
physical access to the car or the Pi — the threat model assumes the
cable is the owner's.

## Reporting

Open a GitHub issue if the report can be written without a working
exploit against someone else's car. Otherwise use GitHub's private
vulnerability reporting on this repository. Include the commit hash
and, for item 1, the exact bytes that would reach the wire.

There is no bug bounty and no response-time commitment. The owner will
read it, fix what is real, and credit the reporter in the commit if
wanted.
