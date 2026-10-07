"""Deep Context v2: candidates, ledgers, one node per block.

The specification is the "Deep Context Spec" page (2026-10-06). The old package
``packs.ingestion.primitives.deep_context`` is reused by import where a module is
pure (readers, dataclasses, normalizers, prompt builders, the OpenAI client);
nothing here imports anything that reads or writes the v1 store, its manifests,
or its legacy scrubs. Each block lists its v1 imports in its module docstring.
"""
