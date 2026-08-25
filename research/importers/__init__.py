"""
Source-specific importers.

Each importer turns ONE pinned external source into normalized research
records, deterministically: the same input file always produces the
identical records in the identical order. Importers preserve every
original field, normalize what has a defined normalization, keep the
unknown as the string "unknown", and never fabricate request semantics
the source does not establish.
"""

__all__ = [
    "d73n47_csv",
    "deep_obd_xml",
    "wican_issue_fixture",
    "klartext_f25",
    "test_o_customjobs",
]
