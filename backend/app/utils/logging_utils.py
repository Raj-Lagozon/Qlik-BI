"""Captures the pipeline's own print()-based progress output into a job's
log, instead of asking qlik_extract/llm_convert/pbip_build to know about
jobs or the web layer at all — they keep printing exactly as they do when
run from the CLI.

Only one job's stdout is captured at a time (`_PIPELINE_LOCK` in
pipeline_runner.py serializes runs), so a plain sys.stdout swap is safe
here without per-thread routing.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import contextmanager


class _LineRelay:
    """A writable stream that splits arbitrary print() calls back into
    whole lines and forwards each one to `on_line`, while still passing
    everything through to the real stdout so `uvicorn` console output
    is unaffected."""

    def __init__(self, real_stream, on_line: Callable[[str], None]):
        self._real_stream = real_stream
        self._on_line = on_line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._real_stream.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._on_line(line)
        return len(text)

    def flush(self) -> None:
        self._real_stream.flush()


@contextmanager
def capture_stdout_lines(on_line: Callable[[str], None]):
    original_stdout, original_stderr = sys.stdout, sys.stderr
    relay = _LineRelay(original_stdout, on_line)
    sys.stdout = relay
    sys.stderr = relay
    try:
        yield
    finally:
        if relay._buffer:
            on_line(relay._buffer)
        sys.stdout = original_stdout
        sys.stderr = original_stderr
