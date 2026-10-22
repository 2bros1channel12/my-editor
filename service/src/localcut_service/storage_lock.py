"""Serialize local destructive maintenance against job registration."""
from threading import RLock

STORAGE_LOCK = RLock()
