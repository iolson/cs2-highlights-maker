"""Process helpers built on psutil: find, kill and await Windows processes."""

from __future__ import annotations

import time

import psutil


def find_processes(name: str) -> list[psutil.Process]:
    procs = []
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name.lower():
                procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_process_running(name: str) -> bool:
    return len(find_processes(name)) > 0


def kill_processes(name: str) -> bool:
    """Terminate all processes with the given name. Returns True if any were killed."""
    procs = find_processes(name)
    for proc in procs:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(procs, timeout=10)
    return len(procs) > 0


def wait_for_process_start(name: str, timeout: float) -> psutil.Process | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        procs = find_processes(name)
        if procs:
            return procs[0]
        time.sleep(0.5)
    return None


def wait_for_process_exit(proc: psutil.Process, timeout: float) -> bool:
    """Wait for the process to exit. Returns True on clean exit, False on timeout."""
    try:
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.NoSuchProcess:
        return True
