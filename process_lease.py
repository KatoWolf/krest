"""Windows lifetime containment for cancellable download workers only."""

import os


class DownloadLease:
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [
                ("process_time", c.c_int64),
                ("job_time", c.c_int64),
                ("flags", w.DWORD),
                ("minimum", c.c_size_t),
                ("maximum", c.c_size_t),
                ("processes", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Extended(c.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", c.c_uint64 * 6),
                ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t),
                ("peak_job", c.c_size_t),
            ]

        api = self.api = c.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        api.SetInformationJobObject.restype = w.BOOL
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.AssignProcessToJobObject.restype = w.BOOL
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not api.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
                raise c.WinError(c.get_last_error())
            if not api.AssignProcessToJobObject(self.handle, int(process._handle)):
                if process.poll() is None:
                    raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
