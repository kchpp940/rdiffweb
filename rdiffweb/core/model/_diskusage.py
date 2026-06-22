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

import os
from datetime import datetime, timezone

import cherrypy
import cherrypy_foundation.plugins.db  # noqa
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, LargeBinary, SmallInteger, String, event, select
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql.functions import func

from ._timestamp import Timestamp
from ._update import column_add, column_exists, table_exists

Base = cherrypy.db.base


def normalize_logical_path(path):
    """
    Normalize a logical path for consistent database storage.

    - Root path is canonicalized to b'.'.
    - Leading and trailing slashes are stripped.
    - Path separators and '.' components are normalized via os.path.normpath.
    - Returns bytes.
    """
    if path is None or path == b'' or path == '':
        return b'.'
    if isinstance(path, str):
        path = path.encode('utf-8', errors='surrogateescape')
    path = os.path.normpath(path).strip(b'/')
    if path == b'' or path == b'.':
        return b'.'
    return path


class RepoDiskUsageScan(Base):
    """
    Tracks a single disk usage scan batch for a repository.

    Each scan goes through:
    pending → collecting data → completed (success) / failed (error)

    DiskUsage rows are linked to a scan_id; only rows from the latest
    completed scan for a repo are considered active.
    """

    __tablename__ = 'repodiskusagescans'
    __table_args__ = (Index('repodiskusagescan_repoid_status_index', 'RepoID', 'Status'),)

    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'

    id = Column('DiskUsageScanID', Integer, primary_key=True)
    repoid = Column('RepoID', Integer, ForeignKey("repos.RepoID", ondelete="CASCADE"), nullable=False, index=True)
    repo = relationship('RepoObject', lazy=True)
    status = Column('Status', String(16), nullable=False, default=STATUS_PENDING, index=True)
    started_at = Column('StartedAt', Timestamp, nullable=False, default=lambda: datetime.now(tz=timezone.utc))
    completed_at = Column('CompletedAt', Timestamp, nullable=True)
    total_paths = Column('TotalPaths', Integer, nullable=True)
    error_message = Column('ErrorMessage', LargeBinary(length=8192), nullable=True)

    disk_usages = relationship('DiskUsage', back_populates='scan', cascade='all, delete-orphan', lazy=True)

    def mark_completed(self, total_paths):
        self.status = self.STATUS_COMPLETED
        self.completed_at = datetime.now(tz=timezone.utc)
        self.total_paths = total_paths
        self.error_message = None

    def mark_failed(self, error_message):
        self.status = self.STATUS_FAILED
        self.completed_at = datetime.now(tz=timezone.utc)
        if isinstance(error_message, str):
            error_message = error_message.encode('utf-8', errors='surrogateescape')
        self.error_message = error_message

    @classmethod
    def create_for_repo(cls, repoid):
        """Create a new pending scan record for the given repo."""
        scan = cls(repoid=repoid, status=cls.STATUS_PENDING)
        scan.add()
        return scan

    @classmethod
    def get_latest_completed(cls, repoid):
        """Return the latest completed scan for a repo, or None."""
        return (
            cls.query.filter(cls.repoid == repoid, cls.status == cls.STATUS_COMPLETED)
            .order_by(cls.id.desc())
            .first()
        )

    def __repr__(self):
        return f"RepoDiskUsageScan(id={self.id!r}, repoid={self.repoid!r}, status={self.status!r})"


