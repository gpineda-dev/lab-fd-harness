"""
logger.py - Unified logging architecture for the harness runtime.
Supports :stdout, :stderr, or file targets, and text or jsonl serialization formats.
Consumes first-class domain models via .to_log() -> LogRecord.
"""
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Dict, Optional, TextIO

from harness.core.model import LogRecord


class HarnessLogger:
    """
    Central logging manager for supervisor engines and coprocessors.
    """

    def __init__(
        self,
        level: Optional[str] = None,
        target: str = ":stdout",
        format: str = "text",
        default_engine: str = "engine",
    ):
        self.level = level.lower() if level else None
        self.target = target
        self.format = format.lower()
        self.default_engine = default_engine

        self._file_handle: Optional[TextIO] = None
        if not target.startswith(":"):
            file_path = Path(target).resolve()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(file_path, "a", encoding="utf-8")

    @property
    def stream(self) -> TextIO:
        if self._file_handle is not None:
            return self._file_handle
        if self.target == ":stdout":
            return sys.stdout
        elif self.target == ":stderr":
            return sys.stderr
        return sys.stderr

    def is_enabled(self, subsystem: str) -> bool:
        """Returns True if the requested subsystem log level is active."""
        if not self.level:
            return False
        levels = [l.strip().lower() for l in self.level.split(",")]
        sub = subsystem.lower()
        if "all" in levels or sub in levels:
            return True
        if sub == "io" and "dlp" in levels:
            return True
        return False

    def log(self, obj: Any, engine: Optional[str] = None) -> None:
        """
        Logs a loggable domain object or LogRecord.
        Extracts LogRecord via obj.to_log() if available.
        """
        record: Optional[LogRecord] = None
        if isinstance(obj, LogRecord):
            record = obj
        elif hasattr(obj, "to_log") and callable(obj.to_log):
            record = obj.to_log()

        if record is None or not self.is_enabled(record.subsystem):
            return

        eng = engine or self.default_engine

        if self.format == "jsonl":
            now_iso = datetime.now().isoformat()
            data = {
                "ts": now_iso,
                "engine": eng,
                "action": record.action,
                "payload": record.payload,
            }
            line = json.dumps(data, ensure_ascii=False) + "\n"
        else:
            # text format
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            domain = record.action.get("domain", record.subsystem)
            act_name = record.action.get("name", "") if record.action else ""
            fd = record.action.get("fd") if record.action else None

            if act_name and fd is not None:
                tag = f"{domain}:{act_name:<5s} FD {fd}"
            elif act_name:
                tag = f"{domain}:{act_name:<5s}"
            else:
                tag = domain

            line = f"\033[90m# [HARNESS] {ts} [{eng}] [{tag}] {record.text}\033[0m\n"

        try:
            self.stream.write(line)
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def log_raw(
        self,
        subsystem: str,
        message: str,
        action: Optional[str] = None,
        fd: Optional[int] = None,
        engine: Optional[str] = None,
    ) -> None:
        """Helper to log arbitrary raw messages using the standard format."""
        action_dict: Dict[str, Any] = {"domain": subsystem}
        if action:
            action_dict["name"] = action.strip()
        if fd is not None:
            action_dict["fd"] = fd

        record = LogRecord(
            action=action_dict,
            payload={"msg": message},
            text=message,
        )
        self.log(record, engine=engine)

    def close(self) -> None:
        """Closes the underlying file handle if a file target was used."""
        if self._file_handle is not None and not self._file_handle.closed:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except OSError:
                pass
            self._file_handle = None
