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

from rdiffweb.core.model import DiskUsage, RepoDiskUsageScan, RepoObject
from rdiffweb.core.model._diskusage import normalize_logical_path

CONTEXT = 'DISKUSAGE'


class DiskUsagePlugin(SimplePlugin):
    """
    Periodically scan backup storage and update disk usage for each repository
    using the `du` command-line tool. The scan is run with `nice` and `ionice`
    when available to reduce its impact on system resources.

    Two-phase commit with scan tokens:
    1. Create a pending RepoDiskUsageScan record (transactional).
    2. Run `du` outside any transaction (may be slow).
    3. On success:
       a. Verify no newer scan has already completed for this repo.
       b. Insert all DiskUsage rows linked to the scan_id.
       c. Mark the scan as completed.
       d. Delete all older scans (and their DiskUsage rows) for this repo.
    4. On failure: mark the scan as failed; existing data remains untouched.

    Concurrency guarantees:
    - Multiple scans can be pending for the same repo.
    - Only the latest completed scan's data is retained.
    - A scan that completes after a newer one is discarded automatically.
    - Failed scans never modify or delete existing data.
    """

    execution_time = '02:00'

    nice_level = 19
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

    def _create_scan(self, repoid):
        """Create a pending scan record and return its id."""
        with cherrypy.db.session.begin():
            scan = RepoDiskUsageScan.create_for_repo(repoid)
            cherrypy.db.session.flush()
            scan_id = scan.id
        return scan_id

    def _mark_scan_failed(self, scan_id, error_message):
        """Mark a scan as failed; does not touch existing DiskUsage data."""
        try:
            with cherrypy.db.session.begin():
                scan = RepoDiskUsageScan.query.filter_by(id=scan_id).one()
                scan.mark_failed(error_message)
        except Exception:
            cherrypy.log(
                f'failed to mark scan {scan_id} as failed',
                severity=logging.ERROR,
                traceback=True,
                context=CONTEXT,
            )

    def _commit_scan(self, scan_id, repoid, usage):
        """
        Commit collected data for a scan atomically.

        Uses a row-level lock on the RepoObject row to guarantee that only
        one pending scan can commit at a time for a given repository.

        Within the lock:
        - Re-checks that no newer completed scan exists.
        - Inserts DiskUsage rows linked to this scan_id.
        - Marks this scan as completed.
        - Explicitly deletes data from older scans.

        Returns True if the scan was committed, False if it was superseded.
        """
        entries = [
            (logical_path, mirror_size, increments_size)
            for logical_path, (mirror_size, increments_size) in usage.items()
        ]

        with cherrypy.db.session.begin():
            # Acquire a row lock on the RepoObject.  This serialises all
            # commit-scan operations for the same repo, so two pending
            # scans cannot both observe the same "latest completed" state
            # and then race to write.  Works on SQLite (table-level) and
            # PostgreSQL (row-level).
            repo = (
                RepoObject.query.filter(RepoObject.id == repoid)
                .with_for_update()
                .one_or_none()
            )
            if repo is None:
                raise RuntimeError(f'repo {repoid} no longer exists during scan commit')

            # Under the lock, re-read the latest completed scan.
            scan = RepoDiskUsageScan.query.filter_by(id=scan_id).one()
            if scan.status != RepoDiskUsageScan.STATUS_PENDING:
                raise RuntimeError(f'scan {scan_id} is no longer pending (status={scan.status})')

            latest = RepoDiskUsageScan.get_latest_completed(repoid)
            if latest is not None and latest.id > scan_id:
                scan.mark_failed(f'superseded by newer scan {latest.id}')
                cherrypy.log(
                    f'scan {scan_id} superseded by newer scan {latest.id} for repo {repoid}',
                    context=CONTEXT,
                )
                return False

            DiskUsage.replace_for_scan(scan_id, repoid, entries)

            scan.mark_completed(len(entries))

            DiskUsage.remove_old_scans_for_repo(repoid, keep_scan_id=scan_id)

        return True

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
        superseded_count = 0

        for repo_obj in repos:
            repo_path = repo_obj.full_path
            scan_id = None
            try:
                scan_id = self._create_scan(repo_obj.id)
                cherrypy.log(
                    f'scan {scan_id} started for repository {repo_path!r}',
                    context=CONTEXT,
                )

                usage = self._collect_repo_usage(repo_obj)

                committed = self._commit_scan(scan_id, repo_obj.id, usage)
                if committed:
                    success_count += 1
                    cherrypy.log(
                        f'scan {scan_id} completed for repository {repo_path!r} ({len(usage)} paths)',
                        context=CONTEXT,
                    )
                else:
                    superseded_count += 1

            except Exception as e:
                fail_count += 1
                if scan_id is not None:
                    self._mark_scan_failed(scan_id, str(e))
                cherrypy.log(
                    f'scan {scan_id} failed for repository {repo_path!r}: {e}',
                    severity=logging.ERROR,
                    traceback=True,
                    context=CONTEXT,
                )
            finally:
                cherrypy.db.session.rollback()

        cherrypy.log(
            f'disk usage scan completed: {success_count} ok, {fail_count} failed, {superseded_count} superseded',
            context=CONTEXT,
        )


cherrypy.disk_usage = DiskUsagePlugin(cherrypy.engine)
cherrypy.disk_usage.subscribe()

cherrypy.config.namespaces['disk_usage'] = lambda key, value: setattr(cherrypy.disk_usage, key, value)
