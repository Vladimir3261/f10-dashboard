# d73_excerpt.csv — provenance sidecar

The CSV importer (`research/importers/d73n47_csv.py`) has no comment
syntax — line 1 of `d73_excerpt.csv` must be the column row — so the
citation the other fixtures carry in-file lives here instead.

- **Source**: MorGuux, "D73N47A0 (BMW N47 DDE).csv", gist
  `832054bcbe6c1207b1f3075d5ecf6a4a`, revision
  `074bac9c7700fdc845bbdf4cd7784dd6be685ba2`
  (manifest id `morguux-d73n47a0`, `research/manifests/sources.yaml`).
- **Full file sha256** (pinned in the manifest):
  `105fd0efc1f8fadee7987fa86d83626067e9b9eb00ae0a72c19634c26b35746f`.
- **Licence**: unknown — no licence on the gist; the manifest marks the
  source `bulk_redistribution: withheld`.
- **Content**: the header row plus 19 of 1,645 data rows, copied
  verbatim. Each is an identifier + scale fact the tracked reports cite
  individually; the excerpt is a parser test vector, not a copy of the
  table. `README.md` in this directory says which rows and why.
- **Applicability**: SGBD `D73N47A0` (E84 X1 N47), not the target car's
  d72-compatible DDE — variants never merge.
