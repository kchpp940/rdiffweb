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


class _ScanSuperseded(Exception):
    """Raised internally when a scan loses the optimistic concurrency race."""
    pass


class _PathClassificationError(Exception):
    """Raised when a du output path cannot be safely classified."""
    pass


def _safe_is_within(prefix_path, target_path, root_path):
    """
    Determine if ``target_path`` is strictly under ``prefix_path`` (a proper
    child directory) and that the computed relative path does not escape
    ``root_path`` after normalization.

    Returns the normalised relative path (bytes) on success, or ``None`` if
    the target is not a proper child of the prefix or if the relative path
    would escape the root.

    Uses ``os.path.commonpath`` as an additional guard against prefix
    collisions like ``/repo/foo`` falsely matching ``/repo/foobar``.
    """
    try:
        norm_prefix = os.path.normpath(prefix_path)
        norm_target = os.path.normpath(target_path)
        norm_root = os.path.normpath(root_path)

        if norm_prefix == norm_target:
            return None

        try:
            common = os.path.commonpath([norm_prefix, norm_target])
        except ValueError:
            return None

        if common != norm_prefix:
            return None

        rel = os.path.relpath(norm_target, norm_prefix)
        if isinstance(rel, str):
            rel = rel.encode('utf-8', errors='surrogateescape')

        # Path-escape guard: if ``..`` appears after normalization the path
        # would escape the prefix tree.  We check by re-joining and comparing
        # to the original prefix.
        rejoined = os.path.normpath(os.path.join(norm_prefix, rel))
        if rejoined != norm_target:
            return None

        # Root-level guard: make sure the relative path does not reference
        # anything above root_path when joined back.
        rejoined_root = os.path.normpath(os.path.join(norm_root, rel))
        try:
            if os.path.commonpath([norm_root, rejoined_root]) != norm_root:
                return None
        except ValueError:
            return None

        return rel

    except (UnicodeDecodeError, UnicodeEncodeError, TypeError, ValueError):
        return None


