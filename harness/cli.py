"""
cli.py - Strongly typed CLI for fd-harness built with Typer.
Central entry point for run, coord, redact, and version subcommands.
"""
from pathlib import Path
import sys
from typing import Annotated, List, Optional
import typer

from harness.coproc import CoprocessorContext, coprocessor_registry
from harness.coproc.dlp import DlpCoprocessor, load_dlp_config
from harness.core import (
    CoprocessorRouter,
    FilterMask,
    HarnessEngine,
    HeapScheduler,
    Instruction,
)

__version__ = "0.1.0"


def normalize_cli_args(args: List[str]) -> List[str]:
    """
    Smart CLI argument normalizer for wrapper subcommands (run, redact).
    Allows executing scripts with their own flags without requiring '--',
    while also supporting '--cmd <target> <args...>' or traditional '--'.
    """
    if not args or args[0] not in ("run", "redact"):
        return args

    subcmd = args[0]
    sub_args = args[1:]

    # 1. If explicit '--cmd' is provided, translate it to '--' for Click/Typer
    if "--cmd" in sub_args:
        idx = sub_args.index("--cmd")
        return [subcmd] + sub_args[:idx] + ["--"] + sub_args[idx + 1 :]

    # 2. If '--' or help flags are already present, preserve them untouched
    if "--" in sub_args or "-h" in sub_args or "--help" in sub_args:
        return args

    # 3. Known options for supervisor subcommands
    if subcmd == "run":
        known_flags = {"-v", "--show-directives"}
        known_options_with_arg = {
            "--strace",
            "--log-level",
            "--log-target",
            "-t",
            "--log-format",
            "-f",
        }
    else:  # redact
        known_flags = {"-v", "--verbose", "--fail-on-leak", "--summary", "--no-summary"}
        known_options_with_arg = {
            "--rules",
            "-r",
            "--mask",
            "-m",
            "--log-target",
            "-t",
            "--log-format",
            "-f",
        }

    i = 0
    while i < len(sub_args):
        tok = sub_args[i]
        if tok in known_flags:
            i += 1
        elif tok in known_options_with_arg:
            i += 2
        elif any(tok.startswith(opt + "=") for opt in known_options_with_arg):
            i += 1
        elif tok.startswith("-"):
            # Unknown option flag preceding the command
            i += 1
        else:
            # First non-option token: this is the target command/script!
            # Stop option parsing by inserting '--' right before it.
            return [subcmd] + sub_args[:i] + ["--"] + sub_args[i:]

    return args


class NormalizedTyper(typer.Typer):
    """Typer application subclass that normalizes sys.argv before Click parsing."""

    def __call__(self, *args, **kwargs):
        if not args and "args" not in kwargs:
            kwargs["args"] = normalize_cli_args(sys.argv[1:])
        elif "args" in kwargs and kwargs["args"] is not None:
            kwargs["args"] = normalize_cli_args(kwargs["args"])
        return super().__call__(*args, **kwargs)


app = NormalizedTyper(
    name="fd-harness",
    help="Event-driven file descriptor supervisor.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command(
    name="run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_command(
    ctx: typer.Context,
    script: Annotated[
        str,
        typer.Argument(help="Target script or command to execute under the harness."),
    ],
    strace: Annotated[
        Optional[Path],
        typer.Option("--strace", help="Path to write strace log (-tt -T -f)."),
    ] = None,
    show_directives: Annotated[
        bool,
        typer.Option(
            "--show-directives",
            "-v",
            help="Print # @harness directives and protocol messages to terminal.",
        ),
    ] = False,
    log_level: Annotated[
        Optional[str],
        typer.Option(
            "--log-level",
            help="Harness logging level (e.g. 'io', 'clock', 'all'). Defaults to 'all' when -v is enabled.",
        ),
    ] = None,
    log_target: Annotated[
        str,
        typer.Option(
            "--log-target",
            "-t",
            help="Harness log target: ':stdout', ':stderr', or a file path.",
        ),
    ] = ":stdout",
    log_format: Annotated[
        str,
        typer.Option(
            "--log-format",
            "-f",
            help="Harness log format: 'text' or 'jsonl'.",
        ),
    ] = "text",
):
    """
    Run a script or executable under the fd-harness supervisor.
    Any trailing arguments are forwarded directly to the target script.
    """
    child_cmd = [script] + list(ctx.args)
    strace_path = str(strace) if strace else None
    engine = HarnessEngine(
        child_cmd=child_cmd,
        strace_file=strace_path,
        show_directives=show_directives,
        log_level=log_level,
        log_target=log_target,
        log_format=log_format,
        attach_stdin=True,
    )
    code = engine.run()
    raise typer.Exit(code=code)


@app.command(name="version")
def version_command():
    """Display fd-harness version."""
    typer.echo(f"fd-harness {__version__}")


