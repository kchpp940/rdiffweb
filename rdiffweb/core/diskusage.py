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

import cherrypy
from cherrypy.process.plugins import SimplePlugin

from rdiffweb.core.model import DiskUsage, RepoObject
from rdiffweb.core.model._diskusage import normalize_logical_path

CONTEXT = 'DISKUSAGE'


class DiskUsagePlugin(SimplePlugin):
    """
    Periodically scan backup storage and update disk usage for each repository
    using the `du` command-line tool. The scan is run with `nice` and `ionice`
    when available to reduce its impact on system resources.

    Consistency guarantees:
    - Each repository is scanned independently; a failure in one repo does not affect others.
    - Results are collected in memory first and written atomically in a single transaction.
    - Stale rows are removed implicitly by replacing all rows for a repo on success.
    - Partial scans (du failure, mid-scan exception) never replace the existing data.
    - A process-wide lock prevents concurrent scans from interleaving.
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
        if DiskUsage.query.first() is None:
            self.bus.publish('scheduler:add_job_now', self._disk_usage_job)

    def stop(self):
        self.bus.log('Stop DiskUsage plugin')
        self.bus.publish('scheduler:remove_job', self._disk_usage_job)

    def graceful(self):
        self.stop()
        self.start()

    def _run_du(self, path):
        """
        Run `du` on the given path and return a list of ``(size_bytes, subpath)`` tuples.

        Raises ``RuntimeError`` if du fails (non-zero exit code) or cannot be started.
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
            raise RuntimeError(f'du failed for path {path!r} (exit {process.returncode})')

        return results

    def _collect_repo_usage(self, repo_obj):
        """
        Scan a single repository and return a dict keyed by normalized logical path
        with ``(mirror_size, increments_size)`` tuples.

        The entire repo scan either succeeds fully or raises an exception;
        partial results are never returned.
        """
        repo_path = repo_obj.full_path
        if not os.path.isdir(repo_path):
            raise RuntimeError(f"repository folder doesn't exist: {repo_path!r}")

        rdiff_data = os.path.join(repo_path, b'rdiff-backup-data')
        increments_prefix = os.path.join(rdiff_data, b'increments')

        raw_entries = self._run_du(repo_path)

        usage = {}
        for size, subpath in raw_entries:
            if subpath.startswith(increments_prefix):
                raw_rel = os.path.relpath(subpath, increments_prefix)
                logical = normalize_logical_path(raw_rel)
                mirror, incr = usage.get(logical, (None, None))
                usage[logical] = (mirror, size)
            elif subpath.startswith(rdiff_data):
                continue
            else:
                raw_rel = os.path.relpath(subpath, repo_path)
                logical = normalize_logical_path(raw_rel)
                mirror, incr = usage.get(logical, (None, None))
                usage[logical] = (size, incr)

        return usage

    def _disk_usage_job(self):
        if not self._lock.acquire(blocking=False):
            cherrypy.log('disk usage scan already running, skipping', context=CONTEXT)
            return

        try:
            self._run_disk_usage_scan()
        finally:
            self._lock.release()

    def _run_disk_usage_scan(self):
        cherrypy.log('starting disk usage scan', context=CONTEXT)

        if cherrypy.db.session is None:
            return

        cherrypy.db.clear_sessions()

        with cherrypy.db.session.begin():
            repos = RepoObject.query.all()
            cherrypy.db.session.expunge_all()

        success_count = 0
        fail_count = 0

        for repo_obj in repos:
            repo_path = repo_obj.full_path
            try:
                usage = self._collect_repo_usage(repo_obj)

                entries = [
                    (logical_path, mirror_size, increments_size)
                    for logical_path, (mirror_size, increments_size) in usage.items()
                ]

                with cherrypy.db.session.begin():
                    DiskUsage.replace_all_for_repo(repo_obj.id, entries)

                success_count += 1
                cherrypy.log(f'disk usage updated for repository {repo_path!r} ({len(entries)} paths)', context=CONTEXT)

            except Exception as e:
                fail_count += 1
                cherrypy.log(
                    f'failed to update disk usage for repository {repo_path!r}: {e}',
                    severity=logging.ERROR,
                    traceback=True,
                    context=CONTEXT,
                )
            finally:
                cherrypy.db.session.rollback()

        cherrypy.log(
            f'disk usage scan completed: {success_count} ok, {fail_count} failed',
            context=CONTEXT,
        )


cherrypy.disk_usage = DiskUsagePlugin(cherrypy.engine)
cherrypy.disk_usage.subscribe()

cherrypy.config.namespaces['disk_usage'] = lambda key, value: setattr(cherrypy.disk_usage, key, value)
