# -*- coding: utf-8 -*-
#
# This file is part of xrootdfs
# Copyright (C) 2015 CERN.
#
# xrootdfs is free software; you can redistribute it and/or modify it under the
# terms of the Revised BSD License; see LICENSE file for more details.

"""PyFilesystem implementation of XRootD protocol."""

from __future__ import absolute_import, print_function, unicode_literals

import re
from datetime import datetime
from glob import fnmatch
from urlparse import parse_qs

from fs.base import FS
from fs.errors import DestinationExistsError, DirectoryNotEmptyError, \
    FSError, InvalidPathError, RemoteConnectionError, ResourceError, \
    ResourceInvalidError, ResourceNotFoundError, UnsupportedError
from fs.path import dirname, normpath, pathcombine, pathjoin
from XRootD.client import FileSystem
from XRootD.client.flags import AccessMode, DirListFlags, MkDirFlags, \
    QueryCode, StatInfoFlags

from .utils import is_valid_path, is_valid_url, spliturl
from .xrdfile import XRootDFile


class XRootDFS(FS):

    """XRootD PyFilesystem interface."""

    _meta = {
        'thread_safe': True,
        'virtual': False,
        'read_only': False,
        'unicode_paths': True,
        'case_insensitive_paths': False,
        'network': True,
        'atomic.move': True,
        'atomic.copy': True,
        'atomic.makedir': True,
        'atomic.rename': True,
        'atomic.setcontents': False
    }

    def __init__(self, url, query=None, timeout=0):
        """Initialize file system object."""
        if not is_valid_url(url):
            raise InvalidPathError(path=url)

        root_url, base_path, queryargs = spliturl(url)

        if not is_valid_path(base_path):
            raise InvalidPathError(path=base_path)

        self.timeout = timeout
        self.root_url = root_url
        self.base_path = base_path
        self.query = queryargs or query
        self.client = FileSystem(root_url)
        super(XRootDFS, self).__init__(thread_synchronize=False)

    def _p(self, path):
        """Join path to base path."""
        # fs.path.pathjoin() omits the first '/' in self.base_path.
        # It is resolved by adding on an additional '/' to its return value.
        return '/' + pathjoin(self.base_path, path)

    def _raise_status(self, path, status):
        """Raise error based on status."""
        if status.errno == 3006:
            raise DestinationExistsError(path=path, details=status)
        elif status.errno == 3005:
            # Unfortunately only way to determine if the error is due to a
            # directory not being empty, or that a resource is not a directory:
            if status.message.endswith("not a directory\n"):
                raise ResourceInvalidError(path=path, details=status)
            else:
                raise DirectoryNotEmptyError(path=path, details=status)
        elif status.errno == 3011:
            raise ResourceNotFoundError(path=path, details=status)
        else:
            raise ResourceError(path=path, details=status)

    def _query(self, flag, arg, parse=True):
        """Query an xrootd server."""
        status, res = self.client.query(flag, arg, timeout=self.timeout)

        if not status.ok:
            if status.errno == 3013:
                raise UnsupportedError(opname="calcualte checksum",
                                       details=status)
            raise FSError(details=status)
        return parse_qs(res) if parse else res

    def open(self, path, mode='r', buffering=-1, encoding=None, errors=None,
             newline=None, line_buffering=False, **kwargs):
        """Open the given path as a file-like object.

        :param path: a path to file that should be opened
        :type path: string
        :param mode: mode of file to open, identical to the mode string used
            in 'file' and 'open' builtins
        :type mode: string
        :param kwargs: additional (optional) keyword parameters that may
            be required to open the file
        :type kwargs: dict

        :rtype: a file-like object

        :raises `fs.errors.ResourceInvalidError`: if an intermediate directory
            is an file
        :raises `fs.errors.ResourceNotFoundError`: if the path is not found
        """
        # Set default timeout if not overwritten.
        kwargs.setdefault("timeout", self.timeout)

        return XRootDFile(
            # Path must be full-on address with the server and everything.
            self.root_url + self._p(path),
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            line_buffering=line_buffering,
            **kwargs
        )

    def listdir(self,
                path="./",
                wildcard=None,
                full=False,
                absolute=False,
                dirs_only=False,
                files_only=False):
        """List the the files and directories under a given path.

        The directory contents are returned as a list of unicode paths.

        :param path: root of the path to list
        :type path: string
        :param wildcard: only returns paths that match this wildcard
        :type wildcard: string containing a wildcard, or a callable that
            accepts a path and returns a boolean
        :param full: returns full paths (relative to the root)
        :type full: bool
        :param absolute: returns absolute paths (paths beginning with /)
        :type absolute: bool
        :param dirs_only: if True, only return directories
        :type dirs_only: bool
        :param files_only: if True, only return files
        :type files_only: bool

        :rtype: iterable of paths

        :raises `fs.errors.ResourceInvalidError`: if the path exists, but is
            not a directory
        :raises `fs.errors.ResourceNotFoundError`: if the path is not found
        """
        return list(self.ilistdir(
            path=path, wildcard=wildcard, full=full, absolute=absolute,
            dirs_only=dirs_only, files_only=files_only
        ))

    def _stat_flags(self, path):
        """Get status of a path."""
        status, stat = self.client.stat(self._p(path))

        if not status.ok:
            raise self._raise_status(path, status)
        return stat.flags

    def isdir(self, path, _statobj=None):
        """Check if a path references a directory.

        :param path: a path in the filesystem
        :type path: string

        :rtype: bool

        """
        flags = self._stat_flags(path) if _statobj is None else _statobj.flags
        return bool(flags & StatInfoFlags.IS_DIR)

    def isfile(self, path, _statobj=None):
        """Check if a path references a file.

        :param path: a path in the filesystem
        :type path: string

        :rtype: bool

        """
        flags = self._stat_flags(path) if _statobj is None else _statobj.flags
        return not bool(flags & (StatInfoFlags.IS_DIR | StatInfoFlags.OTHER))

    def exists(self, path):
        """Check if a path references a valid resource.

        :param path: A path in the filesystem.
        :type path: `string`
        :rtype: `bool`
        """
        status, stat = self.client.stat(self._p(path))
        return status.ok

    def makedir(self, path, recursive=False, allow_recreate=False):
        """Make a directory on the filesystem.

        :param path: path of directory
        :type path: string
        :param recursive: if True, any intermediate directories will also be
            created
        :type recursive: `bool`
        :param allow_recreate: if True, re-creating a directory wont be an
            error
        :type allow_create: `bool`

        :raises `fs.errors.DestinationExistsError`: if the path is already a
            existing, and allow_recreate is False
        :raises `fs.errors.ResourceInvalidError`: if a containing
            directory is missing and recursive is False or if a path is an
            existing file
        """
        flags = MkDirFlags.MAKEPATH if recursive else MkDirFlags.NONE
        mode = AccessMode.NONE

        status, res = self.client.mkdir(self._p(path), flags=flags, mode=mode)

        if not status.ok:
            if allow_recreate and status.errno == 3006:
                return True
            self._raise_status(path, status)
        return True

    def remove(self, path):
        """Remove a file from the filesystem.

        :param path: Path of the resource to remove
        :type path: string

        :raises `fs.errors.ResourceInvalidError`: if the path is a directory
        :raises `fs.errors.DirectoryNotEmptyError`: if the directory is not
            empty
        """
        status, res = self.client.rm(self._p(path))

        if not status.ok:
            self._raise_status(path, status)
        return True

    def removedir(self, path, recursive=False, force=False):
        """Remove a directory from the filesystem.

        :param path: path of the directory to remove.
        :type path: string
        :param recursive: Unsupported by XRootDFS implementation.
        :type recursive: bool
        :param force: if True, any directory contents will be removed
            (recursively). Note that this can be very expensive as the xrootd
            protocol does not support recursive deletes - i.e. the library
            will do a full recursive listing of the directory and send a
            network request per file/directory.
        :type force: bool

        :raises `fs.errors.DirectoryNotEmptyError`: if the directory is not
            empty and force is `False`.
        :raises `fs.errors.ResourceInvalidError`: if the path is not a
            directory.
        :raises `fs.errors.ResourceNotFoundError`: if the path does not exist.
        """
        if recursive:
            raise UnsupportedError("recursive parameter is not supported.")

        status, res = self.client.rmdir(self._p(path), timeout=self.timeout)

        if not status.ok:
            if force and status.errno == 3005:
                # xrootd does not support recursive removal so do we have to
                # do it ourselves.
                for d, filenames in self.walk(path, search="depth"):
                    for filename in filenames:
                        relpath = pathjoin(d, filename)
                        status, res = self.client.rm(
                            self._p(relpath), timeout=self.timeout)
                        if not status.ok:
                            self._raise_status(relpath, status)
                    status, res = self.client.rmdir(
                        self._p(d), timeout=self.timeout)
                    if not status.ok:
                        self._raise_status(path, status)
                return True
            self._raise_status(path, status)
        return True

    def rename(self, src, dst):
        """Rename a file or directory.

        :param src: path to rename.
        :type src: string
        :param dst: new name.
        :type dst: string

        :raises DestinationExistsError: if destination already exists.
        :raises ResourceNotFoundError: if source does not exists.
        """
        src = self._p(src)
        dst = self._p(pathjoin(dirname(src), dst))

        if not self.exists(src):
            raise ResourceNotFoundError(src)
        return self._move(src, dst, overwrite=False)

    def getinfo(self, path):
        """Return information for a path as a dictionary.

        The following values can be found in the info dictionary:

        * ``size`` - Number of bytes used to store the file or directory
        * ``created_time`` - A datetime object containing the time the
           resource was created.
        * ``modified_time`` - A datetime object containing the time the
           resource was modified.
        * ``accessed_time`` - A datetime object containing the time the
           resource was accessed.
        * ``offline`` - True if file/directory is offline.
        * ``writable`` - True if file/directory is writable.
        * ``readable`` - True if file/directory is readable.
        * ``executable`` - True if file/directory is executable.

        :param path: a path to retrieve information for.
        :type path: `string`
        :rtype: `dict`
        """
        fullpath = self._p(path)
        status, stat = self.client.stat(fullpath)

        if not status.ok:
            self._raise_status(path, status)

        info = dict()
        info['size'] = stat.size
        info['offline'] = bool(stat.flags & StatInfoFlags.OFFLINE)
        info['writable'] = bool(stat.flags & StatInfoFlags.IS_WRITABLE)
        info['readable'] = bool(stat.flags & StatInfoFlags.IS_READABLE)
        info['executable'] = bool(stat.flags & StatInfoFlags.X_BIT_SET)

        res = self._query(QueryCode.XATTR, fullpath)
        ct = res.get('oss.ct', [None])[0]
        mt = res.get('oss.mt', [None])[0]
        at = res.get('oss.at', [None])[0]

        if ct:
            info['created_time'] = datetime.fromtimestamp(int(ct))
        if mt:
            info['modified_time'] = datetime.fromtimestamp(int(mt))
        if at:
            info['accessed_time'] = datetime.fromtimestamp(int(at))
        return info

    def ilistdir(self,
                 path="./",
                 wildcard=None,
                 full=False,
                 absolute=False,
                 dirs_only=False,
                 files_only=False):
        """Generator yielding the files and directories under a given path.

        This method behaves identically to :py:meth:`fs.base.FS.listdir` but
        returns an generator instead of a list.  Depending on the filesystem
        this may be more efficient than calling :py:meth:`fs.base.FS.listdir`
        and iterating over the resulting list.
        """
        flag = DirListFlags.STAT if dirs_only or files_only else \
            DirListFlags.NONE

        full_path = self._p(path)
        status, entries = self.client.dirlist(
            full_path, flag, timeout=self.timeout)

        if not status.ok:
            self._raise_status(path, status)

        return self._ilistdir_helper(
            path, entries, wildcard=wildcard, full=full,
            absolute=absolute, dirs_only=dirs_only, files_only=files_only
        )

    def _ilistdir_helper(self, path, entries, wildcard=None, full=False,
                         absolute=False, dirs_only=False, files_only=False):
        """A helper method called by ilistdir method that applies filtering.

        Given the path to a directory and a list of the names of entries within
        that directory, this method applies the semantics of the ilistdir()
        keyword arguments. An appropriately modified and filtered list of
        directory entries is returned.
        """
        path = normpath(path)

        if dirs_only and files_only:
            raise ValueError("dirs_only and files_only cannot both be True")

        if wildcard is not None:
            if not callable(wildcard):
                wildcard_re = re.compile(fnmatch.translate(wildcard))

                def wildcard(fn):
                    return bool(wildcard_re.match(fn))

            entries = (p for p in entries if wildcard(p.name))

        if dirs_only:
            entries = (
                p for p in entries if self.isdir(p.name, _statobj=p.statinfo)
            )
        elif files_only:
            entries = (
                p for p in entries if self.isfile(p.name, _statobj=p.statinfo)
            )

        if full:
            entries = (pathcombine(path, p.name) for p in entries)
        elif absolute:
            path = self._p(path)
            entries = ((pathcombine(path, p.name)) for p in entries)
        else:
            entries = (p.name for p in entries)

        return entries

    def move(self, src, dst, overwrite=False, **kwargs):
        """Move a file from one location to another.

        :param src: source path
        :type src: string
        :param dst: destination path
        :type dst: string
        :param overwrite: When True the destination will be overwritten (if it
            exists), otherwise a DestinationExistsError will be thrown.
        :type overwrite: bool
        :raise `fs.errors.DestinationExistsError`: if destination exists and
            `overwrite` is False
        :raise `fs.errors.ResourceInvalidError`: if source is not a file.
        :raise `fs.errors.ResourceNotFoundError`: if source was not found.
        """
        src, dst = self._p(src), self._p(dst)

        # isdir/isfile throws an error if file/dir doesn't exists
        if not self.isfile(src):
            if self.isdir(src):
                raise ResourceInvalidError(
                    src, msg="Source is not a file: %(path)s")
        return self._move(src, dst, overwrite=overwrite)

    def movedir(self, src, dst, overwrite=False, **kwargs):
        """Move a directory from one location to another.

        :param src: source directory path
        :type src: string
        :param dst: destination directory path
        :type dst: string
        :param overwrite: When True the destination will be overwritten (if it
            exists), otherwise a DestinationExistsError will be thrown.
        :type overwrite: bool
        :raise `fs.errors.DestinationExistsError`: if destination exists and
            `overwrite` is `False`.
        :raise `fs.errors.ResourceInvalidError`: if source is not a directory.
        :raise `fs.errors.ResourceNotFoundError`: if source was not found.
        """
        src, dst = self._p(src), self._p(dst)

        # isdir/isfile throws an error if file/dir doesn't exists
        if not self.isdir(src):
            if self.isfile(src):
                raise ResourceInvalidError(
                    src, msg="Source is not a directory: %(path)s")
        return self._move(src, dst, overwrite=overwrite)

    def _move(self, src, dst, overwrite=False):
        """Move source to destination with support for overwriting destination.

        Used by ``XRootDFS.move()``, ``XRootDFS.movedir()`` and
        ``XRootDFS.rename()``.

        .. warning::

           It is the responsibility of the caller of this method to check that
           the source exists.

           If ``overwrite`` is ``True``, this method will first remove any
           destination directory/file if it exists, and then try to move the
           source. Hence, if the source doesn't exists, it will remove the
           destination and then fail.
        """
        if overwrite and self.exists(dst):
            if self.isfile(dst):
                self.remove(dst)
            elif self.isdir(dst):
                self.removedir(dst, force=True)

        status, dummy = self.client.mv(src, dst, timeout=self.timeout)

        if status.ok:
            return True

        self._raise_status(dst, status)

    #
    # XRootD specific methods.
    #
    def checksum(self, path, _statobj=None):
        """Get checksum of file from server (XRootD only).

        Specific to ``XRootdFS``. Note not all XRootD server supports the
        checksum operation (in particular the default local xrootd server).

        :param src: path to calculate checksum for.
        :type src: string
        :raise `fs.errors.UnsupportedError`: if server does not support
            checksum calculation.
        :raise `fs.errors.FSError`: if you try to get the checksum of e.g. a
            directory.
        """
        if not self.isfile(path, _statobj=_statobj):
            raise ResourceInvalidError("Path is not a file: %s" % path)

        value = self._query(QueryCode.CHECKSUM, self._p(path), parse=False)
        algorithm, value = value.strip().split(" ")
        if value[-1] == "\x00":
            value = value[:-1]
        return (algorithm, value)

    def ping(self):
        """Ping xrootd server (XRootD only).

        Specific to ``XRootdFS``.

        :raise `fs.errors.RemoteConnectionError`:
        """
        status, dummy = self.client.ping(timeout=self.timeout)

        if not status.ok:
            raise RemoteConnectionError(opname="ping", details=status)

        return True
