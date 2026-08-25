# Source cache

Third-party sources are **not committed** — they are cached under the
gitignored `local/research-cache/` and pinned (commit + sha256) in
`../manifests/sources.yaml`. `python3 -m research.build` refuses to run
a full import without them and refuses a D73 CSV whose hash does not
match the pin.

## Fetch commands

```bash
mkdir -p local/research-cache/{gists/morguux,ediabaslib,bmwxdfs,klartext,wican,misc}

# MorGuux D73N47A0 CSV — pinned gist revision
curl -sL "https://gist.githubusercontent.com/MorGuux/832054bcbe6c1207b1f3075d5ecf6a4a/raw/074bac9c7700fdc845bbdf4cd7784dd6be685ba2/D73N47A0%20(BMW%20N47%20DDE).csv" \
  -o "local/research-cache/gists/morguux/D73N47A0.csv"
# sha256 must be 105fd0efc1f8fadee7987fa86d83626067e9b9eb00ae0a72c19634c26b35746f

# Deep OBD E90 Motor.ccpage — pinned ediabaslib commit
curl -sL "https://raw.githubusercontent.com/uholeschak/ediabaslib/a7cef80490412115b16d700901573ec821f01ec8/BmwDeepObd/Xml/E90/Motor.ccpage" \
  -o local/research-cache/ediabaslib/Motor.ccpage

# TestO Datalogger customjobs.xml — pinned BMW-XDFs commit
curl -sL "https://raw.githubusercontent.com/MotorMouth93/BMW-XDFs/54a7ce420867452609e1116adc0afb8fe8a395ba/Me7.2/Datalogger/TestO%20Datalogger/config/customjobs.xml" \
  -o local/research-cache/bmwxdfs/customjobs.xml
```

Reference documents consulted during the audit (klartext docs, the
ediabasx-docs-sgbd pages, the WiCAN issue JSON, BMW-STATUS.md) live in
the same cache for convenience; the pipeline does not parse them — their
facts are transcribed with citations under `../evidence/n47/`.

Do **not** put BMW `.prg`/`.grp`, ISTA or DATEN files anywhere in this
repository, cached or otherwise. Future PRG-derived imports consume a
JSON/CSV export the user produces offline from a legally-acquired
installation.
