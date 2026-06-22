# rdiffweb, A web interface to rdiff-backup repositories
# Copyright (C) 2026 rdiffweb contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone

import cherrypy
from cherrypy.process.plugins import SimplePlugin

from rdiffweb.core.model import DiskUsage, RepoObject

CONTEXT = 'DISKUSAGE'


def _normalize_logical_path(path):
    """
    Normalize a logical path for consistent database storage and lookup.
    
    Rules:
    - os.path.normpath to resolve '.' (collapseseparators and resolve '..' etc.)
    - strip leading/trailing '/'
    - convert '.' to empty bytes

    This matches the normalization logic used by RepoObject.listdir()
    """
    path = os.path.normpath(path).strip(b'/')
    if path == b'.':
        path = b''
    return path


class DiskUsagePlugin(SimplePlugin):
    """
    Periodically scan backup storage and update disk usage for each repository
    using the `du` command-line tool. The scan is run with `nice` and `ionice`
    when available to reduce its impact on system resources.
    """

    execution_time = '02:00'

    # `nice` CPU priority level (0-19, higher means lower priority)
    nice_level = 19

    # `ionice` class: 1=realtime, 2=best-effort, 3=idle
    ionice_class = 3

    _lock = threading.Lock()

    def start(self):
        if not self.execution_time:
            return
        self.bus.log('Start DiskUsage plugin')
        self.bus.publish('scheduler:add_job_daily', self.execution_time, self._disk_usage_job)
        # Start the background process if disk usage is empty.
        if DiskUsage.query.first() is None:
            self.bus.publish('scheduler:add_job_now', self._disk_usage_job)

    def stop(self):
        self.bus.log('Stop DiskUsage plugin')
        self.bus.publish('scheduler:remove_job', self._disk_usage_job)

    def graceful(self):
        self.stop()
        self.start()

    def _scan_disk_usage(self, path):
        """
        Use `du` to scan all folder recursively to get the disk usage.

        Returns a list of (size, subpath) tuples only if the du command
        completes successfully with exit code 0.

        Raises RuntimeError if du fails.
        """
        cmd = []

        ionice = shutil.which(b'ionice')
        if ionice:
            cmd += [ionice, b'-c', str(self.ionice_class).encode()]

        nice = shutil.which(b'nice')
        if nice:
            cmd += [nice, b'-n', str(self.nice_level).encode()]

        cmd += [b'du', b'--block-size=1', path]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            raise RuntimeError(f'failed to start du for path {path!r}: {e}') from e

        results = []
        for line in process.stdout:
            line = line.rstrip(b'\n')
            parts = line.split(b'\t', 1)
            if len(parts) == 2:
                size_str, subpath = parts
                try:
                    results.append((int(size_str), subpath))
                except ValueError:
                    cherrypy.log(f'unexpected du output line {line!r}', severity=logging.WARNING, context=CONTEXT)
            else:
                cherrypy.log(f'du unexpected output: {line!r}', severity=logging.WARNING, context=CONTEXT)

        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f'd