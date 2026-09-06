# Research test fixtures — provenance

Small verbatim excerpts of the pinned sources, so the importers have
deterministic input without the gitignored source cache. Each file has a
header citing the source and its pinned revision. They stay small on
purpose: a handful of rows is an individual fact with a citation; the
whole table is the bulk derivative the legal notes flag (see
`research/reports/legal-and-license-notes.md`, "Derived technical facts"
and flag 1), and the bulk lives only in the local cache.

| file | source (manifest id) | licence | size | why it is fine to keep |
|---|---|---|---|---|
| `d73_excerpt.csv` | `morguux-d73n47a0` @ `074bac9c` | unknown (no licence on the gist) | 19 data rows of 1645 | A test vector, not a redistribution of the table: rows chosen to exercise the parser and the reports (the OBD PIDs, the four soot rows the conflict report discusses, `unsigned char/int/long` and `motorola float` types, a negative ADD, a quoted description). Each row is an identifier + scale fact the reports already cite individually. Keep it at this size; do not grow it into a mirror. |
| `motor_ccpage_excerpt.xml` | `ediabaslib` @ `a7cef804` | GPL-3.0 | one `<fragment>`: 1 job, 5 display rows | Config fragment (data, not code) quoted for format-parsing tests; GPL sources contribute cited facts and test vectors, never implementation (standing rule in the legal notes). Nothing from it is copied into the runtime. |
| `customjobs_excerpt.xml` | `bmw-xdfs-testo` @ `54a7ce42` | unknown (no licence on the repo) | 5 custom jobs | Same reasoning as the CSV excerpt: a few job definitions to drive the importer, each already cited as a Tier C fact. |

Regenerating the *full* normalized output needs the cache
(`research/sources/README.md`); tests that need the full output skip
visibly when it is absent (`tests/research/test_provenance.py`,
`tests/research/test_d73_csv_importer.py::FullCsv`).

If a fixture ever needs more rows, add the rows the new test needs and
say why in this table — do not paste the source file.