@app.command(name="coord")
def coordinate_command(
    target: Annotated[
        Path,
        typer.Argument(
            help="Workspace directory containing harness.toml or direct path to TOML file.",
            exists=True,
            file_okay=True,
            dir_okay=True,
            readable=True,
        ),
    ] = Path("."),
    show_directives: Annotated[
        bool,
        typer.Option(
            "--show-directives",
            "-v",
            help="Print # @harness protocol trace messages.",
        ),
    ] = False,
    log_level: Annotated[
        Optional[str],
        typer.Option(
            "--log-level",
            help="Harness logging level (e.g. 'io').",
        ),
    ] = None,
    log_target: Annotated[
        Optional[str],
        typer.Option(
            "--log-target",
            "-t",
            help="Harness log target: ':stdout', ':stderr', or a file path.",
        ),
    ] = None,
    log_format: Annotated[
        Optional[str],
        typer.Option(
            "--log-format",
            "-f",
            help="Harness log format: 'text' or 'jsonl'.",
        ),
    ] = None,
):
    """
    Run a multi-engine coordinated environment defined in a TOML file.
    Multiplexes file descriptors, schedulers, and in-memory event bus.
    """
    from harness.config import load_coordinator_from_toml
    coordinator = load_coordinator_from_toml(
        target,
        show_directives=show_directives if show_directives else None,
        log_level=log_level,
        log_target=log_target,
        log_format=log_format,
    )
    results = coordinator.run()
    max_code = max(results.values()) if results else 0
    raise typer.Exit(code=max_code)


@app.command(
    name="redact",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def redact_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str],
        typer.Argument(help="Optional command to execute and sanitize. If omitted, reads from stdin."),
    ] = None,
    rules: Annotated[
        Optional[Path],
        typer.Option("--rules", "-r", help="Path to TOML rules file defining DLP patterns."),
    ] = None,
    mask: Annotated[
        Optional[List[str]],
        typer.Option("--mask", "-m", help="Ad-hoc masking rule formatted as 'PATTERN=REPLACEMENT'."),
    ] = None,
    fail_on_leak: Annotated[
        bool,
        typer.Option("--fail-on-leak", help="Exit with code 1 if any secret leak was detected."),
    ] = False,
    summary: Annotated[
        bool,
        typer.Option("--summary/--no-summary", help="Print audit summary to stderr upon completion."),
    ] = True,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose real-time match alerts and rule inspection."),
    ] = False,
    log_target: Annotated[
        str,
        typer.Option(
            "--log-target",
            "-t",
            help="Harness log target: ':stdout', ':stderr', or a file path.",
        ),
    ] = ":stdout",
    log_format: Annotated[
        str,
        typer.Option(
            "--log-format",
            "-f",
            help="Harness log format: 'text' or 'jsonl'.",
        ),
    ] = "text",
):
    """
    Sanitize secrets and sensitive tokens in real time from a piped stream or child process.
    """
    instructions: List[Instruction] = []
    toml_fail = False
    toml_summary = True

    if rules:
        loaded, toml_fail, toml_summary = load_dlp_config(rules)
        instructions.extend(loaded)

    if mask:
        for m in mask:
            rule_name = None
            if ":" in m and "=" in m and m.index(":") < m.index("="):
                rule_name, rest = m.split(":", 1)
                pat, rep = rest.split("=", 1)
            elif "=" in m:
                pat, rep = m.split("=", 1)
            else:
                pat, rep = m, "[REDACTED]"
            instructions.append(FilterMask(pattern=pat, replacement=rep, name=rule_name))

    effective_fail_on_leak = fail_on_leak or toml_fail
    effective_summary = summary and toml_summary

    if command:
        cmd_list = [command] + list(ctx.args)
        engine = HarnessEngine(
            child_cmd=cmd_list,
            initial_instructions=instructions,
            redirect_stderr=True,
            log_level="dlp" if verbose else None,
            log_target=log_target,
            log_format=log_format,
        )
        engine.start()
        dlp = engine.router.get_coprocessor(DlpCoprocessor) if engine.router else None
        if dlp:
            dlp.fail_on_leak = effective_fail_on_leak
            dlp.print_summary = effective_summary
            dlp.verbose = verbose
            if verbose:
                dlp.emit_startup_info()

        from harness.core.coordinator import HarnessCoordinator
        coordinator = HarnessCoordinator([engine])
        results = coordinator.run()
        code = results.get(engine, 0)
        if dlp:
            dlp.emit_audit_summary()
            if effective_fail_on_leak and dlp.total_leaks > 0:
                raise typer.Exit(code=1)
        raise typer.Exit(code=code)
    else:
        # Pipe mode: read from sys.stdin
        from harness.core.logger import HarnessLogger
        logger = HarnessLogger(
            level="dlp" if verbose else None,
            target=log_target,
            format=log_format,
            default_engine="stdin",
        )
        ctx_coproc = CoprocessorContext(
            scheduler=HeapScheduler(),
            emit_event=lambda ev: None,
            engine_name="stdin",
            log_level="dlp" if verbose else None,
            logger=logger,
        )
        router = CoprocessorRouter(coprocessor_registry, ctx_coproc)
        dlp = router.get_coprocessor(DlpCoprocessor)
        if dlp:
            dlp.fail_on_leak = effective_fail_on_leak
            dlp.print_summary = effective_summary
            dlp.verbose = verbose

        for inst in instructions:
            router.handle_instruction(inst)

        if dlp and verbose:
            dlp.emit_startup_info()

        for line in sys.stdin:
            sanitized = ctx_coproc.apply_stream_filters(line)
            if sanitized is not None:
                sys.stdout.write(sanitized)
                sys.stdout.flush()

        if dlp:
            dlp.emit_audit_summary()

        logger.close()

        if dlp and effective_fail_on_leak and dlp.total_leaks > 0:
            raise typer.Exit(code=1)
        raise typer.Exit(code=0)


def main():
    app()


if __name__ == "__main__":
    main()
