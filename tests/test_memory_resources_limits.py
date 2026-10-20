"""Request limits honor app headroom, configured limits and container limits."""
import unittest
from unittest.mock import patch

from packs.memory.resources import available_capacity, concurrency_limit


class ConcurrencyLimits(unittest.TestCase):
    def test_limits_cpu_memory_and_configuration(self):
        with patch.dict('os.environ', {'POWERPACKS_OPENAI_CONCURRENCY': '64'}):
            for cpus, mib, maximum, expected in ((16, 4096, 64, 64), (2, 1024, 64, 8),
                                                (16, 256, 64, 8), (16, 64, 64, 1),
                                                (16, 4096, 8, 8)):
                with self.subTest(cpus=cpus, mib=mib, maximum=maximum):
                    with patch('packs.memory.resources.available_capacity', return_value=(cpus, mib * 1024**2)):
                        self.assertEqual(concurrency_limit(maximum), expected)
        with patch.dict('os.environ', {'POWERPACKS_OPENAI_CONCURRENCY': '2'}), \
                patch('packs.memory.resources.available_capacity', return_value=(16, 4096 * 1024**2)):
            self.assertEqual(concurrency_limit(64), 2)

    def test_macos_reclaimable_pages(self):
        vm = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages inactive: 200.\n'
        with patch('sys.platform', 'darwin'), patch('os.cpu_count', return_value=8), \
                patch('subprocess.check_output', return_value=vm):
            self.assertEqual(available_capacity(), (8, 300 * 16384))

    def test_container_memory_and_cpu_quota(self):
        files = {'/proc/meminfo': 'MemAvailable: 8388608 kB\n', '/sys/fs/cgroup/memory.max': '1073741824',
                 '/sys/fs/cgroup/memory.current': '268435456', '/sys/fs/cgroup/cpu.max': '200000 100000'}
        with patch('sys.platform', 'linux'), patch('os.cpu_count', return_value=16), \
                patch('pathlib.Path.exists', lambda path: str(path) in files), \
                patch('pathlib.Path.read_text', lambda path: files[str(path)]):
            self.assertEqual(available_capacity(), (2, 768 * 1024**2))
