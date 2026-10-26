"""Minimal systemd sd_notify (no dependency)."""

import os
import socket


def notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def watchdog_interval() -> float:
    """Half the systemd WatchdogSec, in seconds; 0 if not enabled."""
    try:
        return int(os.environ.get("WATCHDOG_USEC", "0")) / 2e6
    except ValueError:
        return 0.0
