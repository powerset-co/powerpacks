"""Chat messages with an explicit cache boundary before candidate evidence."""
from typing import Any


def cached_messages(system: str, shared_text: str, candidate_text: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": shared_text, "prompt_cache_breakpoint": {"mode": "explicit"}},
            {"type": "text", "text": candidate_text},
        ]},
    ]
