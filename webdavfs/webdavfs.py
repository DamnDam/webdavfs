from contextlib import closing
import io
import os
import six
import threading
import logging

import webdav2.client as wc
import webdav2.exceptions as we
import webdav2.urn as wu

from fs import errors
from fs.base import FS
from fs.enums import ResourceType, Seek
from fs.info import Info
from fs.iotools import line_iterator
from fs.mode import Mode
from fs.path import abspath, normpath


log = logging.getLogger(__name__)

basics = frozenset(['name'])
details = frozenset(('type', 'accessed', 'modified', 'created',
                     'metadata_changed', 'size'))
access = frozenset(('permissions', 'user', 'uid', 'group', 'gid'))


class WebDAVFile(io.RawIOBase):

    def __init__(self, wdfs, path, mode):
        super(WebDAVFile, self).__init__()

        self.fs = wdfs
        self.path = path
        self.res = self.fs.get_resource(self.path)
        self.mode = mode
        self._mode = Mode(mode)
        self._lock = threading.RLock()
        self.data = self._get_file_data()

        self.pos = 0

        if 'a' in mode:
            self.pos = self._get_data_size()

    def _get_file_data(self):
        with self._lock:
            data = io.BytesIO()
            try:
                self.res.write_to(data)
                if 'a' not in self.mode:
                    data.seek(io.SEEK_SET)
            except we.RemoteResourceNotFound:
                data.write(b'')

            return data

    if six.PY2:
        def __length_hint__(self):
            return len(self.data.getvalue())
    else:
        def __length_hint__(self):
            return self.data.getbuffer().nbytes

    def __repr__(self):
        _repr = "WebDAVFile({!r}, {!r}, {!r})"
        return _repr.format(self.fs, self.path, self.mode)

    def close(self):
        if not self.closed:
            log.debug("closing")
            self.flush()
            super(WebDAVFile, self).close()
            self.data.close()

    def flush(self):
        if self._mode.writing:
            log.debug("flush")
            self.data.seek(io.SEEK_SET)
            self.res.read_from(self.data)

    def readline(self, size=-1):
        return next(line_iterator(self, None if size==-1 else size))

    def readable(self):
        return self._mode.reading

    def read(self, size=-1):
        if not self._mode.reading:
            raise IOError("File is not in read mode")
        self.pos = self.pos + size if size != -1 else self.__length_hint__()
        return self.data.read(size)

    def seekable(self):
        return True

    def seek(self, pos, whence=Seek.set):
        if whence == Seek.set:
            if pos < 0:
                raise ValueError('Negative seek position {}'.format(pos))
            self.pos = pos
        elif whence == Seek.current:
            self.pos = max(0, self.pos + pos)
        elif whence == Seek.end:
            if pos > 0:
                raise ValueError('Positive seek position {}'.format(pos))
            self.pos = max(0, self.__length_hint__() + pos)
        else:
            raise ValueError('invalid value for whence')

        self.data.seek(self.pos)
        return self.pos

    def tell(self):
        return self.pos

    def truncate(self, size=None):
        self.data.truncate(size)
        data_size = self.__length_hint__()
        if size and data_size < size:
            self.data.write(b'\0' * (size - data_size))
        return size or data_size

    def writable(self):
        return self._mode.writing

    def write(self, data):
        if not self._mode.writing:
            raise IOError("File is not in write mode")
        bytes_written = self.data.write(data)
        self.seek(bytes_written, Seek.current)
        return bytes_written



