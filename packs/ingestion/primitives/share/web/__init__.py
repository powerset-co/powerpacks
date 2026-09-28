"""Local web UI for the share decision: the People page's data routes.

The page itself is the React app (`packs/shared/web/app.py` serves it); this
package serves `/api/people/*`.
"""

from pathlib import Path

# The row virtualizer, vendored (MIT); the Searches page's virtual-table.js imports it.
VIRTUAL_CORE_JS = Path(__file__).resolve().parent / "vendor" / "tanstack-virtual-core.js"

__all__ = ["VIRTUAL_CORE_JS"]
