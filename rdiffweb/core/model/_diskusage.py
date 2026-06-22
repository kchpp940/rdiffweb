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

import cherrypy
import cherrypy_foundation.plugins.db  # noqa
from sqlalchemy import Column, ForeignKey, Index, Integer, LargeBinary
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql.functions import func

from ._timestamp import Timestamp

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

    @validates('logical_path')
    def _validate_logical_path(self, key, value):
        value = normalize_logical_path(value)
        if not value or b'/' not in value:
            self.parent_path = b''
        else:
            self.parent_path = value[: value.rfind(b'/')]
        return value

    @classmethod
    def replace_all_for_repo(cls, repoid, entries):
        """
        Atomically replace all DiskUsage rows for a repository.

        ``entries`` is an iterable of ``(logical_path, mirror_size, increments_size)`` tuples.

        This deletes all existing rows for the repo and inserts the new ones
        in a single operation. Caller must be within an active transaction.
        """
        cls.query.filter(cls.repoid == repoid).delete()
        for logical_path, mirror_size, increments_size in entries:
            cls(
                repoid=repoid,
                logical_path=normalize_logical_path(logical_path),
                mirror_size=mirror_size,
                increments_size=increments_size,
            ).add()

    def __repr__(self):
        return f"DiskUsage({self.repoid!r}, {self.logical_path!r}, mirror_size={self.mirror_size!r}, increments_size={self.increments_size!r})"
