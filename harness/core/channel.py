"""
channel.py - Transport Channels for the harness.
FDChannel connects a subprocess stdout/stdin pipe using a Codec,
and routes events to default stdout/stdin or designated extra FDs (e.g. FD 3).
"""
from datetime import datetime
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

from harness.core.codec import AnnotationCodec
from harness.core.logger import HarnessLogger
from harness.core.model import Event, FilterMask, IORead, IOWrite, Instruction, LogMessage
from harness.utils.pratt import interpolate_template


def format_harness_log(
    engine_name: str,
    subsystem: str,
    message: str,
    action: Optional[str] = None,
    fd: Optional[int] = None,
) -> str:
    """Standardized supervisor log line formatter matching # [HARNESS] specification."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    if action is not None and fd is not None:
        tag = f"{subsystem}:{action:<5s} FD {fd}"
    elif action is not None:
        tag = f"{subsystem}:{action:<5s}"
    else:
        tag = subsystem
    return f"\033[90m# [HARNESS] {ts} [{engine_name}] [{tag}] {message}\033[0m\n"


class FDChannel:
    """I/O Transport connecting subprocess stdout and stdin pipes, plus extra FDs."""

    def __init__(
        self,
        proc: subprocess.Popen,
        codec: AnnotationCodec,
        on_instruction_cb: Callable[[Instruction], None],
        extra_fds: Optional[Dict[int, int]] = None,
        show_directives: bool = False,
        engine_name: str = "engine",
        log_level: Optional[str] = None,
        logger: Optional[HarnessLogger] = None,
        on_prompt_cb: Optional[Callable[[str], None]] = None,
        apply_filters: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.proc = proc
        self.codec = codec
        self.on_instruction = on_instruction_cb
        self.extra_fds = extra_fds or {}
        self.show_directives = show_directives
        self.engine_name = engine_name
        self.log_level = (log_level or ("all" if show_directives else None))
        if self.log_level:
            self.log_level = self.log_level.lower()
        self.logger = logger or HarnessLogger(level=self.log_level, default_engine=self.engine_name)
        self.on_prompt = on_prompt_cb
        self._apply_filters = apply_filters
        self._read_buffer = ""
        self._filters: List[Tuple[re.Pattern, str]] = []

    def is_log_enabled(self, level: str) -> bool:
        """Returns True if the requested subsystem log level is active."""
        if not self.log_level:
            return False
        levels = [l.strip().lower() for l in self.log_level.split(",")]
        return level.lower() in levels or "all" in levels

    def fileno(self) -> int:
        assert self.proc.stdout is not None
        return self.proc.stdout.fileno()

    def add_filter(self, pattern: str, replacement: str):
        try:
            self._filters.append((re.compile(pattern), replacement))
        except re.error:
            pass

    def apply_filters(self, text: str) -> Optional[str]:
        out: Optional[str] = text
        if self._apply_filters is not None:
            out = self._apply_filters(text)
            if out is None:
                return None
        for pat, rep in self._filters:
            out = pat.sub(rep, out)
        return out

    def _handle_decoded_line(self, line: str):
        # Detach any in-band prompt that preceded a harness directive without newline
        if "# @harness." in line and not line.strip().startswith("# @harness."):
            prefix_part, harness_part = line.split("# @harness.", 1)
            prompt = self.apply_filters(prefix_part)
            if prompt is not None:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                if self.on_prompt:
                    self.on_prompt(prompt)
            line = "# @harness." + harness_part

        inst = self.codec.decode(line)
        if inst is not None and self.is_log_enabled("io"):
            self.logger.log(IORead(fd=1, msg=line.strip()), engine=self.engine_name)

        if isinstance(inst, LogMessage):
            rendered = interpolate_template(inst.text)
            filtered = self.apply_filters(rendered)
            if filtered is not None:
                sys.stdout.write(f"[{inst.level}] {filtered}\n")
                sys.stdout.flush()
        elif inst is not None:
            self.on_instruction(inst)
        else:
            # Child's normal stdout is sanitized before reaching user terminal
            filtered_line = self.apply_filters(line)
            if filtered_line is not None:
                sys.stdout.write(filtered_line)
                sys.stdout.flush()

    def flush_partial_prompt(self):
        """Flushes partial text (e.g. interactive prompt without newline) to user terminal."""
        if self._read_buffer and not self._read_buffer.startswith("# @harness."):
            prompt = self.apply_filters(self._read_buffer)
            if prompt is not None:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                if self.on_prompt:
                    self.on_prompt(prompt)
            self._read_buffer = ""

    def process_input(self) -> bool:
        """Reads available bytes directly from child stdout fd."""
        try:
            raw = os.read(self.fileno(), 4096)
        except (OSError, ValueError):
            return False

        if not raw:
            return False

        self._read_buffer += raw.decode("utf-8", errors="replace")

        while "\n" in self._read_buffer:
            line, self._read_buffer = self._read_buffer.split("\n", 1)
            line += "\n"
            self._handle_decoded_line(line)

        return True

    def flush_remaining(self):
        """Processes any partial line left in buffer upon process exit."""
        if self._read_buffer:
            line = self._read_buffer
            self._read_buffer = ""
            self._handle_decoded_line(line)

    def send_event(self, event: Event):
        """Encodes an event and writes to child process stdin or target FD."""
        msg = self.codec.encode(event)
        if not msg:
            return

        target_fd = getattr(event, "target_fd", 0)
        if self.is_log_enabled("io"):
            self.logger.log(IOWrite(fd=target_fd, msg=msg.strip()), engine=self.engine_name)

        out_fd = None
        if target_fd in self.extra_fds:
            out_fd = self.extra_fds[target_fd]
        elif self.proc.stdin and not self.proc.stdin.closed:
            out_fd = self.proc.stdin.fileno()

        if out_fd is not None:
            try:
                os.write(out_fd, msg.encode("utf-8"))
            except (BrokenPipeError, OSError, ValueError):
                pass


class MemoryChannel:
    """In-memory channel for deterministic testing."""

    def __init__(
        self,
        codec: AnnotationCodec,
        on_instruction_cb: Callable[[Instruction], None],
        apply_filters: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.codec = codec
        self.on_instruction = on_instruction_cb
        self._apply_filters = apply_filters
        self.sent_messages: List[str] = []
        self.normal_output: List[str] = []
        self._filters: List[Tuple[re.Pattern, str]] = []

    def add_filter(self, pattern: str, replacement: str):
        try:
            self._filters.append((re.compile(pattern), replacement))
        except re.error:
            pass

    def apply_filters(self, text: str) -> Optional[str]:
        out: Optional[str] = text
        if self._apply_filters is not None:
            out = self._apply_filters(text)
            if out is None:
                return None
        for pat, rep in self._filters:
            out = pat.sub(rep, out)
        return out

    def feed_line(self, line: str):
        inst = self.codec.decode(line)
        if isinstance(inst, LogMessage):
            rendered = interpolate_template(inst.text)
            filtered = self.apply_filters(rendered)
            if filtered is not None:
                self.normal_output.append(f"[{inst.level}] {filtered}")
        elif inst is not None:
            self.on_instruction(inst)
        else:
            filtered = self.apply_filters(line.strip())
            if filtered is not None:
                self.normal_output.append(filtered)

    def send_event(self, event: Event):
        msg = self.codec.encode(event)
        if msg:
            target_fd = getattr(event, "target_fd", 0)
            prefix = f"[FD {target_fd}] " if target_fd > 0 else ""
            self.sent_messages.append(prefix + msg.strip())

    def clear(self):
        self.sent_messages.clear()
        self.normal_output.clear()

