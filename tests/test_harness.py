"""
test_harness.py - Unit tests for HeapScheduler, AnnotationCodec, and TimerCoprocessor.
Runs deterministically with a virtual clock in less than 10 milliseconds.
"""
import unittest

from harness.coproc import (
    BaseCoprocessor,
    BiMapVault,
    CoprocessorContext,
    DlpCoprocessor,
    TimerCoprocessor,
    coprocessor_registry,
    register_coprocessor,
)
from harness.core import (
    AnnotationCodec,
    CoprocessorRouter,
    FilterMask,
    HeapScheduler,
    MemoryChannel,
)
from harness.core.model import TimeRequest, TimeResult


class VirtualClock:
    def __init__(self, initial_time: float = 1000.0):
        self.current_time = initial_time

    def now(self) -> float:
        return self.current_time

    def advance(self, seconds: float):
        self.current_time += seconds


class TestHarness(unittest.TestCase):
    def test_positional_telemetry_and_introspection(self):
        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Init two clocks
        channel.feed_line("# @harness.clock:init id=main interval=0.5 cycles=10 policy=skip")
        channel.feed_line("# @harness.clock:init id=heartbeat interval=2.0")

        # 2. Test clock:list
        channel.feed_line("# @harness.clock:list")
        self.assertEqual(len(channel.sent_messages), 1)
        # Expected positional: clock:list main,heartbeat 2
        parts = channel.sent_messages[0].split()
        self.assertEqual(parts, ["clock:list", "main,heartbeat", "2"])

        # 3. Start main clock and query status
        channel.feed_line("# @harness.clock:start id=main")
        channel.feed_line("# @harness.clock:status id=main")
        self.assertEqual(len(channel.sent_messages), 2)
        # Expected positional: clock:status main 0.500 0.000 0 10 running
        parts = channel.sent_messages[1].split()
        self.assertEqual(parts[0], "clock:status")
        self.assertEqual(parts[1], "main")
        self.assertEqual(parts[2], "0.500")
        self.assertEqual(parts[6], "running")

        # 4. Wait tick (positional)
        channel.feed_line("# @harness.clock:wait id=main")
        self.assertEqual(len(channel.sent_messages), 3)
        # Expected positional: tick main 0 0 0.000 100.0000 ok
        parts = channel.sent_messages[2].split()
        self.assertEqual(parts, ["tick", "main", "0", "0", "0.000", "100.0000", "ok"])

    def test_clock_update_phase_continuity(self):
        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Start clock at T=100.0 with interval 0.1s
        channel.feed_line("# @harness.clock:init id=c1 interval=0.1 cycles=5")
        channel.feed_line("# @harness.clock:start id=c1")

        # Cycle 0 tick immediately at T=100.0
        channel.feed_line("# @harness.clock:wait id=c1")
        self.assertEqual(channel.sent_messages[-1].split(), ["tick", "c1", "0", "0", "0.000", "100.0000", "ok"])

        # Cycle 1 scheduled at T=100.1
        channel.feed_line("# @harness.clock:wait id=c1")
        clock.advance(0.1)
        scheduler.pop_due_events()
        self.assertEqual(channel.sent_messages[-1].split(), ["tick", "c1", "1", "0", "0.000", "100.1000", "ok"])

        # Cycle 2 scheduled at T=100.2
        channel.feed_line("# @harness.clock:wait id=c1")
        clock.advance(0.1)
        scheduler.pop_due_events()
        self.assertEqual(channel.sent_messages[-1].split(), ["tick", "c1", "2", "0", "0.000", "100.2000", "ok"])

        # At T=100.24 (Bash did 40ms of work), we change interval to 0.5s
        clock.advance(0.04)
        channel.feed_line("# @harness.clock:update id=c1 interval=0.5")

        # Request cycle 3:
        # Phase continuity: Target(3) MUST be T_theor(2) + 0.5s = 100.2 + 0.5 = 100.7s
        # NOT 100.24 + 0.5 = 100.74s (which would leak Bash processing time into the grid)
        channel.feed_line("# @harness.clock:wait id=c1")
        self.assertAlmostEqual(scheduler.time_to_next(), 100.7 - 100.24)  # 0.46s remaining

        # Advance to exactly 100.7
        clock.advance(0.46)
        scheduler.pop_due_events()
        parts = channel.sent_messages[-1].split()
        self.assertEqual(parts, ["tick", "c1", "3", "0", "0.000", "100.7000", "ok"])

    def test_clock_overrun_skip_preserves_grid_and_telemetry(self):
        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Start clock with policy=skip, interval=1.0s
        channel.feed_line("# @harness.clock:init id=c1 interval=1.0 cycles=10 policy=skip")
        channel.feed_line("# @harness.clock:wait id=c1")
        self.assertEqual(channel.sent_messages[-1].split(), ["tick", "c1", "0", "0", "0.000", "100.0000", "ok"])

        # Cycle 1
        channel.feed_line("# @harness.clock:wait id=c1")
        clock.advance(1.0)
        scheduler.pop_due_events()
        self.assertEqual(channel.sent_messages[-1].split(), ["tick", "c1", "1", "0", "0.000", "101.0000", "ok"])

        # Simulate a 2.5s stall/spike after cycle 1 (T advances from 101.0 to 103.5)
        clock.advance(2.5)
        # Target for cycle 2 was 102.0. At 103.5, overrun!
        # policy=skip: skipped = (103.5 - 102.0) // 1.0 + 1 = 2
        # next_cycle = 2 + 2 = 4, next_target = 100.0 + 4*1.0 = 104.0
        channel.feed_line("# @harness.clock:wait id=c1")
        self.assertAlmostEqual(scheduler.time_to_next(), 0.5)  # 104.0 - 103.5
        clock.advance(0.5)
        scheduler.pop_due_events()
        parts = channel.sent_messages[-1].split()
        self.assertEqual(parts, ["tick", "c1", "4", "2", "0.000", "104.0000", "overrun"])

    def test_pratt_expressions_in_annotations(self):
        clock = VirtualClock(0.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Init clock with arithmetic expression: '1s / 60'
        channel.feed_line('# @harness.clock:init id=fps interval="1s / 60"')
        channel.feed_line("# @harness.clock:start id=fps")

        clock_obj = coprocessor._clocks["fps"]
        self.assertAlmostEqual(clock_obj.interval, 1.0 / 60.0)

        # 2. Dynamic update with compound units expression: '(100ms + 50ms) * 2' -> 300ms = 0.3s
        channel.feed_line('# @harness.clock:update id=fps interval="(100ms + 50ms) * 2"')
        self.assertAlmostEqual(clock_obj.interval, 0.3)

    def test_calc_and_sprint_coprocessor(self):
        clock = VirtualClock(0.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        ctx = CoprocessorContext(scheduler=scheduler, emit_event=emit_event)
        router = CoprocessorRouter(coprocessor_registry, ctx)
        channel = MemoryChannel(codec, router.handle_instruction)
        channel_holder.append(channel)

        # Test calc
        channel.feed_line('# @harness.calc expr="5 * 6"')
        self.assertEqual(channel.sent_messages[-1].strip(), "calc 30")

        # Test sprint with bracket interpolation
        channel.feed_line('# @harness.sprint text="Result=[2 ** 8] Latency=[100ms * 2]"')
        self.assertEqual(channel.sent_messages[-1].strip(), "sprint Result=256 Latency=0.2")

        # Test calc with variable storage and calc:list
        channel.feed_line('# @harness.calc expr="78 * 9" store="k"')
        self.assertEqual(channel.sent_messages[-1].strip(), "calc k=702")
        channel.feed_line('# @harness.calc expr="k % 10" store="l"')
        self.assertEqual(channel.sent_messages[-1].strip(), "calc l=2")

        channel.feed_line('# @harness.calc:list')
        self.assertEqual(channel.sent_messages[-1].strip(), "calc:vars k=702,l=2 2")

    def test_output_filtering_and_silencing(self):
        codec = AnnotationCodec()
        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        router = CoprocessorRouter(coprocessor_registry, ctx)
        channel = MemoryChannel(codec, router.handle_instruction, apply_filters=ctx.apply_stream_filters)

        # 1. Register a filter mask
        channel.feed_line('# @harness.filter:mask pattern="secret-[0-9]+" replacement="[MASKED]"')

        # 2. Feed an instruction line: MUST be intercepted and NOT appear in normal_output
        channel.feed_line('# @harness.clock:wait id=test')
        self.assertEqual(len(channel.normal_output), 0)

        # 3. Feed a normal stdout line containing a secret: MUST be redacted
        channel.feed_line('Connecting with token secret-998811 now...')
        self.assertEqual(channel.normal_output[-1], 'Connecting with token [MASKED] now...')

        # 4. Feed a supervisor log instruction: MUST be formatted with log level
        channel.feed_line('# @harness.log text="Calculation done: [5 * 4]" level="INFO"')
        self.assertEqual(channel.normal_output[-1], '[INFO] Calculation done: 20')

    def test_terminal_line_editor_history(self):
        import os
        from harness.utils import TerminalLineEditor

        r, w = os.pipe()
        try:
            editor = TerminalLineEditor(out_fd=w)
            editor.set_prompt("calc> ")

            # 1. Type "sprint Hello" and hit Enter (\r)
            for ch in "sprint Hello\r":
                editor.feed_char(ch)

            line1 = os.read(r, 1024).decode()
            self.assertEqual(line1, "sprint Hello\n")

            # 2. Press Up Arrow: \x1b [ A
            for ch in "\x1b[A":
                editor.feed_char(ch)

            # 3. Hit Enter (\r) to re-submit recalled line
            editor.feed_char("\r")
            line2 = os.read(r, 1024).decode()
            self.assertEqual(line2, "sprint Hello\n")
        finally:
            os.close(r)
            os.close(w)

    def test_segmented_grammars(self):
        from harness.utils import evaluate_temporal, evaluate_math, TemporalGrammar, MathGrammar

        # 1. TemporalGrammar evaluates physical time units
        self.assertAlmostEqual(evaluate_temporal("1s / 60"), 1.0 / 60.0)
        self.assertAlmostEqual(evaluate_temporal("100ms * 2 + 50ms"), 0.25)
        # Rejects math functions (security boundary)
        with self.assertRaises(ValueError):
            evaluate_temporal("sin(pi / 2)")

        # 2. MathGrammar evaluates functions, constants, and variables
        self.assertAlmostEqual(evaluate_math("sin(pi / 2)"), 1.0)
        self.assertAlmostEqual(evaluate_math("sqrt(64)"), 8.0)
        self.assertAlmostEqual(evaluate_math("var1 * 2", variables={"var1": 14.0}), 28.0)

    def test_editor_cursor_navigation(self):
        import os
        from harness.utils import TerminalLineEditor

        r, w = os.pipe()
        try:
            editor = TerminalLineEditor(out_fd=w)
            editor.set_prompt("calc> ")

            # Type '5 * 6'
            for ch in "5 * 6":
                editor.feed_char(ch)
            self.assertEqual(editor.cursor, 5)

            # Left arrow twice -> cursor at 3
            for ch in "\x1b[D\x1b[D":
                editor.feed_char(ch)
            self.assertEqual(editor.cursor, 3)

            # Insert '+ 2 '
            for ch in "+ 2 ":
                editor.feed_char(ch)
            self.assertEqual("".join(editor.buffer), "5 *+ 2  6")

            # Hit Enter
            editor.feed_char("\r")
            res = os.read(r, 1024).decode()
            self.assertEqual(res, "5 *+ 2  6\n")
        finally:
            os.close(r)
            os.close(w)

    def test_coprocessor_registry_and_router(self):
        from harness.coproc.registry import CoprocessorRegistry
        from harness.core import CalcRequest, Instruction

        local_registry = CoprocessorRegistry()

        class DummyInstruction(Instruction):
            pass

        @local_registry.register
        class CustomCoprocessor(BaseCoprocessor):
            handled_instructions = (DummyInstruction,)

            def __init__(self, ctx):
                super().__init__(ctx)
                self.received = []

            def handle_instruction(self, inst):
                self.received.append(inst)

        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        router = CoprocessorRouter(local_registry, ctx)

        dummy = DummyInstruction()
        router.handle_instruction(dummy)

        custom_coproc = router.get_coprocessor(CustomCoprocessor)
        self.assertIsNotNone(custom_coproc)
        self.assertEqual(custom_coproc.received, [dummy])

        # Test duplicate collision raises ValueError
        conflict_registry = CoprocessorRegistry()

        @conflict_registry.register
        class FirstCoprocessor(BaseCoprocessor):
            handled_instructions = (DummyInstruction,)

        @conflict_registry.register
        class SecondCoprocessor(BaseCoprocessor):
            handled_instructions = (DummyInstruction,)

        with self.assertRaises(ValueError):
            conflict_registry.build_handlers(ctx)

    def test_bus_coprocessor_pub_sub(self):
        from harness.core import EventBus
        from harness.coproc import CoprocessorContext, coprocessor_registry
        from harness.core import CoprocessorRouter

        bus = EventBus()
        events_received = []

        ctx_sub = CoprocessorContext(
            scheduler=HeapScheduler(),
            emit_event=lambda ev: events_received.append(ev),
            bus=bus,
        )
        router_sub = CoprocessorRouter(coprocessor_registry, ctx_sub)
        channel_sub = MemoryChannel(AnnotationCodec(), router_sub.handle_instruction)

        # 1. Subscriber registers interest in topic 'deploy'
        channel_sub.feed_line('# @harness.bus:subscribe topic="deploy"')

        # 2. Publisher emits event to 'deploy'
        ctx_pub = CoprocessorContext(
            scheduler=HeapScheduler(),
            emit_event=lambda ev: None,
            bus=bus,
        )
        router_pub = CoprocessorRouter(coprocessor_registry, ctx_pub)
        channel_pub = MemoryChannel(AnnotationCodec(), router_pub.handle_instruction)

        channel_pub.feed_line('# @harness.bus:emit topic="deploy" payload="status=success version=2.0"')

        self.assertEqual(len(events_received), 1)
        ev = events_received[0]
        self.assertEqual(ev.topic, "deploy")
        self.assertEqual(ev.payload, "status=success version=2.0")

    def test_multi_engine_coordinator_execution(self):
        import sys
        from harness.core import HarnessCoordinator

        coord = HarnessCoordinator()
        # Engine 1: Simple echo script
        e1 = coord.spawn([sys.executable, "-c", "import sys; print('engine 1 running')"], name="e1", attach_stdin=False)
        # Engine 2: Another simple script
        e2 = coord.spawn([sys.executable, "-c", "import sys; print('engine 2 running')"], name="e2", attach_stdin=False)

        results = coord.run()
        self.assertEqual(results[e1], 0)
        self.assertEqual(results[e2], 0)

    def test_toml_coordinator_config_loading(self):
        import tempfile
        from harness.config import load_coordinator_from_toml

        toml_content = """
        [coordinator]
        show_directives = true

        [[engines]]
        name = "worker1"
        command = ["python3", "-c", "print('hello from worker1')"]
        attach_stdin = false

        [[engines]]
        name = "worker2"
        command = "python3 -c 'print(\\"hello from worker2\\")'"
        attach_stdin = false
        """
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_content)
            temp_path = f.name

        try:
            coordinator = load_coordinator_from_toml(temp_path)
            self.assertEqual(len(coordinator.engines), 2)
            self.assertEqual(coordinator.engines[0].name, "worker1")
            self.assertEqual(coordinator.engines[0].child_cmd, ["python3", "-c", "print('hello from worker1')"])
            self.assertTrue(coordinator.engines[0].show_directives)

            self.assertEqual(coordinator.engines[1].name, "worker2")
            self.assertEqual(coordinator.engines[1].child_cmd, ["python3", "-c", 'print("hello from worker2")'])

            results = coordinator.run()
            self.assertEqual(results[coordinator.engines[0]], 0)
            self.assertEqual(results[coordinator.engines[1]], 0)
        finally:
            import os
            os.unlink(temp_path)

        # Test dictionary table syntax [engines.<name>]
        toml_dict = """
        [engines.app1]
        command = ["python3", "-c", "print('app1')"]

        [engines.app2]
        command = "python3 -c 'print(\\"app2\\")'"
        """
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_dict)
            temp_dict_path = f.name

        try:
            coordinator = load_coordinator_from_toml(temp_dict_path)
            self.assertEqual(len(coordinator.engines), 2)
            names = {e.name for e in coordinator.engines}
            self.assertEqual(names, {"app1", "app2"})
            results = coordinator.run()
            for code in results.values():
                self.assertEqual(code, 0)
        finally:
            import os
            os.unlink(temp_dict_path)

    def test_stream_filter_pipeline_priority_and_silencing(self):
        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)

        # Filter A (priority 100): wraps in brackets
        ctx.attach_stdout_filter("bracket", lambda s: f"[{s}]", priority=100)
        # Filter B (priority 50): converts to uppercase
        ctx.attach_stdout_filter("upper", lambda s: s.upper(), priority=50)

        # Lower priority runs first: upper (50) then bracket (100)
        out = ctx.apply_stream_filters("hello")
        self.assertEqual(out, "[HELLO]")

        # Filter C (priority 10): silences lines starting with 'DROP'
        ctx.attach_stdout_filter("silence", lambda s: None if s.startswith("DROP") else s, priority=10)
        self.assertIsNone(ctx.apply_stream_filters("DROP this line"))
        self.assertEqual(ctx.apply_stream_filters("keep this"), "[KEEP THIS]")

        # Detach silence filter
        ctx.detach_stdout_filter("silence")
        self.assertEqual(ctx.apply_stream_filters("DROP this line"), "[DROP THIS LINE]")

    def test_dlp_coprocessor_audit_and_leak_counts(self):
        import io
        from harness.coproc.dlp import DlpCoprocessor
        from harness.core import FilterMask

        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        dlp = DlpCoprocessor(ctx, fail_on_leak=True, print_summary=True)
        dlp.handle_instruction(FilterMask(pattern=r"token-[0-9]+", replacement="[TOKEN]"))

        # Apply stream filter via ctx
        out1 = ctx.apply_stream_filters("Using token-1234 to login\n")
        self.assertEqual(out1, "Using [TOKEN] to login\n")
        self.assertEqual(dlp.total_leaks, 1)
        self.assertEqual(dlp.match_counts["token-[0-9]+"], 1)

        # Test audit summary output
        buf = io.StringIO()
        dlp.emit_audit_summary(target=buf)
        summary_text = buf.getvalue()
        self.assertIn("Security alert: 1 sensitive pattern(s) redacted", summary_text)
        self.assertIn("token-[0-9]+: 1 occurrence(s) masked", summary_text)
        self.assertIn("build failed due to secret leak", summary_text)

    def test_engine_initial_instructions_dlp_injection(self):
        import sys
        from harness.core import FilterMask, HarnessEngine

        mask_inst = FilterMask(pattern=r"MY_SECRET_[A-Z]+", replacement="[MASKED_SECRET]")
        script = "import sys; print('Output with MY_SECRET_KEY in stdout')"
        engine = HarnessEngine(
            child_cmd=[sys.executable, "-c", script],
            initial_instructions=[mask_inst],
            attach_stdin=False,
        )

        code = engine.run()
        self.assertEqual(code, 0)
        dlp = engine.router.get_coprocessor(DlpCoprocessor)
        self.assertIsNotNone(dlp)
        self.assertEqual(dlp.total_leaks, 1)


    def test_legacy_clock_push_ticks(self):
        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        channel.feed_line("# @harness.clock:every interval=0.2 cycles=3")
        # Immediately receives cycle 0
        self.assertEqual(len(channel.sent_messages), 1)
        self.assertTrue(channel.sent_messages[0].startswith("tick 0"))

        # Advance to cycle 1
        clock.advance(0.2)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 2)
        self.assertTrue(channel.sent_messages[1].startswith("tick 1"))

        # Advance to cycle 2
        clock.advance(0.2)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 3)
        self.assertTrue(channel.sent_messages[2].startswith("tick 2"))


    def test_cli_argument_normalization(self):
        from harness.cli import normalize_cli_args

        # 1. Standard run without options
        args1 = ["run", "./script.sh", "-s", "pure", "-v"]
        self.assertEqual(
            normalize_cli_args(args1),
            ["run", "--", "./script.sh", "-s", "pure", "-v"],
        )

        # 2. Run with --cmd
        args2 = ["run", "--strace", "trace.log", "--cmd", "./script.sh", "-v"]
        self.assertEqual(
            normalize_cli_args(args2),
            ["run", "--strace", "trace.log", "--", "./script.sh", "-v"],
        )

        # 3. Run with harness options preceding script without --cmd
        args3 = ["run", "--strace", "trace.log", "./script.sh", "-v"]
        self.assertEqual(
            normalize_cli_args(args3),
            ["run", "--strace", "trace.log", "--", "./script.sh", "-v"],
        )

        # 4. Run with harness flag preceding script
        args4 = ["run", "-v", "./script.sh", "-v"]
        self.assertEqual(
            normalize_cli_args(args4),
            ["run", "-v", "--", "./script.sh", "-v"],
        )

        # 5. Preserves existing --
        args5 = ["run", "--", "./script.sh", "-v"]
        self.assertEqual(normalize_cli_args(args5), args5)

        # 6. Preserves --help
        args6 = ["run", "--help"]
        self.assertEqual(normalize_cli_args(args6), args6)

        # 7. Run with --log-level preceding script
        args7 = ["run", "--log-level", "io", "./script.sh", "--foo"]
        self.assertEqual(
            normalize_cli_args(args7),
            ["run", "--log-level", "io", "--", "./script.sh", "--foo"],
        )

        # 8. dlp redact normalization
        args8 = ["dlp", "redact", "-r", "rules.toml", "cat", "input.txt"]
        self.assertEqual(
            normalize_cli_args(args8),
            ["dlp", "redact", "-r", "rules.toml", "--", "cat", "input.txt"],
        )

    def test_io_trace_log_format(self):
        import io
        import sys
        from unittest.mock import MagicMock
        from harness.core.channel import FDChannel
        from harness.core.model import ClockWaitResult

        mock_proc = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stdin.fileno.return_value = 11
        mock_proc.stdin.closed = False

        codec = AnnotationCodec()
        received_instructions = []

        channel = FDChannel(
            proc=mock_proc,
            codec=codec,
            on_instruction_cb=lambda inst: received_instructions.append(inst),
            engine_name="test-worker",
            log_level="io",
        )

        # Capture sys.stdout
        old_stdout = sys.stdout
        captured = io.StringIO()
        try:
            sys.stdout = captured
            channel._handle_decoded_line("# @harness.clock:wait id=main\n")
            tick_event = ClockWaitResult(clock_id="main", cycle=0, skipped=0, lag_ms=0.0, monotonic_ts=100.0, status="ok")
            channel.send_event(tick_event)
        finally:
            sys.stdout = old_stdout

        out = captured.getvalue()
        self.assertIn("# [HARNESS]", out)
        self.assertIn("[test-worker]", out)
        self.assertIn("[IO:READ  FD 1]", out)
        self.assertIn("# @harness.clock:wait id=main", out)
        self.assertIn("[IO:WRITE FD 0]", out)
        self.assertIn("tick main 0 0 0.000 100.0000 ok", out)

    def test_clock_grid_phase_alignment(self):
        from harness.coproc.timer import compute_align_delay

        # 1. Test delay computation
        self.assertAlmostEqual(compute_align_delay("*/1s", now_epoch=100.4), 0.6)
        self.assertAlmostEqual(compute_align_delay("*/100ms", now_epoch=100.042), 0.058, places=4)
        self.assertAlmostEqual(compute_align_delay("@second", now_epoch=10.2), 0.8)
        self.assertAlmostEqual(compute_align_delay("*/250ms", now_epoch=10.1), 0.15, places=4)

        # 2. Test grid alignment in pull synchronizer
        clock = VirtualClock(100.042)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event, epoch_fn=clock.now)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # Init clock aligned to 100ms grid: next boundary is 100.100 (delay = 0.058s)
        channel.feed_line('# @harness.clock:init id=grid interval=0.1 align="*/100ms" cycles=3')
        channel.feed_line('# @harness.clock:wait id=grid')

        # At T=100.042, Cycle 0 should NOT have fired synchronously because T0=100.100 is in the future
        self.assertEqual(len(channel.sent_messages), 0)

        # Advance to T=100.100 and trigger scheduler
        clock.advance(0.058)
        scheduler.pop_due_events()

        self.assertEqual(len(channel.sent_messages), 1)
        self.assertTrue(channel.sent_messages[-1].startswith("tick grid 0 0"))
        parts = channel.sent_messages[-1].strip().split()
        self.assertAlmostEqual(float(parts[5]), 100.1, places=2)

        # Cycle 1 wait
        channel.feed_line('# @harness.clock:wait id=grid')
        clock.advance(0.1)
        scheduler.pop_due_events()

        self.assertEqual(len(channel.sent_messages), 2)
        self.assertTrue(channel.sent_messages[-1].startswith("tick grid 1 0"))
        parts = channel.sent_messages[-1].strip().split()
        self.assertAlmostEqual(float(parts[5]), 100.2, places=2)

    def test_dlp_coprocessor_verbose_and_named_rules(self):
        from harness.coproc.dlp import DlpCoprocessor
        import io
        import sys

        ctx = CoprocessorContext(
            scheduler=HeapScheduler(),
            emit_event=lambda ev: None,
            engine_name="test-worker",
            log_level="dlp",
        )
        dlp = DlpCoprocessor(ctx, verbose=True, print_summary=True)
        dlp.add_rule(pattern=r"ghp_[0-9a-zA-Z]{10}", replacement="[GH_MASKED]", name="gh-token")
        dlp.add_rule(pattern=r"AKIA[0-9A-Z]{8}", replacement="[AWS_MASKED]", name="aws-token")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = stdout_buf, stderr_buf
            dlp.emit_startup_info()
            line1 = ctx.apply_stream_filters("Connecting with ghp_1234567890 securely\n")
            line2 = ctx.apply_stream_filters("All clean here\n")
            dlp.emit_audit_summary()
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        self.assertEqual(line1, "Connecting with [GH_MASKED] securely\n")
        self.assertEqual(line2, "All clean here\n")

        stdout_out = stdout_buf.getvalue()
        self.assertIn("# [HARNESS]", stdout_out)
        self.assertIn("[test-worker]", stdout_out)
        self.assertIn("[DLP:INIT ]", stdout_out)
        self.assertIn("gh-token", stdout_out)
        self.assertIn("[IO:READ  FD 1]", stdout_out)
        self.assertIn("ghp_1234567890", stdout_out)
        self.assertIn("[DLP:MUTATE]", stdout_out)
        self.assertIn("[DLP:AUDIT]", stdout_out)

        stderr_out = stderr_buf.getvalue()
        self.assertIn("[DLP AUDIT] Security report", stderr_out)
        self.assertIn("[VIOLATION] gh-token", stderr_out)
        self.assertIn("[CLEAN]     aws-token", stderr_out)

    def test_logger_text_and_jsonl_formatting(self):
        import json
        import tempfile
        from harness.core.logger import HarnessLogger
        from harness.core.model import ClockHold, ClockTick, IORead

        # 1. Text format to file
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            log_path = f.name

        logger_text = HarnessLogger(level="all", target=log_path, format="text", default_engine="bench.sh")
        logger_text.log(IORead(fd=1, msg="# @harness.clock:wait id=bench"))
        logger_text.log(ClockHold(id="bench", cycle=0, target=100.5, delay_ms=500.0, align="*/1s", phase_lock=True))
        logger_text.close()

        with open(log_path, "r", encoding="utf-8") as f:
            text_lines = f.readlines()

        self.assertEqual(len(text_lines), 2)
        self.assertIn("[bench.sh]", text_lines[0])
        self.assertIn("[IO:READ  FD 1]", text_lines[0])
        self.assertIn("# @harness.clock:wait id=bench", text_lines[0])
        self.assertIn("[CLOCK:HOLD ]", text_lines[1])
        self.assertIn("delay=500.0ms (grid phase lock)", text_lines[1])

        # 2. JSONL format to file
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            jsonl_path = f.name

        logger_jsonl = HarnessLogger(level="clock,io", target=jsonl_path, format="jsonl", default_engine="bench.sh")
        logger_jsonl.log(IORead(fd=1, msg="# @harness.clock:wait id=bench"))
        logger_jsonl.log(ClockHold(id="bench", cycle=0, target=100.5, delay_ms=500.0, align="*/1s", phase_lock=True))
        logger_jsonl.log(ClockTick(id="bench", cycle=0, lag_ms=0.25))
        logger_jsonl.close()

        with open(jsonl_path, "r", encoding="utf-8") as f:
            json_lines = [json.loads(line) for line in f]

        self.assertEqual(len(json_lines), 3)

        # Verify Record 1: IO:READ
        rec1 = json_lines[0]
        self.assertIn("ts", rec1)
        self.assertEqual(rec1["engine"], "bench.sh")
        self.assertEqual(rec1["action"], {"domain": "IO", "name": "READ", "fd": 1})
        self.assertEqual(rec1["payload"], {"msg": "# @harness.clock:wait id=bench"})

        # Verify Record 2: CLOCK:HOLD
        rec2 = json_lines[1]
        self.assertEqual(rec2["action"], {"domain": "CLOCK", "name": "HOLD"})
        self.assertEqual(rec2["payload"]["id"], "bench")
        self.assertEqual(rec2["payload"]["cycle"], 0)
        self.assertEqual(rec2["payload"]["target"], 100.5)
        self.assertEqual(rec2["payload"]["delay_ms"], 500.0)
        self.assertEqual(rec2["payload"]["grid"], {"align": "*/1s", "phase_lock": True})

        # Verify Record 3: CLOCK:TICK
        rec3 = json_lines[2]
        self.assertEqual(rec3["action"], {"domain": "CLOCK", "name": "TICK"})
        self.assertEqual(rec3["payload"]["id"], "bench")
        self.assertEqual(rec3["payload"]["cycle"], 0)
        self.assertEqual(rec3["payload"]["lag_ms"], 0.25)

    def test_logger_level_filtering_and_stderr(self):
        import io
        import sys
        from harness.core.logger import HarnessLogger
        from harness.core.model import ClockHold, IORead, TimerSleep

        # Stderr target with level="clock" only
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = stderr_buf
            logger = HarnessLogger(level="clock", target=":stderr", format="text", default_engine="filter-test")
            logger.log(IORead(fd=1, msg="Ignored line"))
            logger.log(TimerSleep(duration=1.0, target=101.0))
            logger.log(ClockHold(id="c1", cycle=1, target=10.0, delay_ms=10.0))
        finally:
            sys.stderr = old_stderr

        out = stderr_buf.getvalue()
        self.assertNotIn("IO:READ", out)
        self.assertNotIn("TIMER:SLEEP", out)
    def test_schedule_multi_rule_and_tags(self):
        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event, epoch_fn=clock.now)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Init schedule
        channel.feed_line("# @harness.schedule:init id=agenda")

        # 2. Add two rules: */5s (fast) and */10s (slow)
        channel.feed_line('# @harness.schedule:rule id=agenda expr="*/5s" tags="fast"')
        channel.feed_line('# @harness.schedule:rule id=agenda expr="*/10s" tags="slow"')

        # 3. Wait next occurrence: at T=100.0, next 5s slot is 105.0 (fast only)
        channel.feed_line("# @harness.schedule:wait id=agenda")
        self.assertEqual(len(channel.sent_messages), 0)  # Sleeping until 105.0

        # Advance to 105.0 and pop
        clock.advance(5.0)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 1)
        # Expected: schedule agenda <iso> 0.000 fast ok
        parts = channel.sent_messages[0].strip().split()
        self.assertEqual(parts[0], "schedule")
        self.assertEqual(parts[1], "agenda")
        self.assertEqual(parts[4], "fast")
        self.assertEqual(parts[5], "ok")

        # 4. Wait next occurrence: at T=105.0, next slot is 110.0 (both fast and slow!)
        channel.feed_line("# @harness.schedule:wait id=agenda")
        clock.advance(5.0)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 2)
        parts = channel.sent_messages[1].strip().split()
        self.assertEqual(parts[0], "schedule")
        self.assertEqual(parts[1], "agenda")
        self.assertEqual(parts[4], "fast,slow")
        self.assertEqual(parts[5], "ok")

    def test_schedule_dump_and_resume_catchup(self):
        import tempfile

        clock = VirtualClock(100.0)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event, epoch_fn=clock.now)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            state_path = tf.name

        try:
            # Session 1: run until T=110.0, dump state
            channel.feed_line(f'# @harness.schedule:init id=cron policy=catchup state_file="{state_path}"')
            channel.feed_line('# @harness.schedule:rule id=cron expr="*/5s" tags="job1"')
            channel.feed_line('# @harness.schedule:rule id=cron expr="*/10s" tags="job2"')

            # T=100.0 -> T=105.0 tick
            channel.feed_line("# @harness.schedule:wait id=cron")
            clock.advance(5.0)
            scheduler.pop_due_events()

            # T=105.0 -> T=110.0 tick
            channel.feed_line("# @harness.schedule:wait id=cron")
            clock.advance(5.0)
            scheduler.pop_due_events()
            self.assertEqual(len(channel.sent_messages), 2)

            # Explicit dump
            channel.feed_line(f'# @harness.schedule:dump id=cron file="{state_path}"')

            # Session 2: simulate worker crash/pause and resume at T=132.0 (22s later)
            clock2 = VirtualClock(132.0)
            scheduler2 = HeapScheduler(time_fn=clock2.now)
            channel_holder2 = []

            def emit_event2(ev):
                channel_holder2[0].send_event(ev)

            coprocessor2 = TimerCoprocessor(scheduler2, emit_event2, epoch_fn=clock2.now)
            channel2 = MemoryChannel(codec, coprocessor2.handle_instruction)
            channel_holder2.append(channel2)

            # Init from state file (restores rules and last_checkpoint=110.0)
            channel2.feed_line(f'# @harness.schedule:init id=cron policy=catchup state_file="{state_path}"')

            # Missed occurrences between 110.0 and 132.0:
            # 115.0 (job1)
            # 120.0 (job1, job2)
            # 125.0 (job1)
            # 130.0 (job1, job2)
            # These 4 must replay immediately without advancing time / sleep!

            # Missed 1: 115.0
            channel2.feed_line("# @harness.schedule:wait id=cron")
            self.assertEqual(len(channel2.sent_messages), 1)
            parts = channel2.sent_messages[-1].strip().split()
            self.assertEqual(parts[4], "job1")
            self.assertEqual(parts[5], "missed")

            # Missed 2: 120.0
            channel2.feed_line("# @harness.schedule:wait id=cron")
            self.assertEqual(len(channel2.sent_messages), 2)
            parts = channel2.sent_messages[-1].strip().split()
            self.assertEqual(parts[4], "job1,job2")
            self.assertEqual(parts[5], "missed")

            # Missed 3: 125.0
            channel2.feed_line("# @harness.schedule:wait id=cron")
            self.assertEqual(len(channel2.sent_messages), 3)
            parts = channel2.sent_messages[-1].strip().split()
            self.assertEqual(parts[4], "job1")
            self.assertEqual(parts[5], "missed")

            # Missed 4: 130.0
            channel2.feed_line("# @harness.schedule:wait id=cron")
            self.assertEqual(len(channel2.sent_messages), 4)
            parts = channel2.sent_messages[-1].strip().split()
            self.assertEqual(parts[4], "job1,job2")
            self.assertEqual(parts[5], "missed")

            # Next wait: missed queue is empty! Now it waits for future occurrence (135.0)
            channel2.feed_line("# @harness.schedule:wait id=cron")
            self.assertEqual(len(channel2.sent_messages), 4)  # No immediate message

            clock2.advance(3.0)  # 132.0 + 3.0 = 135.0
            scheduler2.pop_due_events()
            self.assertEqual(len(channel2.sent_messages), 5)
            parts = channel2.sent_messages[-1].strip().split()
            self.assertEqual(parts[4], "job1")
            self.assertEqual(parts[5], "ok")

        finally:
            import os
            if os.path.exists(state_path):
                os.remove(state_path)

    def test_schedule_jsonl_logging(self):
        import json
        import tempfile
        import os
        from harness.core.logger import HarnessLogger
        from harness.core.model import ScheduleHoldLog, ScheduleInitLog, ScheduleRuleLog, ScheduleTickLog

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            jsonl_path = tf.name

        try:
            logger = HarnessLogger(level="schedule", target=jsonl_path, format="jsonl", default_engine="agenda.sh")
            logger.log(ScheduleInitLog(id="cron", policy="catchup", state_file="/tmp/sched.json"))
            logger.log(ScheduleRuleLog(id="cron", expr="0 2 * * *", tags=["backup", "daily"], grid={"type": "cron"}))
            logger.log(ScheduleHoldLog(id="cron", target=1700000000.0, delay_ms=5000.0, tags=["backup"], grid={"type": "cron"}))
            logger.log(ScheduleTickLog(id="cron", scheduled="2026-09-13T02:00:00Z", lag_ms=1.25, tags=["backup"], status="ok", grid={"type": "cron"}))
            logger.close()

            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f]

            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[0]["action"], {"domain": "SCHEDULE", "name": "INIT"})
            self.assertEqual(lines[0]["payload"]["id"], "cron")
            self.assertEqual(lines[0]["payload"]["policy"], "catchup")

            self.assertEqual(lines[1]["action"], {"domain": "SCHEDULE", "name": "RULE"})
            self.assertEqual(lines[1]["payload"]["tags"], ["backup", "daily"])

            self.assertEqual(lines[2]["action"], {"domain": "SCHEDULE", "name": "HOLD"})
            self.assertEqual(lines[2]["payload"]["delay_ms"], 5000.0)

            self.assertEqual(lines[3]["action"], {"domain": "SCHEDULE", "name": "TICK"})
            self.assertEqual(lines[3]["payload"]["status"], "ok")
            self.assertEqual(lines[3]["payload"]["lag_ms"], 1.25)
        finally:
            if os.path.exists(jsonl_path):
                os.remove(jsonl_path)

    def test_minute_step_and_units(self):
        from harness.core.codec import parse_duration
        from harness.utils.cron import compute_next_occurrence
        from harness.coproc.timer import compute_align_delay

        # 1. Units parsing
        self.assertEqual(parse_duration("1min"), 60.0)
        self.assertEqual(parse_duration("5min"), 300.0)
        self.assertEqual(parse_duration("30sec"), 30.0)
        self.assertEqual(parse_duration("1.5min"), 90.0)

        # 2. Next occurrence with */1min
        nxt, parsed = compute_next_occurrence("*/1min", after_epoch=100.0)
        self.assertEqual(nxt, 120.0)
        self.assertEqual(parsed["type"], "step")
        self.assertEqual(parsed["step_seconds"], 60.0)

        # 3. Align delay with */1min
        delay = compute_align_delay("*/1min", now_epoch=100.0)
        self.assertEqual(delay, 20.0)

    def test_shift_action_golden_path(self):
        clock = VirtualClock(100.3)
        scheduler = HeapScheduler(time_fn=clock.now)
        codec = AnnotationCodec()

        channel_holder = []

        def emit_event(ev):
            channel_holder[0].send_event(ev)

        coprocessor = TimerCoprocessor(scheduler, emit_event, epoch_fn=clock.now)
        channel = MemoryChannel(codec, coprocessor.handle_instruction)
        channel_holder.append(channel)

        # 1. Shift to next round second (*/1s): at 100.3 -> target is 101.0 (delay 0.7s)
        channel.feed_line('# @harness.shift to="*/1s"')
        self.assertEqual(len(channel.sent_messages), 0)

        # Advance 0.7s to 101.0
        clock.advance(0.7)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 1)
        self.assertEqual(channel.sent_messages[0].strip(), "wakeup")

        # 2. Shift to next minute (*/1min): at 101.0 -> target is 120.0 (delay 19.0s)
        channel.feed_line('# @harness.shift to="*/1min"')
        self.assertEqual(len(channel.sent_messages), 1)

        clock.advance(19.0)
        scheduler.pop_due_events()
        self.assertEqual(len(channel.sent_messages), 2)
        self.assertEqual(channel.sent_messages[1].strip(), "wakeup")

    def test_millisecond_cron_expressions(self):
        from harness.utils.cron import compute_next_occurrence

        # 1. 7-field cron mask: ms sec min hour day month dow
        # Candidate steps: 0, 250, 500, 750
        nxt, parsed = compute_next_occurrence("*/250 * * * * * *", after_epoch=100.100)
        self.assertAlmostEqual(nxt, 100.250, places=5)
        self.assertEqual(parsed["type"], "cron")
        self.assertEqual(parsed["millisecond"], "*/250")
        self.assertEqual(parsed["second"], "*")

        # 2. Specific ms list: 0,500 * * * * * *
        nxt2, _ = compute_next_occurrence("0,500 * * * * * *", after_epoch=100.000)
        self.assertAlmostEqual(nxt2, 100.500, places=5)

        # 3. Next cycle wraps over into next second
        nxt3, _ = compute_next_occurrence("0,500 * * * * * *", after_epoch=100.500)
        self.assertAlmostEqual(nxt3, 101.000, places=5)

        # 4. Uniform step with ms duration: */250ms
        nxt4, parsed4 = compute_next_occurrence("*/250ms", after_epoch=100.100)
        self.assertAlmostEqual(nxt4, 100.250, places=5)
        self.assertEqual(parsed4["type"], "step")
        self.assertEqual(parsed4["step_seconds"], 0.25)

    def test_harness_time_directive(self):
        codec = AnnotationCodec()
        inst = codec.decode("# @harness.time")
        self.assertIsInstance(inst, TimeRequest)

        inst_now = codec.decode("# @harness.now")
        self.assertIsInstance(inst_now, TimeRequest)

        events_out = []
        ctx = CoprocessorContext(
            scheduler=HeapScheduler(),
            emit_event=lambda ev: events_out.append(ev),
        )
        timer = TimerCoprocessor(ctx)
        timer.handle_instruction(inst)

        self.assertEqual(len(events_out), 1)
        ev = events_out[0]
        self.assertIsInstance(ev, TimeResult)
        self.assertGreater(ev.epoch_ns, 0)
        self.assertTrue(":" in ev.wall_time)

        encoded = codec.encode(ev)
        self.assertTrue(encoded.startswith("time "))
        parts = encoded.strip().split()
        self.assertEqual(len(parts), 4)  # time <epoch_ns> <wall_time> <mono_s>

    def test_dlp_action_mask_templates_and_capture_groups(self):
        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        dlp = DlpCoprocessor(ctx)
        # Capture group and template with named group
        dlp.add_rule(
            pattern=r"user:(?P<uname>[a-zA-Z0-9]+)",
            action="mask",
            template="user:[REDACTED-{uname}]",
            name="user-mask",
        )
        out = ctx.apply_stream_filters("Logged in as user:alice and user:bob\n")
        self.assertEqual(out, "Logged in as user:[REDACTED-alice] and user:[REDACTED-bob]\n")

    def test_dlp_action_hash_deterministic_hmac(self):
        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        dlp = DlpCoprocessor(ctx, salt="secret-salt-xyz")
        dlp.add_rule(
            pattern=r"sk_live_[0-9a-zA-Z]{8}",
            action="hash",
            template="sk_live_{hash:8}",
            name="api-key",
        )
        line1 = ctx.apply_stream_filters("Auth key: sk_live_ABCDEF12\n")
        line2 = ctx.apply_stream_filters("Auth key: sk_live_ABCDEF12\n")
        line3 = ctx.apply_stream_filters("Auth key: sk_live_99999999\n")

        self.assertEqual(line1, line2)  # Deterministic hash
        self.assertNotEqual(line1, line3)
        self.assertTrue(line1.startswith("Auth key: sk_live_"))
        self.assertNotIn("ABCDEF12", line1)

    def test_dlp_action_alias_bimap_and_sequence_counters(self):
        import json
        import tempfile

        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        dlp = DlpCoprocessor(ctx)

        # 2 rules with different counter namespaces
        dlp.add_rule(
            pattern=r"CUST-\d{4}",
            action="alias",
            template="client_{seq.cust:03d}",
            name="customer",
        )
        dlp.add_rule(
            pattern=r"IP:10\.0\.0\.\d+",
            action="alias",
            template="IP:internal_{seq.ip:02d}",
            name="ip-addr",
        )

        # Line 1: first discovery of CUST-1001 and IP:10.0.0.5
        l1 = ctx.apply_stream_filters("Connect CUST-1001 via IP:10.0.0.5\n")
        self.assertEqual(l1, "Connect client_001 via IP:internal_01\n")

        # Line 2: discovery of new CUST-2002, same IP:10.0.0.5
        l2 = ctx.apply_stream_filters("Connect CUST-2002 via IP:10.0.0.5\n")
        self.assertEqual(l2, "Connect client_002 via IP:internal_01\n")

        # Line 3: recurrence of CUST-1001 -> cache hit, preserves client_001
        l3 = ctx.apply_stream_filters("Disconnect CUST-1001\n")
        self.assertEqual(l3, "Disconnect client_001\n")

        # Verify BiMap reversibility
        reconstructed_l1 = dlp.vault.unmask_line(l1)
        self.assertEqual(reconstructed_l1, "Connect CUST-1001 via IP:10.0.0.5\n")
        reconstructed_l2 = dlp.vault.unmask_line(l2)
        self.assertEqual(reconstructed_l2, "Connect CUST-2002 via IP:10.0.0.5\n")

        # Verify Vault JSONL (WAL) and JSON Serialization round-trip
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            vault_jsonl_file = tf.name

        dlp.vault.save_file(vault_jsonl_file)
        loaded_vault_jsonl = BiMapVault.load_file(vault_jsonl_file)
        self.assertEqual(loaded_vault_jsonl.unmask_line(l1), "Connect CUST-1001 via IP:10.0.0.5\n")
        self.assertEqual(loaded_vault_jsonl.forward["CUST-1001"], "client_001")
        self.assertEqual(loaded_vault_jsonl.reverse["client_001"], "CUST-1001")
        self.assertEqual(loaded_vault_jsonl.counters["cust"], 2)
        self.assertEqual(loaded_vault_jsonl.counters["ip"], 1)

        # Verify JSON dictionary round-trip
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf2:
            vault_json_file = tf2.name

        dlp.vault.save_file(vault_json_file)
        loaded_vault_json = BiMapVault.load_file(vault_json_file)
        self.assertEqual(loaded_vault_json.unmask_line(l1), "Connect CUST-1001 via IP:10.0.0.5\n")
        self.assertEqual(loaded_vault_json.forward["CUST-1001"], "client_001")
        self.assertEqual(loaded_vault_json.reverse["client_001"], "CUST-1001")

        # Test execute_unmask with '-' representing stdin
        from harness.cli import execute_unmask
        import io
        from unittest.mock import patch

        with patch("sys.stdin", io.StringIO(l1)), patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            code = execute_unmask(vault_jsonl_file, "-")
            self.assertEqual(code, 0)
            self.assertEqual(mock_stdout.getvalue(), "Connect CUST-1001 via IP:10.0.0.5\n")

    def test_dlp_inband_directive_alias(self):
        codec = AnnotationCodec()
        inst = codec.decode('# @harness.filter:mask pattern="user_[0-9]+" action="alias" template="u_{seq:02d}" name="usr"')
        self.assertIsInstance(inst, FilterMask)
        self.assertEqual(inst.action, "alias")
        self.assertEqual(inst.template, "u_{seq:02d}")

        ctx = CoprocessorContext(scheduler=HeapScheduler(), emit_event=lambda ev: None)
        dlp = DlpCoprocessor(ctx)
        dlp.handle_instruction(inst)

        out1 = ctx.apply_stream_filters("Hello user_42\n")
        out2 = ctx.apply_stream_filters("Goodbye user_42 and user_99\n")
        self.assertEqual(out1, "Hello u_01\n")
        self.assertEqual(out2, "Goodbye u_01 and u_02\n")

    def test_dlp_toml_configuration_loading(self):
        import tempfile
        from harness.coproc.dlp import load_dlp_config

        toml_content = """
[dlp]
fail_on_leak = true
summary = false
vault_file = "custom_vault.json"
salt = "my-custom-salt"

[[rules]]
name = "customer-id"
pattern = 'CUST-\\d{4}'
action = "alias"
template = "client_{seq.cust:03d}"

[[rules]]
name = "api-token"
pattern = 'tok_[0-9a-z]{8}'
action = "hash"
template = "tok_{hash:6}"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as tf:
            tf.write(toml_content)
            toml_path = tf.name

        instructions, fail_on_leak, summary, vault_file, salt = load_dlp_config(toml_path)
        self.assertTrue(fail_on_leak)
        self.assertFalse(summary)
        self.assertEqual(vault_file, "custom_vault.json")
        self.assertEqual(salt, "my-custom-salt")
        self.assertEqual(len(instructions), 2)
        self.assertEqual(instructions[0].name, "customer-id")
        self.assertEqual(instructions[0].action, "alias")
        self.assertEqual(instructions[0].template, "client_{seq.cust:03d}")
        self.assertEqual(instructions[1].name, "api-token")
        self.assertEqual(instructions[1].action, "hash")
        self.assertEqual(instructions[1].template, "tok_{hash:6}")

    def test_engine_user_group_umask_resolution(self):
        from harness.core.engine import HarnessEngine

        # 1. Compact user:group syntax
        e1 = HarnessEngine(child_cmd=["echo", "hi"], user="nobody:nogroup", umask="027")
        self.assertEqual(e1.user, "nobody")
        self.assertEqual(e1.group, "nogroup")
        self.assertEqual(e1.umask, 0o027)

        # 2. Separate user and group with integer umask
        e2 = HarnessEngine(child_cmd=["echo", "hi"], user="alice", group="staff", umask=0o022)
        self.assertEqual(e2.user, "alice")
        self.assertEqual(e2.group, "staff")
        self.assertEqual(e2.umask, 0o022)

    def test_coordinator_toml_user_group_umask(self):
        import tempfile
        from harness.config import load_coordinator_from_toml

        toml_content = """
