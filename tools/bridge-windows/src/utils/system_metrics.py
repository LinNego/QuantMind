"""系统资源采样（零新依赖）：Windows ctypes / Linux /proc 双实现。

桥 UI 每 5s 轮询一次；CPU 百分比取两次采样的差值，首次返回 0。
"""
from __future__ import annotations

import os
import platform
import shutil
import threading
import time


class SystemMetrics:
    def __init__(self, disk_path: str = ""):
        self._lock = threading.Lock()
        self._start = time.time()
        self._prev = None  # (wall, idle, total, proc_seconds)
        self._disk_path = disk_path or os.path.abspath(os.sep)

    def sample(self) -> dict:
        with self._lock:
            now = time.time()
            try:
                if os.name == "nt":
                    idle, total, proc = self._win_cpu_times()
                    mem = self._win_memory()
                else:
                    idle, total, proc = self._linux_cpu_times()
                    mem = self._linux_memory()
            except Exception:
                idle = total = proc = 0.0
                mem = {"total": 0, "available": 0, "rss": 0}
            cpu = 0.0
            pcpu = 0.0
            if self._prev is not None:
                pwall, pidle, ptotal, pproc = self._prev
                dt = max(now - pwall, 1e-6)
                dtot = max(total - ptotal, 1e-9)
                cpu = max(0.0, min(100.0, 100.0 * (1.0 - (idle - pidle) / dtot)))
                pcpu = max(0.0, min(100.0, 100.0 * (proc - pproc) / dt))
            self._prev = (now, idle, total, proc)
            try:
                disk = shutil.disk_usage(self._disk_path)
                disk_total, disk_free = disk.total, disk.free
            except OSError:
                disk_total = disk_free = 0
            try:
                rss = self._rss_bytes(mem)
            except Exception:
                rss = 0
            total_mem = int(mem.get("total") or 0)
            avail = int(mem.get("available") or 0)
            used = max(total_mem - avail, 0)
            return {
                "cpu_percent": round(cpu, 1),
                "process_cpu_percent": round(pcpu, 1),
                "cpu_count": os.cpu_count() or 1,
                "mem_total": total_mem,
                "mem_used": used,
                "mem_percent": round(100.0 * used / total_mem, 1) if total_mem else 0.0,
                "process_rss": rss,
                "disk_total": disk_total,
                "disk_free": disk_free,
                "disk_percent": round(100.0 * (disk_total - disk_free) / disk_total, 1)
                if disk_total else 0.0,
                "threads": threading.active_count(),
                "pid": os.getpid(),
                "uptime_s": int(now - self._start),
                "os": platform.platform(),
            }

    # ---- Windows ----

    @staticmethod
    def _win_cpu_times():
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        def _to_int(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        idle_v = _to_int(idle)
        total_v = _to_int(kernel) + _to_int(user)

        # 伪句柄 GetCurrentProcess() 在部分 API 上拿不到计数；用 OpenProcess 真实句柄
        real = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, os.getpid())
        handle = real or ctypes.windll.kernel32.GetCurrentProcess()
        creation, exit_, ktime, utime = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_),
            ctypes.byref(ktime), ctypes.byref(utime))
        proc_v = (_to_int(ktime) + _to_int(utime)) / 10_000_000.0
        if real:
            ctypes.windll.kernel32.CloseHandle(real)
        return idle_v, total_v, proc_v

    @staticmethod
    def _win_memory():
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        real = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, os.getpid())
        handle = real or ctypes.windll.kernel32.GetCurrentProcess()
        ok = False
        try:
            psapi = ctypes.WinDLL("psapi.dll")
            ok = bool(psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb))
        except Exception:
            ok = False
        if not ok:
            try:
                ok = bool(ctypes.windll.kernel32.K32GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb))
            except Exception:
                ok = False
        if real:
            ctypes.windll.kernel32.CloseHandle(real)
        return {
            "total": int(status.ullTotalPhys),
            "available": int(status.ullAvailPhys),
            "rss": int(counters.WorkingSetSize) if ok else 0,
        }

    @staticmethod
    def _rss_bytes(mem: dict) -> int:
        return int(mem.get("rss") or 0)

    # ---- Linux ----

    @staticmethod
    def _linux_cpu_times():
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        with open("/proc/self/stat", encoding="utf-8") as f:
            fields = f.read().split()
        utime = int(fields[13])
        stime = int(fields[14])
        hz = os.sysconf("SC_CLK_TCK") or 100
        return float(idle), float(total), (utime + stime) / hz

    @staticmethod
    def _linux_memory():
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024
        rss = 0
        try:
            with open("/proc/self/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass
        return {
            "total": info.get("MemTotal", 0),
            "available": info.get("MemAvailable", 0),
            "rss": rss,
        }
