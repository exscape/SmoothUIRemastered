"""Desktop shortcuts (.lnk), made with the Windows shell's own interfaces (IShellLinkW, IPersistFile) through
ctypes: no PowerShell, no pywin32. Windows only.

COM objects are called through their tables of functions (vtables): each interface's methods in the order of
its declaration in the Windows SDK, after IUnknown's three. Pointers and handles are always passed as ctypes
objects (c_void_p, byref): a plain Python int would be passed as a 32-bit int.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from ..common import fail

FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2

# Vtable indexes: IUnknown, IShellLinkW, IPersistFile
QUERY_INTERFACE, RELEASE = 0, 2
SET_DESCRIPTION, SET_WORKING_DIRECTORY, SET_ICON_LOCATION, SET_PATH = 7, 9, 17, 20
SAVE = 6


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    guid = GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(guid))
    return guid


def _method(obj: ctypes.c_void_p, index: int, *argtypes, restype=None):
    """A COM object's method, by its vtable index, to call with the object first. Returns an HRESULT unless
    restype says otherwise: a failed one raises OSError. (ctypes.HRESULT only exists on Windows.)"""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype or ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])


def desktop_folder() -> Path:
    """The user's desktop folder (it may be redirected, e.g. into OneDrive: not always ~\\Desktop)"""
    path = ctypes.c_void_p()
    try:
        ctypes.oledll.shell32.SHGetKnownFolderPath(ctypes.byref(_guid(FOLDERID_DESKTOP)), 0, None, ctypes.byref(path))
        return Path(ctypes.wstring_at(path.value))
    except OSError as e:
        fail(f"the desktop folder isn't known: {e}")
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def create_shortcut(folder: Path, name: str, target: Path, icon: str | None, description: str) -> Path:
    """Write folder\\<name>.lnk (replacing one of that name), which starts target in target's folder. icon:
    "<file>,<index>", e.g. an exe's first icon; None: target's own. Returns the shortcut's path."""
    path = Path(folder) / f"{name}.lnk"
    # COM may already be set up on this thread (Tk's file dialogs use it): then it stays as it is
    initialized = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) >= 0
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
                                             ctypes.byref(_guid(IID_SHELL_LINK_W)), ctypes.byref(link))
        _method(link, SET_PATH, ctypes.c_wchar_p)(link, str(target))
        _method(link, SET_WORKING_DIRECTORY, ctypes.c_wchar_p)(link, str(Path(target).parent))
        _method(link, SET_DESCRIPTION, ctypes.c_wchar_p)(link, description)
        if icon:
            file, _, index = icon.rpartition(",")
            _method(link, SET_ICON_LOCATION, ctypes.c_wchar_p, ctypes.c_int)(link, file, int(index))
        _method(link, QUERY_INTERFACE, ctypes.c_void_p, ctypes.c_void_p)(
            link, ctypes.byref(_guid(IID_PERSIST_FILE)), ctypes.byref(persist))
        _method(persist, SAVE, ctypes.c_wchar_p, wintypes.BOOL)(persist, str(path), True)
    except OSError as e:
        fail(f"the shortcut {path} can't be made: {e}")
    finally:
        for obj in (persist, link):
            if obj.value:
                _method(obj, RELEASE, restype=ctypes.c_ulong)(obj)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()
    return path
