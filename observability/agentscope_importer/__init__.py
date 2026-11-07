"""M5: headless AgentScope/importer contract.

One metadata-only, versioned end-to-end import: fleet schema-v1 spans
are mapped to AgentScope canonical 0.1 documents, written to a bounded
local SQLite ledger, and acknowledged with a receiver receipt.

Public API:
  importer.import_spans / import_otlp_payload /
      import_canonical_document / import_file
  mapper.map_spans
  ledger.open_ledger
  verify.run_verify / real_import_check
  importer.ImportSettings
"""

from .importer import (
    ImportRefused,
    ImportSettings,
    import_canonical_document,
    import_file,
    import_otlp_payload,
    import_spans,
)

__all__ = [
    "ImportRefused",
    "ImportSettings",
    "import_canonical_document",
    "import_file",
    "import_otlp_payload",
    "import_spans",
]