class DiskUsage(Base):
    __tablename__ = 'diskusages'
    __table_args__ = (Index('diskusage_parentpath_index', 'RepoID', 'ParentPath'),)

    repoid = Column('RepoID', Integer, ForeignKey("repos.RepoID", ondelete="CASCADE"), nullable=False, primary_key=True)
    repo = relationship('RepoObject', lazy=True)
    logical_path = Column(
        'LogicalPath', LargeBinary(length=4096), nullable=False, server_default=None, primary_key=True
    )
    parent_path = Column('ParentPath', LargeBinary(length=4096), nullable=False, server_default=None)
    mirror_size = Column('MirrorSize', Integer, nullable=True, server_default=None)
    increments_size = Column('IncrementsSize', Integer, nullable=True, server_default=None)
    last_updated = Column('LastUpdated', Timestamp, nullable=False, default=func.now(), onupdate=func.now())
    scan_id = Column(
        'DiskUsageScanID',
        Integer,
        ForeignKey("repodiskusagescans.DiskUsageScanID", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    scan = relationship('RepoDiskUsageScan', back_populates='disk_usages')

    @validates('logical_path')
    def _validate_logical_path(self, key, value):
        value = normalize_logical_path(value)
        if not value or b'/' not in value:
            self.parent_path = b''
        else:
            self.parent_path = value[: value.rfind(b'/')]
        return value

    @classmethod
    def _active_scan_id_subquery(cls, repoid):
        """
        Return a scalar subquery that yields the ``scan_id`` considered
        "active" for ``repoid``, or ``None`` if all rows should be treated
        as active (legacy / migration-incomplete).

        A scan_id is active when it belongs to the latest completed scan.
        When no completed scan exists for the repo but there are legacy rows
        (scan_id IS NULL), the subquery returns NULL, which callers should
        interpret as "consider all rows for this repo as active".
        """
        from sqlalchemy import select as sa_select

        latest_completed_id = (
            sa_select(RepoDiskUsageScan.id)
            .where(
                RepoDiskUsageScan.repoid == repoid,
                RepoDiskUsageScan.status == RepoDiskUsageScan.STATUS_COMPLETED,
            )
            .order_by(RepoDiskUsageScan.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        return latest_completed_id

    @classmethod
    def active_query(cls, repoid):
        """
        Return a query object pre-filtered to only include "active" DiskUsage
        rows for ``repoid``.

        Active-row selection logic:
        1. If a completed scan exists for the repo: only rows whose
           ``scan_id`` matches the latest completed scan are active.
        2. If NO completed scan exists: every row for the repo is active
           (covers legacy data without scan_id, and migration failures).
        3. Rows from pending / failed scans are never visible.

        This method does NOT rely on cascade-delete to remove old data.
        """
        from sqlalchemy import and_, or_, select as sa_select

        latest_completed_id = (
            sa_select(RepoDiskUsageScan.id)
            .where(
                RepoDiskUsageScan.repoid == repoid,
                RepoDiskUsageScan.status == RepoDiskUsageScan.STATUS_COMPLETED,
            )
            .order_by(RepoDiskUsageScan.id.desc())
            .limit(1)
            .scalar_subquery()
        )

        has_any_completed = (
            sa_select(func.count())
            .where(
                RepoDiskUsageScan.repoid == repoid,
                RepoDiskUsageScan.status == RepoDiskUsageScan.STATUS_COMPLETED,
            )
            .scalar_subquery()
        )

        return cls.query.filter(
            cls.repoid == repoid,
            or_(
                # Case A: there are completed scans; only rows of latest completed are active
                and_(
                    has_any_completed > 0,
                    cls.scan_id == latest_completed_id,
                ),
                # Case B: no completed scans yet; show all rows including legacy (scan_id IS NULL)
                has_any_completed == 0,
            ),
        )

    @classmethod
    def replace_for_scan(cls, scan_id, repoid, entries):
        """
        Insert DiskUsage rows for a completed scan.

        ``entries`` is an iterable of ``(logical_path, mirror_size, increments_size)`` tuples.
        """
        for logical_path, mirror_size, increments_size in entries:
            cls(
                repoid=repoid,
                scan_id=scan_id,
                logical_path=normalize_logical_path(logical_path),
                mirror_size=mirror_size,
                increments_size=increments_size,
            ).add()

    @classmethod
    def remove_old_scans_for_repo(cls, repoid, keep_scan_id):
        """
        Explicitly delete old DiskUsage rows and RepoDiskUsageScan records
        for ``repoid``, keeping only those belonging to ``keep_scan_id``.

        Deletes DiskUsage rows first (independent of FK cascade) then the
        scan records, so this works correctly even when cascade is not
        enforced by the database.
        """
        from sqlalchemy import delete

        cherrypy.db.session.execute(
            delete(DiskUsage).where(
                DiskUsage.repoid == repoid,
                DiskUsage.scan_id != keep_scan_id,
                DiskUsage.scan_id.isnot(None),
            )
        )

        cherrypy.db.session.execute(
            delete(RepoDiskUsageScan).where(
                RepoDiskUsageScan.repoid == repoid,
                RepoDiskUsageScan.id != keep_scan_id,
            )
        )

    def __repr__(self):
        return f"DiskUsage({self.repoid!r}, {self.logical_path!r}, mirror_size={self.mirror_size!r}, increments_size={self.increments_size!r})"


@event.listens_for(Base.metadata, 'after_create')
def update_diskusage_schema(target, conn, **kw):
    """
    Schema migration for disk usage tables.

    Runs after all tables are created to handle incremental schema changes
    and data migrations for existing databases.
    """
    if not table_exists(conn, DiskUsage.__table__):
        return

    # Add scan_id column if missing (for existing databases)
    if not column_exists(conn, DiskUsage.scan_id):
        column_add(conn, DiskUsage.scan_id)

    # Migrate legacy rows (without scan_id) by creating a synthetic scan record
    if column_exists(conn, DiskUsage.scan_id):
        legacy_count = conn.execute(
            select(func.count()).where(DiskUsage.__table__.c.RepoID.isnot(None), DiskUsage.__table__.c.DiskUsageScanID.is_(None))
        ).scalar()
        if legacy_count and legacy_count > 0:
            # Get distinct repoids with legacy data
            legacy_repos = conn.execute(
                select(DiskUsage.__table__.c.RepoID)
                .where(DiskUsage.__table__.c.DiskUsageScanID.is_(None))
                .distinct()
            ).fetchall()

            now = datetime.now(tz=timezone.utc)
            for (repoid,) in legacy_repos:
                # Create a synthetic completed scan
                result = conn.execute(
                    RepoDiskUsageScan.__table__.insert().values(
                        RepoID=repoid,
                        Status=RepoDiskUsageScan.STATUS_COMPLETED,
                        StartedAt=now,
                        CompletedAt=now,
                        TotalPaths=conn.execute(
                            select(func.count())
                            .where(DiskUsage.__table__.c.RepoID == repoid)
                            .where(DiskUsage.__table__.c.DiskUsageScanID.is_(None))
                        ).scalar(),
                        ErrorMessage=None,
                    )
                )
                scan_id = result.inserted_primary_key[0]

                # Update legacy rows to point to the synthetic scan
                conn.execute(
                    DiskUsage.__table__.update()
                    .where(DiskUsage.__table__.c.RepoID == repoid)
                    .where(DiskUsage.__table__.c.DiskUsageScanID.is_(None))
                    .values(DiskUsageScanID=scan_id)
                )