class WebDAVFS(FS):

    _meta = {
        'case_insensitive': False,
        'invalid_path_chars': '\0',
        'network': True,
        'read_only': False,
        'thread_safe': True,
        'unicode_paths': True,
        'virtual': False,
    }

    def __init__(self, url, credentials=None, root=None):
        self.url = url
        self.credentials = credentials
        self.root = root
        super(WebDAVFS, self).__init__()

        options = {
            'webdav_hostname': self.url,
            'webdav_login': self.credentials["login"],
            'webdav_password': self.credentials["password"],
            'root': self.root
        }
        self.client = wc.Client(options)

    def _create_resource(self, path):
        urn = wu.Urn(path)
        res = wc.Resource(self.client, urn)
        return res

    def get_resource(self, path):
        return self._create_resource(path)

    @staticmethod
    def _create_info_dict(info):
        info_dict = {
            'basic': {"is_dir": False},
            'details': {'type': int(ResourceType.file)},
            'access': {}
        }

        for key, val in six.iteritems(info):
            if key in basics:
                info_dict['basic'][key] = six.u(val)
            elif key in details:
                if key == 'size' and val:
                    val = int(val)
                elif val:
                    val = six.u(val)
                info_dict['details'][key] = val
            elif key in access:
                info_dict['access'][key] = six.u(val)
            else:
                info_dict['other'][key] = six.u(val)

        return info_dict

    def isdir(self, path):
        try:
            return self.client.is_dir(path)
        except we.RemoteResourceNotFound:
            return False

    def exists(self, path):
        return self.client.check(path)

    def getinfo(self, path, namespaces=None):
        _path = self.validatepath(path)
        namespaces = namespaces or ()

        if _path in '/':
            return Info({
                "basic":
                {
                    "name": "",
                    "is_dir": True
                },
                "details":
                {
                    "type": int(ResourceType.directory)
                }
            })

        try:
            info = self.client.info(path)
            info_dict = self._create_info_dict(info)
            if self.isdir(path):
                info_dict['basic']['is_dir'] = True
                info_dict['details']['type'] = ResourceType.directory
            return Info(info_dict)
        except we.RemoteResourceNotFound as exc:
            raise errors.ResourceNotFound(path, exc=exc)

    def listdir(self, path):
        self.check()
        _path = self.validatepath(path)

        if not self.getinfo(_path).is_dir:
            raise errors.DirectoryExpected(path)

        dir_list = self.client.list(_path)
        return map(six.u, dir_list) if six.PY2 else dir_list

    def makedir(self, path, permissions=None, recreate=False):
        self.validatepath(path)
        _path = abspath(normpath(path))

        if _path == '/':
            if not recreate:
                raise errors.DirectoryExists(path)

        elif not (recreate and self.isdir(path)):
            if self.exists(_path):
                raise errors.DirectoryExists(path)
            try:
                self.client.mkdir(_path)
            except we.RemoteParentNotFound as exc:
                raise errors.ResourceNotFound(path, exc=exc)

        return self.opendir(path)

    def openbin(self, path, mode='r', buffering=-1, **options):
        _mode = Mode(mode)
        _mode.validate_bin()
        self.validatepath(path)

        log.debug("openbin: %s, %s", path, mode)
        with self._lock:
            try:
                info = self.getinfo(path)
                log.debug("Info: %s", info)
            except errors.ResourceNotFound:
                if _mode.reading:
                    raise errors.ResourceNotFound(path)
            else:
                if info.is_dir:
                    raise errors.FileExpected(path)
            if _mode.exclusive:
                raise errors.FileExists(path)
        wdfile = WebDAVFile(self, abspath(normpath(path)), mode)
        return wdfile

    def remove(self, path):
        if not self.exists(path):
            raise errors.ResourceNotFound(path)

        if self.getinfo(path).is_dir:
            raise errors.FileExpected(path)

        self.client.clean(path)

    def removedir(self, path):
        if path == '/':
            raise errors.RemoveRootError

        if not self.exists(path):
            raise errors.ResourceNotFound(path)

        if not self.isdir(path):
            raise errors.DirectoryExpected(path)

        checklist = self.client.list(path)
        if checklist:
            raise errors.DirectoryNotEmpty(path)

        self.client.clean(path)

    def setbytes(self, path, contents):
        if not isinstance(contents, bytes):
            raise ValueError('contents must be bytes')
        _path = abspath(normpath(path))
        self.validatepath(path)
        bin_file = io.BytesIO(contents)
        with self._lock:
            resource = self._create_resource(_path)
            resource.read_from(bin_file)

    def setinfo(self, path, info):
        if not self.exists(path):
            raise errors.ResourceNotFound(path)

    def create(self, path, wipe=False):
        with self._lock:
            if not wipe and self.exists(path):
                return False
            with self.open(path, 'wb') as new_file:
                # log.debug("CREATE %s", new_file)
                new_file.truncate(0)
            return True

    def copy(self, src_path, dst_path, overwrite=False):
        with self._lock:
            if not overwrite and self.exists(dst_path):
                raise errors.DestinationExists(dst_path)
            try:
                self.client.copy(src_path, dst_path)
            except we.RemoteResourceNotFound:
                raise errors.ResourceNotFound(src_path)
            except we.RemoteParentNotFound:
                raise errors.ResourceNotFound(dst_path)

    def move(self, src_path, dst_path, overwrite=False):
        if not overwrite and self.exists(dst_path):
            raise errors.DestinationExists(dst_path)
        with self._lock:
            try:
                self.client.move(src_path, dst_path, overwrite=overwrite)
            except we.RemoteResourceNotFound:
                raise errors.ResourceNotFound(src_path)
            except we.RemoteParentNotFound:
                raise errors.ResourceNotFound(dst_path)
