"""
test_harness.py - Unit tests for HeapScheduler, AnnotationCodec, and TimerCoprocessor.
Runs deterministically with a virtual clock in less than 10 milliseconds.
"""
import unittest

from harness.coproc import (
    BaseCoprocessor,
    CoprocessorContext,
    DlpCoprocessor,
    TimerCoprocessor,
    coprocessor_registry,
    register_coprocessor,
)
from harness.core import (
    AnnotationCodec,
    CoprocessorRouter,
    HeapScheduler,
    MemoryChannel,
)


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


if __name__ == "__main__":
    unittest.main()

