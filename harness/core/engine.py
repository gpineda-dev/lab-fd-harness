"""
engine.py - Autonomous execution runtime for a single supervised process.
Encapsulates process lifecycle, file descriptor plumbing, scheduler, and coprocessors.
"""
import os
import subprocess
from typing import Dict, List, Optional

from harness.coproc import CoprocessorContext, CoprocessorRegistry, coprocessor_registry
from harness.core.bus import EventBus
from harness.core.channel import FDChannel
from harness.core.codec import AnnotationCodec
from harness.core.model import Event, Instruction
from harness.core.router import CoprocessorRouter
from harness.core.scheduler import HeapScheduler
from harness.utils.editor import TerminalLineEditor


class HarnessEngine:
    """
    Manages a single supervised child process, its dedicated FD channels (0, 1, 3, 4),
    its scheduler, and domain coprocessors.
    """

    def __init__(
        self,
        child_cmd: List[str],
        name: str = "",
        strace_file: Optional[str] = None,
        show_directives: bool = False,
        shared_variables: Optional[Dict[str, float]] = None,
        bus: Optional[EventBus] = None,
        attach_stdin: bool = True,
        cwd: Optional[str] = None,
        scheduler: Optional[HeapScheduler] = None,
        registry: Optional[CoprocessorRegistry] = None,
        initial_instructions: Optional[List[Instruction]] = None,
        redirect_stderr: bool = False,
    ):
        self.child_cmd = child_cmd
        self.name = name or (child_cmd[0] if child_cmd else "engine")
        self.strace_file = strace_file
        self.show_directives = show_directives
        self.shared_variables = shared_variables if shared_variables is not None else {}
        self.bus = bus
        self.attach_stdin = attach_stdin
        self.cwd = cwd
        self.initial_instructions = initial_instructions or []
        self.redirect_stderr = redirect_stderr

        self.scheduler = scheduler or HeapScheduler()
        self.codec = AnnotationCodec()
        self.registry = registry or coprocessor_registry

        self.proc: Optional[subprocess.Popen] = None
        self.event_w: Optional[int] = None
        self.user_w: Optional[int] = None
        self.user_w_open: bool = False

        self.channel: Optional[FDChannel] = None
        self.editor: Optional[TerminalLineEditor] = None
        self.router: Optional[CoprocessorRouter] = None
        self.ctx: Optional[CoprocessorContext] = None

    def start(self) -> None:
        """Sets up pipes and spawns the supervised child process."""
        if self.proc is not None:
            return

        cmd = self.child_cmd
        if self.strace_file:
            cmd = ["strace", "-tt", "-T", "-f", "-o", self.strace_file] + cmd

        # Create dedicated event pipe for child FD 3 and user-input pipe for child FD 4
        event_r, event_w = os.pipe()
        user_r, user_w = os.pipe()

        self.event_w = event_w
        self.user_w = user_w
        self.user_w_open = True

        def preexec():
            os.dup2(event_r, 3)
            os.dup2(user_r, 4)

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if self.redirect_stderr else None,
                cwd=self.cwd,
                bufsize=0,    # Unbuffered raw bytes
                pass_fds=[3, 4, event_r, user_r],
                preexec_fn=preexec,
            )
        finally:
            # Parent closes the read ends; only child uses them
            os.close(event_r)
            os.close(user_r)

        # Wire communication callbacks
        def emit_event(ev: Event):
            if self.channel is not None:
                self.channel.send_event(ev)

        self.editor = TerminalLineEditor(out_fd=user_w)
        self.ctx = CoprocessorContext(
            scheduler=self.scheduler,
            emit_event=emit_event,
            variables=self.shared_variables,
            bus=self.bus,
        )
        self.router = CoprocessorRouter(self.registry, self.ctx)

        # Inject pre-configured instructions (e.g. from TOML or CLI flags)
        for inst in self.initial_instructions:
            self.router.handle_instruction(inst)

        self.channel = FDChannel(
            proc=self.proc,
            codec=self.codec,
            on_instruction_cb=self.router.handle_instruction,
            extra_fds={3: event_w},
            show_directives=self.show_directives,
            on_prompt_cb=self.editor.set_prompt,
            apply_filters=self.ctx.apply_stream_filters,
        )

    def get_poll_fds(self) -> List[int]:
        """Returns the file descriptors this engine needs to monitor for reads."""
        if self.channel is not None and self.is_running():
            return [self.channel.fileno()]
        return []

    def time_to_next(self) -> Optional[float]:
        """Returns seconds remaining until next scheduler deadline."""
        return self.scheduler.time_to_next()

    def flush_partial_prompt(self) -> None:
        """Flushes any pending partial prompt (e.g. 'calc> ') before I/O wait."""
        if self.channel is not None:
            self.channel.flush_partial_prompt()

    def on_fd_ready(self, fd: int) -> bool:
        """Handles I/O readiness on an engine-owned file descriptor."""
        if self.channel is not None and fd == self.channel.fileno():
            return self.channel.process_input()
        return True

    def on_tick_timeout(self) -> None:
        """Fires all due scheduler callbacks."""
        self.scheduler.pop_due_events()

    def is_running(self) -> bool:
        """Checks whether the child process is currently alive."""
        return self.proc is not None and self.proc.poll() is None

    def drain_remaining(self) -> None:
        """Drains any buffered child stdout bytes after process exit."""
        if self.channel is not None:
            import select
            while True:
                rlist, _, _ = select.select([self.channel.fileno()], [], [], 0.0)
                if not rlist or not self.channel.process_input():
                    break
            self.channel.flush_remaining()

    def close_user_w(self) -> None:
        """Closes child FD 4 input pipe (signals EOF to child reader)."""
        if self.user_w is not None and self.user_w_open:
            try:
                os.close(self.user_w)
            except OSError:
                pass
            self.user_w_open = False

    def terminate(self) -> None:
        """Sends SIGTERM to child process if alive."""
        if self.proc is not None and self.is_running():
            self.proc.terminate()

    def stop(self) -> int:
        """Waits for child exit and cleans up pipe resources."""
        ret = 0
        if self.proc is not None:
            self.proc.wait()
            ret = self.proc.returncode or 0

        self.close_user_w()

        if self.event_w is not None:
            try:
                os.close(self.event_w)
            except OSError:
                pass
            self.event_w = None

        return ret

    def run(self) -> int:
        """Convenience method to execute a single engine under a coordinator."""
        from harness.core.coordinator import HarnessCoordinator
        coordinator = HarnessCoordinator([self])
        results = coordinator.run()
        return results.get(self, 0)
