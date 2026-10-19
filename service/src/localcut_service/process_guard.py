"""Contain media child processes; Windows kills the tree if the service dies."""
import os
import signal
import time


class ProcessGuard:
    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "Cannot contain media process")
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "Cannot assign media process to its local job")

    def close(self):
        if self.handle:
            import ctypes
            from ctypes import wintypes as w
            class Accounting(ctypes.Structure):
                _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong), ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                            ("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]
            self.kernel.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + 2
            info = Accounting()
            while time.monotonic() < deadline:
                if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None) or info.active == 0:
                    break
                time.sleep(0.01)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
