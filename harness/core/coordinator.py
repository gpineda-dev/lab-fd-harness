"""
coordinator.py - Multi-engine event coordinator and reactor loop.
Multiplexes file descriptors and schedulers across N concurrent child runtimes
using a single synchronous select.select reactor.
"""
import os
import select
import sys
from typing import Dict, List, Optional

from harness.core.bus import EventBus
from harness.core.engine import HarnessEngine


class HarnessCoordinator:
    """
    Coordinates execution of one or multiple HarnessEngines.
    Provides a unified event loop, an in-memory event bus, and shared memory.
    """

    def __init__(self, engines: Optional[List[HarnessEngine]] = None):
        self.bus = EventBus()
        self.shared_variables: Dict[str, float] = {}
        self.engines: List[HarnessEngine] = []

        if engines:
            for engine in engines:
                self.add_engine(engine)

    def add_engine(self, engine: HarnessEngine) -> None:
        """Enrolls an engine into the coordinator, sharing bus and variables if not already set."""
        if engine.bus is None:
            engine.bus = self.bus
        if not engine.shared_variables:
            engine.shared_variables = self.shared_variables
        self.engines.append(engine)

    def spawn(
        self,
        cmd: List[str],
        name: str = "",
        attach_stdin: bool = True,
        show_directives: bool = False,
    ) -> HarnessEngine:
        """Factory helper creating and registering a new HarnessEngine."""
        engine = HarnessEngine(
            child_cmd=cmd,
            name=name,
            attach_stdin=attach_stdin,
            show_directives=show_directives,
            bus=self.bus,
            shared_variables=self.shared_variables,
        )
        self.add_engine(engine)
        return engine

    def run(self) -> Dict[HarnessEngine, int]:
        """
        Runs the coordinator event loop until all child engines terminate.
        Returns a mapping from HarnessEngine to its exit return code.
        """
        if not self.engines:
            return {}

        # 1. Start all enrolled engines
        for engine in self.engines:
            engine.start()

        # 2. Check interactive stdin capabilities
        try:
            stdin_fileno = sys.stdin.fileno()
        except Exception:
            stdin_fileno = None

        stdin_active = stdin_fileno is not None
        is_tty = stdin_active and sys.stdin.isatty()

        # Active engine receiving stdin (defaults to the first engine configured with attach_stdin)
        active_stdin_engine: Optional[HarnessEngine] = None
        for engine in self.engines:
            if engine.attach_stdin:
                active_stdin_engine = engine
                break

        if is_tty and active_stdin_engine and active_stdin_engine.editor:
            active_stdin_engine.editor.enable_raw_mode(stdin_fileno)

        results: Dict[HarnessEngine, int] = {}

        try:
            while any(e.is_running() for e in self.engines):
                # A. Aggregate FDs from all alive engines
                fd_map: Dict[int, HarnessEngine] = {}
                select_inputs: List[int] = []

                running_engines = [e for e in self.engines if e.is_running()]
                if not running_engines:
                    break

                for engine in running_engines:
                    engine.flush_partial_prompt()
                    for fd in engine.get_poll_fds():
                        fd_map[fd] = engine
                        select_inputs.append(fd)

                # Monitor parent stdin only if the active engine is still running and listening
                if (
                    stdin_active
                    and active_stdin_engine
                    and active_stdin_engine.is_running()
                    and active_stdin_engine.user_w_open
                ):
                    select_inputs.append(stdin_fileno)

                if not select_inputs:
                    break

                # B. Compute minimal timeout across all active schedulers
                deadlines = [e.time_to_next() for e in running_engines]
                valid_deadlines = [d for d in deadlines if d is not None]
                min_timeout = min(valid_deadlines) if valid_deadlines else None

                # C. Single-threaded kernel multiplexing call
                rlist, _, _ = select.select(select_inputs, [], [], min_timeout)

                # D. Dispatch ready child FDs
                for fd in rlist:
                    if fd in fd_map:
                        engine = fd_map[fd]
                        if not engine.on_fd_ready(fd):
                            # Channel closed / EOF
                            pass

                # E. Handle user terminal input
                if stdin_active and stdin_fileno in rlist and active_stdin_engine and active_stdin_engine.user_w_open:
                    try:
                        data = os.read(stdin_fileno, 4096)
                        if data:
                            if is_tty and active_stdin_engine.editor:
                                active_stdin_engine.editor.feed_bytes(data)
                            elif active_stdin_engine.user_w:
                                os.write(active_stdin_engine.user_w, data)
                        else:
                            # EOF on stdin
                            stdin_active = False
                            active_stdin_engine.close_user_w()
                    except (OSError, ValueError):
                        stdin_active = False

                # F. Dispatch tick deadlines
                for engine in self.engines:
                    if engine.is_running():
                        engine.on_tick_timeout()

        except KeyboardInterrupt:
            # Terminate all running children on Ctrl-C
            for engine in self.engines:
                if engine.is_running():
                    engine.terminate()
        finally:
            if is_tty and active_stdin_engine and active_stdin_engine.editor and stdin_fileno is not None:
                active_stdin_engine.editor.restore_mode(stdin_fileno)

            for engine in self.engines:
                engine.drain_remaining()
                code = engine.stop()
                results[engine] = code

        return results
