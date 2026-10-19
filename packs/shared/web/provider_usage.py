"""Read account usage on expansion, separately from fast local credential status."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from packs.shared.web import account


def read(root: Path, provider: str) -> dict[str, object]:
    if provider != "modal":
        raise KeyError(provider)
    values = account.config(root)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30)
    # The installed SDK's official CLI keeps credentials scoped to this query and releases
    # its gRPC client on exit. A slow billing service never holds up account rendering.
    result = subprocess.run(
        [sys.executable, "-m", "modal", "billing", "report", "--start", start.date().isoformat(),
         "--end", end.date().isoformat(), "--json"],
        env={**os.environ, "MODAL_TOKEN_ID": values.get("MODAL_TOKEN_ID", ""),
             "MODAL_TOKEN_SECRET": values.get("MODAL_TOKEN_SECRET", "")},
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("Modal couldn't read billing. Billing reports require a Team or Enterprise plan.")
    total = sum((Decimal(row["cost"]) for row in json.loads(result.stdout)), Decimal(0))
    return {"metrics": [{"label": "Last 30 days", "value": f"${total:,.2f} USD"}],
            "note": f"{start:%b %d}–{end - timedelta(days=1):%b %d}, UTC. Metered workspace usage before credits and discounts."}
