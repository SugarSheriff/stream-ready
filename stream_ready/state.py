# SPDX-License-Identifier: GPL-3.0-or-later
"""Session state shared between operators, panels, and the upload worker thread.

bpy is not thread-safe, so the worker never touches it. It only writes here, under a
lock, and a bpy.app.timers callback on the main thread redraws the panel. Nothing here
is saved into the .blend, which is where the one-time viewer key has to stay out of.
"""

import threading
import time
from collections import deque

findings = []
preflight_ran = False


class PublishState:
    def __init__(self):
        self._lock = threading.Lock()
        self._lines = deque(maxlen=60)
        self.status = "idle"  # idle | running | ready | processing | rejected | failed | cancelled
        self.result = None
        self.done_bytes = 0
        self.total_bytes = 0
        self.cancel = threading.Event()

    @property
    def running(self):
        return self.status == "running"

    def start(self):
        with self._lock:
            self._lines.clear()
            self.status = "running"
            self.result = None
            self.done_bytes = self.total_bytes = 0
            self.cancel = threading.Event()

    def log(self, message):
        with self._lock:
            self._lines.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def progress(self, done, total):
        with self._lock:
            self.done_bytes, self.total_bytes = done, total

    def finish(self, status, result=None):
        with self._lock:
            self.status = status
            self.result = result

    def lines(self):
        with self._lock:
            return list(self._lines)


publish = PublishState()
