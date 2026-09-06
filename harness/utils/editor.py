"""
editor.py - Terminal Line Editor with full cursor navigation and history.
Supports:
  - Arrow keys: Up/Down (history recall), Left/Right (in-line cursor movement)
  - Home / End (Ctrl-A, Ctrl-E, escape sequences)
  - Backspace and Delete (in-line deletion)
  - In-place character insertion
Zero external dependencies, standard library only.
"""
import os
import sys
from typing import List, Optional

try:
    import termios
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False


class TerminalLineEditor:
    """
    Lightweight line editor capturing arrow keys, cursor motion, and history on terminal stdin.
    Proxies complete, sanitized lines to the child process via out_fd (FD 4).
    """

    def __init__(self, out_fd: int):
        self.out_fd = out_fd
        self.history: List[str] = []
        self.history_index: int = 0
        self.buffer: List[str] = []
        self.cursor: int = 0
        self.current_prompt: str = ""
        self._esc_buf: str = ""
        self._orig_termios: Optional[List] = None

    def enable_raw_mode(self, fd: int):
        """Enables cbreak mode with echo disabled so editor controls screen updates."""
        if not HAS_TERMIOS or not sys.stdin.isatty():
            return
        try:
            self._orig_termios = termios.tcgetattr(fd)
            new_attr = termios.tcgetattr(fd)
            # Disable canonical mode and echo
            new_attr[3] = new_attr[3] & ~termios.ICANON & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, new_attr)
        except Exception:
            self._orig_termios = None

    def restore_mode(self, fd: int):
        """Restores original terminal settings upon exit."""
        if self._orig_termios is not None and HAS_TERMIOS:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, self._orig_termios)
            except Exception:
                pass
            self._orig_termios = None

    def set_prompt(self, prompt: str):
        """Updates the active prompt string for line redisplay."""
        if "\n" in prompt:
            prompt = prompt.split("\n")[-1]
        self.current_prompt = prompt

    def redisplay(self):
        """Clears line, prints prompt + buffer, and repositions cursor to absolute column."""
        line = "".join(self.buffer)
        col = len(self.current_prompt) + self.cursor + 1
        sys.stdout.write(f"\r\033[K{self.current_prompt}{line}\033[{col}G")
        sys.stdout.flush()

    def feed_bytes(self, data: bytes) -> bool:
        """Feeds a chunk of bytes from stdin."""
        text = data.decode("utf-8", errors="replace")
        for ch in text:
            self.feed_char(ch)
        return False

    def feed_char(self, char: str) -> bool:
        """
        Processes a single input character or escape sequence.
        Returns True if a line was submitted to child process.
        """
        # 1. Escape sequence parsing
        if self._esc_buf:
            self._esc_buf += char
            # Continue accumulating until terminal sequence finishes
            if self._esc_buf in ("\x1b[", "\x1bO", "\x1b[1", "\x1b[3", "\x1b[4"):
                return False

            seq = self._esc_buf
            self._esc_buf = ""

            # Up Arrow -> Recall older history
            if seq in ("\x1b[A", "\x1bOA"):
                if self.history and self.history_index > 0:
                    self.history_index -= 1
                    self.buffer = list(self.history[self.history_index])
                    self.cursor = len(self.buffer)
                    self.redisplay()
                elif self.history and self.history_index == len(self.history):
                    self.history_index = len(self.history) - 1
                    self.buffer = list(self.history[self.history_index])
                    self.cursor = len(self.buffer)
                    self.redisplay()
                return False

            # Down Arrow -> Recall newer history
            elif seq in ("\x1b[B", "\x1bOB"):
                if self.history and self.history_index < len(self.history) - 1:
                    self.history_index += 1
                    self.buffer = list(self.history[self.history_index])
                    self.cursor = len(self.buffer)
                    self.redisplay()
                elif self.history_index >= len(self.history) - 1:
                    self.history_index = len(self.history)
                    self.buffer = []
                    self.cursor = 0
                    self.redisplay()
                return False

            # Left Arrow -> Move cursor left
            elif seq in ("\x1b[D", "\x1bOD"):
                if self.cursor > 0:
                    self.cursor -= 1
                    self.redisplay()
                return False

            # Right Arrow -> Move cursor right
            elif seq in ("\x1b[C", "\x1bOC"):
                if self.cursor < len(self.buffer):
                    self.cursor += 1
                    self.redisplay()
                return False

            # Home key -> Move to beginning of line
            elif seq in ("\x1b[H", "\x1b[1~"):
                self.cursor = 0
                self.redisplay()
                return False

            # End key -> Move to end of line
            elif seq in ("\x1b[F", "\x1b[4~"):
                self.cursor = len(self.buffer)
                self.redisplay()
                return False

            # Delete key (Suppr) -> Delete character under cursor
            elif seq == "\x1b[3~":
                if self.cursor < len(self.buffer):
                    self.buffer.pop(self.cursor)
                    self.redisplay()
                return False

            return False

        if char == "\x1b":
            self._esc_buf = "\x1b"
            return False

        # 2. Enter / Return -> Submit line
        if char in ("\r", "\n"):
            line = "".join(self.buffer)
            sys.stdout.write("\n")
            sys.stdout.flush()

            if line.strip():
                self.history.append(line)
            self.history_index = len(self.history)
            self.buffer = []
            self.cursor = 0

            try:
                os.write(self.out_fd, (line + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            return True

        # 3. Backspace -> Erase character before cursor
        if char in ("\x7f", "\x08"):
            if self.cursor > 0:
                self.buffer.pop(self.cursor - 1)
                self.cursor -= 1
                self.redisplay()
            return False

        # 4. Ctrl-A -> Move cursor to start
        if char == "\x01":
            self.cursor = 0
            self.redisplay()
            return False

        # 5. Ctrl-E -> Move cursor to end
        if char == "\x05":
            self.cursor = len(self.buffer)
            self.redisplay()
            return False

        # 6. Ctrl-C -> KeyboardInterrupt
        if char == "\x03":
            raise KeyboardInterrupt()

        # 7. Ctrl-D -> EOF on empty buffer
        if char == "\x04":
            if not self.buffer:
                try:
                    os.close(self.out_fd)
                except OSError:
                    pass
                return True
            return False

        # 8. Standard character insertion at current cursor position
        if char >= " ":
            self.buffer.insert(self.cursor, char)
            self.cursor += 1
            self.redisplay()
            return False

        return False