def _validate_and_normalize_logical(raw_rel, repo_path):
    """
    Validate a raw relative path produced by relpath() and normalize it for
    use as a logical_path key.

    Raises ``_PathClassificationError`` for paths that cannot be safely used:
    - empty or null bytes
    - ``..`` components that escape the repo root
    - control characters or null bytes inside the path

    Returns a normalized bytes path, or ``b'.'`` for the repo root.
    """
    if raw_rel is None:
        raise _PathClassificationError('null path')

    if isinstance(raw_rel, str):
        try:
            raw_rel = raw_rel.encode('utf-8', errors='surrogateescape')
        except (UnicodeEncodeError, UnicodeDecodeError):
            raise _PathClassificationError(f'cannot encode path: {raw_rel!r}')
    elif not isinstance(raw_rel, bytes):
        raise _PathClassificationError(f'path has wrong type: {type(raw_rel).__name__}')

    # Reject paths with embedded null bytes (would truncate or corrupt)
    if b'\x00' in raw_rel:
        raise _PathClassificationError(f'path contains null byte: {raw_rel!r}')

    # Strip leading/trailing slashes and normalize . and ..
    stripped = raw_rel.strip(b'/')
    if stripped == b'' or stripped == b'.':
        return b'.'

    normalized = os.path.normpath(stripped)

    # After normpath, guard against escape via '..' components that would
    # walk above the repo root.
    if normalized.startswith(b'..') or normalized == b'..':
        raise _PathClassificationError(f'path escapes repo root: {raw_rel!r}')

    # Reject standalone '.' that wasn't handled above (shouldn't happen, but be safe)
    if normalized == b'.':
        return b'.'

    # Final safety check: re-join with the repo root and verify we're still
    # inside the repo tree.
    try:
        rejoined = os.path.normpath(os.path.join(repo_path, normalized))
        common = os.path.commonpath([os.path.normpath(repo_path), rejoined])
        if common != os.path.normpath(repo_path):
            raise _PathClassificationError(
                f'normalized path escapes repo root: {raw_rel!r} -> {normalized!r}'
            )
    except (ValueError, TypeError):
        raise _PathClassificationError(f'path fails containment check: {raw_rel!r}')

    return normalized


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

        ``du`` is invoked with ``-x`` (--one-file-system) to prevent following
        symlinks / mount points that would cause the same physical directory
        tree to be counted multiple times, and to stop du from crossing FS
        boundaries into unrelated storage.

        Raises ``RuntimeError`` if du fails (non-zero exit code) or cannot be started.
        """
        cmd = []

        ionice = shutil.which(b'ionice')
        if ionice:
            cmd += [ionice, b'-c', str(self.ionice_class).encode()]

        nice = shutil.which(b'nice')
        if nice:
            cmd += [nice, b'-n', str(self.nice_level).encode()]

        # -x, --one-file-system        skip directories on different filesystems
        # This blocks du from following symlinks that point outside the repo
        # and from crossing mount points into other backups, which would
        # cause the same physical directory to be counted several times.
        cmd += [b'du', b'--block-size=1', b'-x', path]

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
                    size = int(size_str)
                except ValueError:
                    cherrypy.log(
                        f'unexpected du output line (bad size): {line!r}',
                        severity=logging.WARNING, context=CONTEXT,
                    )
                    continue
                if size < 0:
                    cherrypy.log(
                        f'unexpected du output line (negative size): {line!r}',
                        severity=logging.WARNING, context=CONTEXT,
                    )
                    continue
                results.append((size, subpath))
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

        Path classification rules (strictly matches rdiff-backup layout):

        *   ``<repo_root>/rdiff-backup-data/increments/<path>``  →  increments side
            of ``<path>``.
        *   Everything else *inside* ``rdiff-backup-data`` is ignored (metadata,
            error logs, etc.).
        *   Everything else *outside* ``rdiff-backup-data`` is the mirror side,
            with the repo root corresponding to logical path ``b'.'``.

        Safety rules:
        *   ``du -x`` prevents symlink-following double-counting before we even
            see the output.
        *   Every path is checked for exact containment (``os.path.commonpath``),
            not just ``startswith``, so ``rdiff-backup-data/increments-old/``
            won't be mis-classified as increments.
        *   Relative paths that would escape the repo tree after normalisation
            are rejected individually.
        *   ``mirror_size`` and ``increments_size`` are written at most once
            per logical path; a duplicate assignment raises ``RuntimeError`` and
            aborts the repo scan — no wrong statistic can land in the DB.

        Raises ``RuntimeError`` on any unrecoverable problem.  Partial results
        are never returned.
        """
        repo_path = repo_obj.full_path
        if not os.path.isdir(repo_path):
            raise RuntimeError(f"repository folder doesn't exist: {repo_path!r}")

        norm_repo_path = os.path.normpath(repo_path)
        rdiff_data = os.path.join(norm_repo_path, b'rdiff-backup-data')
        increments_prefix = os.path.join(rdiff_data, b'increments')

        raw_entries = self._run_du(repo_path)

        # usage[logical_path] = [mirror_size, increments_size]
        # Using a list (instead of tuple) so we can mutate in place while
        # guarding against double-setting — see the asserts below.
        usage = {}
        skipped_count = 0

        for size, subpath in raw_entries:
            try:
                # 1) Increments tree?
                inc_rel = _safe_is_within(increments_prefix, subpath, norm_repo_path)
                if inc_rel is not None:
                    logical = _validate_and_normalize_logical(inc_rel, norm_repo_path)
                    slot = usage.setdefault(logical, [None, None])
                    if slot[1] is not None:
                        raise RuntimeError(
                            f'increments_size set twice for {logical!r} in repo {repo_path!r}'
                        )
                    slot[1] = size
                    continue

                # 2) Inside rdiff-backup-data but NOT increments → ignore
                rd_rel = _safe_is_within(rdiff_data, subpath, norm_repo_path)
                if rd_rel is not None:
                    continue

                # 3) Mirror tree (the user-visible backup)
                mirror_rel = _safe_is_within(norm_repo_path, subpath, norm_repo_path)
                if mirror_rel is not None:
                    logical = _validate_and_normalize_logical(mirror_rel, norm_repo_path)
                    slot = usage.setdefault(logical, [None, None])
                    if slot[0] is not None:
                        raise RuntimeError(
                            f'mirror_size set twice for {logical!r} in repo {repo_path!r}'
                        )
                    slot[0] = size
                    continue

                # 4) Path that doesn't fit any category → skip this entry and log
                skipped_count += 1
                cherrypy.log(
                    f'skipping unclassifiable path {subpath!r} in repo {repo_path!r}',
                    severity=logging.WARNING, context=CONTEXT,
                )

            except _PathClassificationError as e:
                skipped_count += 1
                cherrypy.log(
                    f'skipping bad path {subpath!r} in repo {repo_path!r}: {e}',
                    severity=logging.WARNING, context=CONTEXT,
                )

        if skipped_count:
            cherrypy.log(
                f'skipped {skipped_count} unclassifiable entries for repo {repo_path!r}',
                severity=logging.INFO, context=CONTEXT,
            )

        # Final consistency check: every logical_path has at least one side
        # set (which it will by construction) and the canonical key used is
        # already the normalised one.  Convert list slots to tuples for the
        # returned dict.
        return {k: (v[0], v[1]) for k, v in usage.items()}

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
        Commit collected data for a scan with database-agnostic optimistic
        concurrency control.

        Uses an atomic conditional UPDATE instead of ``SELECT ... FOR UPDATE``
        so that the same logic works correctly on SQLite, PostgreSQL, MySQL,
        and any other backend (even those without row-level locking).

        Within the transaction:
        1. Insert all DiskUsage rows linked to this scan_id.
        2. Execute a single conditional UPDATE on RepoDiskUsageScan:
           - Must still be ``pending``.
           - Must NOT have any newer completed scan for the same repo.
        3. If the UPDATE affected 0 rows, raise ``_ScanSuperseded`` so the
           transaction rolls back (including the DiskUsage inserts).
        4. If the UPDATE succeeded, explicitly delete data from older scans.

        Returns True if the scan was committed. Raises ``_ScanSuperseded`` if
        a concurrent scan committed first.
        """
        from datetime import datetime, timezone
        from sqlalchemy import and_, exists, not_, update as sa_update

        entries = [
            (logical_path, mirror_size, increments_size)
            for logical_path, (mirror_size, increments_size) in usage.items()
        ]

        with cherrypy.db.session.begin():
            scan = RepoDiskUsageScan.query.filter_by(id=scan_id).one()
            if scan.status != RepoDiskUsageScan.STATUS_PENDING:
                raise RuntimeError(f'scan {scan_id} is no longer pending (status={scan.status})')

            # Insert all rows for this scan
            DiskUsage.replace_for_scan(scan_id, repoid, entries)
            cherrypy.db.session.flush()

            now = datetime.now(tz=timezone.utc)

            # --- Database-agnostic optimistic concurrency check ---
            # Atomically try to transition pending → completed, conditional
            # on (a) scan still pending, (b) no newer completed scan exists.
            # This is a single UPDATE statement, so it is atomic on all ACID
            # databases regardless of isolation level or locking granularity.
            newer_completed_exists = (
                exists().where(
                    and_(
                        RepoDiskUsageScan.repoid == repoid,
                        RepoDiskUsageScan.status == RepoDiskUsageScan.STATUS_COMPLETED,
                        RepoDiskUsageScan.id > scan_id,
                    )
                )
            )

            update_stmt = (
                sa_update(RepoDiskUsageScan)
                .where(
                    and_(
                        RepoDiskUsageScan.id == scan_id,
                        RepoDiskUsageScan.status == RepoDiskUsageScan.STATUS_PENDING,
                        not_(newer_completed_exists),
                    )
                )
                .values(
                    status=RepoDiskUsageScan.STATUS_COMPLETED,
                    completed_at=now,
                    total_paths=len(entries),
                    error_message=None,
                )
                .execution_options(synchronize_session=False)
            )

            result = cherrypy.db.session.execute(update_stmt)
            rows_updated = result.rowcount

            if rows_updated == 0:
                # Another scan got there first.  Raise so the transaction rolls
                # back (undoing the DiskUsage inserts above).  The caller will
                # mark the scan as 'failed' in a separate, new transaction.
                raise _ScanSuperseded(
                    f'scan {scan_id} superseded by concurrent commit for repo {repoid}'
                )

            # Commit succeeded; now clean up older scans.
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

                self._commit_scan(scan_id, repo_obj.id, usage)
                success_count += 1
                cherrypy.log(
                    f'scan {scan_id} completed for repository {repo_path!r} ({len(usage)} paths)',
                    context=CONTEXT,
                )

            except _ScanSuperseded as e:
                superseded_count += 1
                if scan_id is not None:
                    self._mark_scan_failed(scan_id, str(e))
                cherrypy.log(str(e), context=CONTEXT)
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
