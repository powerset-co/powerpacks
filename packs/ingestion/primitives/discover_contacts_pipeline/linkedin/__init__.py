"""LinkedIn ingestion package: Connections.csv import + enrichment (network_import.py).

Consumers import the submodule directly
(`from ...discover_contacts_pipeline.linkedin import network_import`); skills
invoke it by file path. The standalone discovery CLI (`discover.py`) and its
models were deleted with the retired discover-contacts orchestrator.

Changelog:
  2026-07-23 (audit batch 16): dropped the discover.py re-exports; the module
    and its only consumers (the orchestrator and its tests) were deleted.
"""
