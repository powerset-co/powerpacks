"""Choose request concurrency once per stage, leaving memory for the app and SQLite."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int

_MIB = 1024 * 1024


def available_capacity() -> tuple[int, int]:
    cpus = os.cpu_count() or 1
    if sys.platform == 'darwin':
        output = subprocess.check_output(['vm_stat'], text=True)
        page_size = int(re.findall(r'page size of (\d+) bytes', output)[0])
        pages = dict(re.findall(r'^([^:]+):\s+(\d+)\.', output, re.MULTILINE))
        available = sum(int(pages[key]) for key in ('Pages free', 'Pages inactive')) * page_size
    else:
        info = Path('/proc/meminfo').read_text()
        available = int(re.findall(r'^MemAvailable:\s+(\d+)', info, re.MULTILINE)[0]) * 1024
        cgroup = Path('/sys/fs/cgroup')
        if (cgroup / 'memory.max').exists():
            limit = (cgroup / 'memory.max').read_text().strip()
            if limit != 'max':
                available = min(available, int(limit) - int((cgroup / 'memory.current').read_text()))
        if (cgroup / 'cpu.max').exists():
            quota, period = (cgroup / 'cpu.max').read_text().split()
            if quota != 'max':
                cpus = min(cpus, max(1, int(quota) // int(period)))
    return cpus, available


def concurrency_limit(maximum: int) -> int:
    cpus, available = available_capacity()
    configured = env_or_profile_int('POWERPACKS_OPENAI_CONCURRENCY', 'openai_concurrency', fallback=maximum)
    # Requests mostly wait on HTTP. Reserve 128 MiB, then budget 16 MiB per in-flight request.
    return max(1, min(maximum, configured, cpus * 4, (available - 128 * _MIB) // (16 * _MIB)))