[coordinator]
name = "secure-stack"

[[engines]]
name = "worker1"
command = "echo hello"
user = "nobody"
group = "nogroup"
umask = "027"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as tf:
            tf.write(toml_content)
            toml_path = tf.name

        coord = load_coordinator_from_toml(toml_path)
        self.assertEqual(len(coord.engines), 1)
        engine = coord.engines[0]
        self.assertEqual(engine.name, "worker1")
        self.assertEqual(engine.user, "nobody")
        self.assertEqual(engine.group, "nogroup")
        self.assertEqual(engine.umask, 0o027)

    def test_cli_normalization_user_and_umask(self):
        from harness.cli import normalize_cli_args

        # 1. run command
        args1 = ["run", "-u", "nobody", "-g", "nogroup", "--umask", "027", "./script.sh", "-v"]
        self.assertEqual(
            normalize_cli_args(args1),
            ["run", "-u", "nobody", "-g", "nogroup", "--umask", "027", "--", "./script.sh", "-v"],
        )

        # 2. dlp redact command
        args2 = ["dlp", "redact", "-u", "nobody", "-g", "nogroup", "--umask", "027", "-r", "rules.toml", "./server.sh", "--port", "8080"]
        self.assertEqual(
            normalize_cli_args(args2),
            ["dlp", "redact", "-u", "nobody", "-g", "nogroup", "--umask", "027", "-r", "rules.toml", "--", "./server.sh", "--port", "8080"],
        )

    def test_argparse_cli_dlp_redact_user_group_umask(self):
        from unittest.mock import patch
        from harness.cli import run_argparse_cli

        with patch("harness.cli.execute_redact") as mock_exec:
            mock_exec.return_value = 0
            run_argparse_cli(["dlp", "redact", "-u", "nobody:nogroup", "-g", "nogroup", "--umask", "027", "-r", "rules.toml", "echo", "test"])
            self.assertTrue(mock_exec.called)
            kwargs = mock_exec.call_args[1]
            self.assertEqual(kwargs["user"], "nobody:nogroup")
            self.assertEqual(kwargs["group"], "nogroup")
            self.assertEqual(kwargs["umask"], "027")
            self.assertEqual(kwargs["command_args"], ["echo", "test"])


if __name__ == "__main__":
    unittest.main()

