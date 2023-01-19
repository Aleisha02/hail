import asyncio
import logging
import os
import shutil
import struct
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from hailtop.utils import check_shell_output, time_msecs, time_ns

log = logging.getLogger('resource_usage')


iptables_lock = asyncio.Lock()


class ResourceUsageMonitor:
    VERSION = 2
    missing_value = None

    @staticmethod
    def no_data() -> bytes:
        return ResourceUsageMonitor.version_to_bytes()

    @staticmethod
    def version_to_bytes() -> bytes:
        return struct.pack('>q', ResourceUsageMonitor.VERSION)

    @staticmethod
    def decode_to_df(data: bytes) -> Optional[pd.DataFrame]:
        if len(data) == 0:
            return None

        (version,) = struct.unpack_from('>q', data, 0)
        assert 1 <= version <= ResourceUsageMonitor.VERSION, version

        dtype = [
            ('time_msecs', '>i8'),
            ('memory_in_bytes', '>i8'),
            ('cpu_usage', '>f8'),
        ]

        if version > 1:
            assert version == ResourceUsageMonitor.VERSION, version
            dtype += [
                ('non_io_storage_in_bytes', '>i8'),
                ('io_storage_in_bytes', '>i8'),
                ('network_bandwidth_upload_in_bytes_per_second', '>f8'),
                ('network_bandwidth_download_in_bytes_per_second', '>f8'),
            ]
        np_array = np.frombuffer(data, offset=8, dtype=dtype)

        return pd.DataFrame.from_records(np_array)

    def __init__(
        self,
        container_name: str,
        container_overlay: str,
        io_volume_mount: Optional[str],
        veth_host: str,
        output_file_path: str,
    ):
        self.container_name = container_name
        self.container_overlay = container_overlay
        self.io_volume_mount = io_volume_mount
        self.veth_host = veth_host
        self.output_file_path = output_file_path

        self.is_attached_disk = io_volume_mount is not None and os.path.ismount(io_volume_mount)

        self.last_time_ns: Optional[int] = None
        self.last_cpu_ns: Optional[int] = None

        self.last_download_bytes: Optional[int] = None
        self.last_upload_bytes: Optional[int] = None
        self.last_time_msecs: Optional[int] = None

        self.out = open(output_file_path, 'wb')  # pylint: disable=consider-using-with
        self.write_header()

        self.task: Optional[asyncio.Future] = None

    def write_header(self):
        data = ResourceUsageMonitor.version_to_bytes()
        self.out.write(data)
        self.out.flush()

    def cpu_ns(self) -> Optional[int]:
        usage_file = f'/sys/fs/cgroup/cpu/{self.container_name}/cpuacct.usage'
        if os.path.exists(usage_file):
            with open(usage_file, 'r', encoding='utf-8') as f:
                return int(f.read().rstrip())
        return None

    def percent_cpu_usage(self) -> Optional[float]:
        now_time_ns = time_ns()
        now_cpu_ns = self.cpu_ns()

        if now_cpu_ns is None or self.last_cpu_ns is None or self.last_time_ns is None:
            self.last_time_ns = now_time_ns
            self.last_cpu_ns = now_cpu_ns
            return None

        cpu_usage = (now_cpu_ns - self.last_cpu_ns) / (now_time_ns - self.last_time_ns)

        self.last_time_ns = now_time_ns
        self.last_cpu_ns = now_cpu_ns

        return cpu_usage

    def memory_usage_bytes(self) -> Optional[int]:
        usage_file = f'/sys/fs/cgroup/memory/{self.container_name}/memory.usage_in_bytes'
        if os.path.exists(usage_file):
            with open(usage_file, 'r', encoding='utf-8') as f:
                return int(f.read().rstrip())
        return None

    def overlay_storage_usage_bytes(self) -> int:
        return shutil.disk_usage(self.container_overlay).used

    def io_storage_usage_bytes(self) -> int:
        if self.io_volume_mount is not None:
            return shutil.disk_usage(self.io_volume_mount).used
        return 0

    async def network_bandwidth(self) -> Tuple[Optional[float], Optional[float]]:
        async with iptables_lock:
            now_time_msecs = time_msecs()

            iptables_output, stderr = await check_shell_output(
                f'''
iptables -t mangle -L -v -n -x -w | grep "{self.veth_host}" | awk '{{ if ($6 == "{self.veth_host}" || $7 == "{self.veth_host}") print $2, $6, $7 }}'
'''
            )
        if stderr:
            log.exception(stderr)
            return (None, None)

        output = iptables_output.decode('utf-8').rstrip().splitlines()
        assert len(output) == 2, str((output, self.veth_host))

        now_upload_bytes = None
        now_download_bytes = None
        for line in output:
            fields = line.split()
            bytes_transmitted = int(fields[0])

            if fields[1] == self.veth_host and fields[2] != self.veth_host:
                now_upload_bytes = bytes_transmitted
            else:
                assert fields[1] != self.veth_host and fields[2] == self.veth_host, line
                now_download_bytes = bytes_transmitted

        assert now_upload_bytes is not None and now_download_bytes is not None, output

        if self.last_upload_bytes is None or self.last_download_bytes is None or self.last_time_msecs is None:
            self.last_time_msecs = time_msecs()
            self.last_upload_bytes = now_upload_bytes
            self.last_download_bytes = now_download_bytes
            return (None, None)

        upload_bandwidth = (now_upload_bytes - self.last_upload_bytes) / (now_time_msecs - self.last_time_msecs)
        download_bandwidth = (now_download_bytes - self.last_download_bytes) / (now_time_msecs - self.last_time_msecs)

        upload_bandwidth_mb_sec = (upload_bandwidth / 1024 / 1024) * 1000
        download_bandwidth_mb_sec = (download_bandwidth / 1024 / 1024) * 1000

        self.last_time_msecs = now_time_msecs
        self.last_upload_bytes = now_upload_bytes
        self.last_download_bytes = now_download_bytes

        return (upload_bandwidth_mb_sec, download_bandwidth_mb_sec)

    async def measure(self):
        now = time_msecs()
        memory_usage_bytes = self.memory_usage_bytes()
        percent_cpu_usage = self.percent_cpu_usage()

        if memory_usage_bytes is None or percent_cpu_usage is None:
            return

        overlay_usage_bytes = self.overlay_storage_usage_bytes()
        io_usage_bytes = self.io_storage_usage_bytes()
        non_io_usage_bytes = overlay_usage_bytes if self.is_attached_disk else overlay_usage_bytes - io_usage_bytes
        network_upload_bytes_per_second, network_download_bytes_per_second = await self.network_bandwidth()

        if network_upload_bytes_per_second is None or network_download_bytes_per_second is None:
            return

        data = struct.pack(
            '>2qd2q2d',
            now,
            memory_usage_bytes,
            percent_cpu_usage,
            non_io_usage_bytes,
            io_usage_bytes,
            network_upload_bytes_per_second,
            network_download_bytes_per_second,
        )

        self.out.write(data)
        self.out.flush()

    async def __aenter__(self):
        async def periodically_measure():
            cancelled = False
            while True:
                try:
                    await self.measure()
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception:
                    log.exception(f'while monitoring {self.container_name}')
                finally:
                    if not cancelled:
                        await asyncio.sleep(5)

        self.task = asyncio.ensure_future(periodically_measure())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.out.close()