"""
Storage back-ends for sc2rip.

* CascStorage   - reads StarCraft II's CASC storage directly through CascLib (ctypes).
* FolderStorage - reads an already-extracted copy of the game files (e.g. made with CascView),
                  for when CascLib is not available.

Both expose:
    files()      -> list of paths, '/'-separated, as they exist in the storage
    read(path)   -> bytes
"""
import os
import sys
import ctypes
import ctypes.util


class StorageError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# CascLib
# --------------------------------------------------------------------------------------

def _candidate_lib_names():
    if sys.platform.startswith('win'):
        return ['CascLib.dll', 'casc.dll', 'libcasc.dll']
    if sys.platform == 'darwin':
        return ['libcasc.dylib', 'libcasc.1.dylib', 'libCascLib.dylib']
    return ['libcasc.so', 'libcasc.so.1', 'libCascLib.so']


def find_casclib(explicit=None):
    """Locate a CascLib shared library. Returns a path or None."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(here, 'tools'), os.path.join(here, 'tools', 'casclib'), os.getcwd()):
        for n in _candidate_lib_names():
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    found = ctypes.util.find_library('casc') or ctypes.util.find_library('CascLib')
    return found


CASC_LOCALE_ALL = 0xFFFFFFFF
CASC_OPEN_BY_NAME = 0x00000000
FIND_DATA_BUFFER = 16384  # bigger than CASC_FIND_DATA on every platform; szFileName is at offset 0


class CascStorage:
    """Read-only access to a local CASC storage through CascLib."""

    def __init__(self, game_dir, lib_path=None):
        lib_path = find_casclib(lib_path)
        if not lib_path:
            raise StorageError(
                'CascLib shared library not found. Put casc.dll / libcasc.so next to sc2rip.py, '
                'pass --casclib PATH, or use --from-extracted with files extracted by CascView. '
                'See README.md ("Getting CascLib").')
        try:
            if sys.platform.startswith('win'):
                # make dependent DLLs next to it loadable
                if hasattr(os, 'add_dll_directory'):
                    os.add_dll_directory(os.path.dirname(os.path.abspath(lib_path)))
                self.lib = ctypes.WinDLL(lib_path)
            else:
                self.lib = ctypes.CDLL(lib_path)
        except OSError as e:
            raise StorageError(f'Could not load CascLib from {lib_path}: {e}')
        self.lib_path = lib_path
        L = self.lib
        H = ctypes.c_void_p
        L.CascOpenStorage.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(H)]
        L.CascOpenStorage.restype = ctypes.c_bool
        L.CascCloseStorage.argtypes = [H]
        L.CascCloseStorage.restype = ctypes.c_bool
        L.CascFindFirstFile.argtypes = [H, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_char_p]
        L.CascFindFirstFile.restype = H
        L.CascFindNextFile.argtypes = [H, ctypes.c_void_p]
        L.CascFindNextFile.restype = ctypes.c_bool
        L.CascFindClose.argtypes = [H]
        L.CascFindClose.restype = ctypes.c_bool
        L.CascOpenFile.argtypes = [H, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(H)]
        L.CascOpenFile.restype = ctypes.c_bool
        L.CascGetFileSize64.argtypes = [H, ctypes.POINTER(ctypes.c_uint64)]
        L.CascGetFileSize64.restype = ctypes.c_bool
        L.CascReadFile.argtypes = [H, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        L.CascReadFile.restype = ctypes.c_bool
        L.CascCloseFile.argtypes = [H]
        L.CascCloseFile.restype = ctypes.c_bool
        try:
            L.GetCascError.argtypes = []
            L.GetCascError.restype = ctypes.c_uint32
            self._err = L.GetCascError
        except AttributeError:
            self._err = lambda: ctypes.get_last_error() if sys.platform.startswith('win') else -1

        game_dir = os.path.abspath(game_dir)
        if os.path.isdir(os.path.join(game_dir, 'SC2Data')) or os.path.isfile(os.path.join(game_dir, '.build.info')):
            params = game_dir
        else:
            raise StorageError(f'{game_dir} does not look like a StarCraft II install '
                               f'(expected a "SC2Data" folder and ".build.info" file inside it)')
        self.handle = H()
        # CascLib's default (non-UNICODE) build takes an ANSI path on Windows
        enc = 'mbcs' if sys.platform.startswith('win') else (sys.getfilesystemencoding() or 'utf-8')
        if not L.CascOpenStorage(params.encode(enc), CASC_LOCALE_ALL, ctypes.byref(self.handle)):
            raise StorageError(f'CascOpenStorage failed for {params} (CascLib error {self._err()}). '
                               f'Make sure the game is fully installed/updated and not being patched.')
        self._names = None

    def close(self):
        if getattr(self, 'handle', None):
            self.lib.CascCloseStorage(self.handle)
            self.handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def files(self):
        if self._names is not None:
            return self._names
        buf = ctypes.create_string_buffer(FIND_DATA_BUFFER)
        names = []
        h = self.lib.CascFindFirstFile(self.handle, b'*', buf, None)
        if not h:
            raise StorageError(f'CascFindFirstFile failed (CascLib error {self._err()})')
        try:
            while True:
                raw = buf.raw.split(b'\0', 1)[0]
                if raw:
                    names.append(raw.decode('utf-8', 'replace').replace('\\', '/'))
                if not self.lib.CascFindNextFile(h, buf):
                    break
        finally:
            self.lib.CascFindClose(h)
        self._names = names
        return names

    def read(self, path):
        fh = ctypes.c_void_p()
        name = path.encode('utf-8')
        if not self.lib.CascOpenFile(self.handle, name, CASC_LOCALE_ALL, CASC_OPEN_BY_NAME, ctypes.byref(fh)):
            # CascLib historically used backslashes for SC2 names - try both
            alt = path.replace('/', '\\').encode('utf-8')
            if not self.lib.CascOpenFile(self.handle, alt, CASC_LOCALE_ALL, CASC_OPEN_BY_NAME, ctypes.byref(fh)):
                raise StorageError(f'CascOpenFile failed for {path} (error {self._err()})')
        try:
            size = ctypes.c_uint64()
            if not self.lib.CascGetFileSize64(fh, ctypes.byref(size)):
                raise StorageError(f'CascGetFileSize64 failed for {path}')
            out = bytearray()
            chunk = 1 << 20
            buf = ctypes.create_string_buffer(chunk)
            got = ctypes.c_uint32()
            remaining = size.value
            while remaining > 0:
                want = min(chunk, remaining)
                if not self.lib.CascReadFile(fh, buf, want, ctypes.byref(got)):
                    raise StorageError(f'CascReadFile failed for {path} (error {self._err()}) - '
                                       f'the file may not be downloaded locally')
                if got.value == 0:
                    break
                out += buf.raw[:got.value]
                remaining -= got.value
            return bytes(out)
        finally:
            self.lib.CascCloseFile(fh)

    def describe(self):
        return f'CASC storage via {self.lib_path}'


# --------------------------------------------------------------------------------------
# Already-extracted files
# --------------------------------------------------------------------------------------

class FolderStorage:
    """A directory tree containing extracted game files, e.g. the output of CascView
    ("mods/...", "campaigns/..." folders). Paths are returned relative to the root."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise StorageError(f'{root} is not a directory')
        self._names = None

    def files(self):
        if self._names is None:
            names = []
            for d, _, fs in os.walk(self.root):
                rel = os.path.relpath(d, self.root)
                for f in fs:
                    p = f if rel == '.' else os.path.join(rel, f)
                    names.append(p.replace('\\', '/'))
            self._names = names
        return self._names

    def read(self, path):
        with open(os.path.join(self.root, *path.split('/')), 'rb') as f:
            return f.read()

    def close(self):
        pass

    def describe(self):
        return f'extracted files in {self.root}'


def open_storage(game_dir=None, extracted_dir=None, casclib=None):
    if extracted_dir:
        return FolderStorage(extracted_dir)
    if not game_dir:
        raise StorageError('Either --game or --from-extracted is required')
    return CascStorage(game_dir, casclib)
