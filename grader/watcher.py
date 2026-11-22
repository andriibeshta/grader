"""Compatibility shim: the LDR watcher lives in ``grader.adapters.ldr``."""
from .adapters.ldr import LdrWatcher, LDRClient

Watcher = LdrWatcher

__all__ = ["Watcher", "LdrWatcher", "LDRClient"]
