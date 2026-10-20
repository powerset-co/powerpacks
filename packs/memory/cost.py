"""Memory uses the existing indexing prices and records actual provider usage."""
from __future__ import annotations

from dataclasses import dataclass

from packs.indexing.lib.llm_config import CHAT_MODEL_PRICES_PER_1K_USD
from packs.indexing.primitives.embed_records_checkpointed.embed_records_checkpointed import DEFAULT_COST_PER_1K_TOKENS, DEFAULT_MODEL


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    embedding: int = 0

    def cost(self, model: str, embedding_model: str) -> float | None:
        prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model)
        if prices is None or embedding_model != DEFAULT_MODEL:
            return None
        return (self.input * prices["input"] + self.output * prices["output"]
                + self.embedding * DEFAULT_COST_PER_1K_TOKENS) / 1000
