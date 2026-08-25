# N47 unresolved questions

Concrete experiments, each with the evidence that would resolve it.
Read-only throughout; anything touching the car goes through the
allowlisted validation procedure at the bottom. The target vehicle is
`F10-520d-dev` (label — the VIN table is `local/VEHICLES.md`,
deliberately unhosted).

## 1. Which DDE variant does the target F10 actually run?

Everything downstream keys on this. Candidates from the audited
sources: the `d72n47a0` family ("N47TÜ/N57TÜ, DDE7.21/7.01/7.41,
F0x–F3x, UDS") and the `d70bx7a0` family ("N47O1/N57O1/B47, DDE7.01/
7.41, F15/F20/F30…"). A 2010–2013 F10 520d may carry the pre-TÜ N47,
which would make `d72n47a0` the WRONG table despite the F1x label.

**Evidence needed:** the DDE's identification over ENET — `22 F150`/
`22 F1A2`  are *not* assumed known; the honest first step is the
read-only identification our stack already performs (Mode 09 name,
`0x22 0xF190` VIN echo) plus a UDS `22 F1..` ident sweep captured for
offline comparison against the `d_motor` group's IDENT job results in
an EDIABAS oracle (pydiabas/Tool32 on a user-supplied installation).

## 2. Does the F303 dynamic sequence work on the target F10?

`mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml`, request
`n47.d72.dyn.4517`, enabled alone. Expected if the variant matches:
`62 F3 03` + 2 bytes decoding to a plausible oil temperature.
Cross-check: coolant PID 0x05 after a cold start (oil ≈ coolant when
cold, oil lags warm). A `7F 22 31` on the define or read is itself a
result: wrong variant family.

## 3. Can F303 hold more than one define at a time?

The evidence shows one define per read (klartext re-armed each time).
If a multi-source define (`2C 01 F3 03 id1 pos1 w1 id2 pos2 w2 …`) is
accepted, all four dynamic candidates could share one poll.
**Evidence needed:** a capture of ISTA/Tool32 performing
`STATUS_MESSWERTBLOCK_LESEN` with multiple ARGs on a d72-family DDE, or
a supervised on-car try of a two-source define with byte-exact
verification of both fields. Until then: one dynamic request at a time
(enforced by comment + validation report; see conflicts report).

## 4. Does the diesel DDE serve the 58xx static namespace?

`22 586F` gave u16 millibar on an F10 **N55**. If the diesel DDE
answers it, oil pressure becomes a one-request static read with no
define dance. **Evidence needed:** one supervised `22 586F` to the
discovered engine ECU; sanity: idle warm ≈ 1500–4500 mbar rising with
RPM. Also probe the unpinned oil-temp leads `22 5817` / `22 58EB`
against a COLD start (their source could not pin them on a warm one).

## 5. Compare d72 vs d73 vs DDE7-KWP DPF identifiers

`0x44BE/0x44C1` (d72, F303) vs `0x0405/0x0406` (D73 table) vs
`2C 10 04 06` (E90 capture). Same semantic domain, three wire dialects.
**Evidence needed for the F10:** question 2's session reading
`0x44BE`/`0x44C1`; a soot value that tracks the modelled/measured pair
(two reads a few minutes apart should nearly agree, as 15.49 vs 15.5
did on the F25).

## 6. WiCAN's 0x0AF1 scale claim vs the D73 table

Same identifier, same semantic (engine temperature), two scales:
`×0.01969` (issue table, no capture) vs `0.1·raw − 273.14` (D73 SGBD
export). At a warm-engine raw of ~3630 the first gives ~71.5, the
second ~89.9 — distinguishable with one real capture on an E-series
DDE7 car. Not testable on our F10; parked as a community follow-up.

## 7. Transmission data: DDE-received vs direct EGS

The D73 table proves the DDE *receives* gearbox values (0x0604–0x0608,
0x0EA6/0x0EA7 — turbine speed, oil temp, gear); OBDb claims direct EGS
DIDs (`DA12/DA25/DA2A/DA2E` at `0x18`) for 5-series. Our egs.py already
reaches `0x18` over HSFZ (the ELM327 gateway-block finding does not
apply to us). **Evidence needed:** `tools/egs.py scan --ecu 0x18`
results compared against the `DAxx` claims; if `22 DA 12` answers, one
byte with a plausible ATF temperature settles both the DID and the
"8-bit scalar" decode question. Then decide whether DDE-received values
(engine-side, no extra session) suffice for the dashboard.

## 8. KOMBI/JBE fuel-level jobs

`local/captures/kombi_dids.json` already holds a `0x60` DID scan from
the target car. **Evidence needed:** correlate those DIDs against a
fill-up (left sensor / right sensor / calculated / filtered / displayed
are distinct values on BMW body electronics); cross-check against the
Mode-01 `0x2F` percentage our production mapping does not receive from
this DDE.

## 9. Resolve the D73 CSV's ancestry and license

`dieslg8`'s `D73N57C0-MESSWERT.csv` is a sibling export. Diffing the
column conventions (and asking the gist author) would establish the
export tool and firm up the database-right analysis in the legal notes.

## Recommended first on-car validation sequence (read-only)

Precondition: ignition on, engine running for temperature plausibility;
`live.py` NOT polling (one HSFZ client at a time).

1. Standard identification (existing stack): discovery, VIN, Mode 09
   name, supported-PID walk — establishes the session baseline.
2. Question 1: UDS ident reads for variant resolution; record bytes.
3. Question 2: enable `n47.d72.dyn.4517` alone → expect ~46–110 °C oil.
4. Question 5: repeat for `44BE`, then `44C1`; compare the pair.
5. Question 4: `22 586F`, then `22 5817`/`22 58EB` (cold start next day).
6. Question 7: `tools/egs.py scan --ecu 0x18`; targeted `22 DA12/DA2E`.
7. Anything that answers gets promoted `candidate →
   locally_verified` in the mapping file's `verification:` block with
   the raw bytes recorded under `research/evidence/n47/f10_local/`;
   anything rejected gets `rejected` with the NRC. No silent upgrades.
