"""
cli.py - Strongly typed CLI for fd-harness with Typer and standard library fallback.
Central entry point for run, coord, dlp (redact, unmask), and version subcommands.
"""
import argparse
from pathlib import Path
import sys
from typing import Any, List, Optional, Union

try:
    from typing import Annotated  # Python 3.9+
except ImportError:
    from typing_extensions import Annotated  # type: ignore

try:
    import typer
except ImportError:
    typer = None

from harness.coproc import CoprocessorContext, coprocessor_registry
from harness.coproc.dlp import BiMapVault, DlpCoprocessor, load_dlp_config
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
    Smart CLI argument normalizer for wrapper subcommands (run, dlp redact).
    Allows executing scripts with their own flags without requiring '--',
    while also supporting '--cmd <target> <args...>' or traditional '--'.
    """
    if not args:
        return args

    # Check for "dlp redact" nested command
    if args[0] == "dlp" and len(args) > 1 and args[1] == "redact":
        prefix = ["dlp", "redact"]
        subcmd = "redact"
        sub_args = args[2:]
    elif args[0] == "run":
        prefix = [args[0]]
        subcmd = args[0]
        sub_args = args[1:]
    else:
        return args

    # 1. If explicit '--cmd' is provided, translate it to '--' for Click/Typer
    if "--cmd" in sub_args:
        idx = sub_args.index("--cmd")
        return prefix + sub_args[:idx] + ["--"] + sub_args[idx + 1 :]

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
            "--user",
            "-u",
            "--group",
            "-g",
            "--umask",
            "--run-as",
        }
    else:  # redact
        known_flags = {"-v", "--verbose", "--fail-on-leak", "--summary", "--no-summary"}
        known_options_with_arg = {
            "--rules",
            "-r",
            "--mask",
            "-m",
            "--vault",
            "-V",
            "--salt",
            "--log-target",
            "-t",
            "--log-format",
            "-f",
            "--user",
            "-u",
            "--group",
            "-g",
            "--umask",
            "--run-as",
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
            return prefix + sub_args[:i] + ["--"] + sub_args[i:]

    return args


# ==============================================================================
# Core Execution Handlers (Pure Python Stdlib)
# ==============================================================================
def execute_run(
    child_cmd: List[str],
    strace: Optional[Union[str, Path]] = None,
    show_directives: bool = False,
    log_level: Optional[str] = None,
    log_target: str = ":stdout",
    log_format: str = "text",
    user: Optional[str] = None,
    group: Optional[str] = None,
    umask: Optional[Union[str, int]] = None,
) -> int:
    strace_path = str(strace) if strace else None
    engine = HarnessEngine(
        child_cmd=child_cmd,
        strace_file=strace_path,
        show_directives=show_directives,
        log_level=log_level,
        log_target=log_target,
        log_format=log_format,
        attach_stdin=True,
        user=user,
        group=group,
        umask=umask,
    )
    return engine.run()


def execute_coord(
    target: Union[str, Path] = Path("."),
    show_directives: bool = False,
    log_level: Optional[str] = None,
    log_target: Optional[str] = None,
    log_format: Optional[str] = None,
) -> int:
    from harness.config import load_coordinator_from_toml

    coordinator = load_coordinator_from_toml(
        target,
        show_directives=show_directives if show_directives else None,
        log_level=log_level,
        log_target=log_target,
        log_format=log_format,
    )
    results = coordinator.run()
    return max(results.values()) if results else 0


def execute_redact(
    command_args: Optional[List[str]] = None,
    rules_path: Optional[Union[str, Path]] = None,
    mask_list: Optional[List[str]] = None,
    vault_path: Optional[Union[str, Path]] = None,
    salt_str: Optional[str] = None,
    fail_on_leak: bool = False,
    summary: bool = True,
    verbose: bool = False,
    log_target: str = ":stdout",
    log_format: str = "text",
    user: Optional[str] = None,
    group: Optional[str] = None,
    umask: Optional[Union[str, int]] = None,
) -> int:
    instructions: List[Instruction] = []
    toml_fail = False
    toml_summary = True
    toml_vault_file = None
    toml_salt = None

    if rules_path:
        loaded, toml_fail, toml_summary, toml_vault_file, toml_salt = load_dlp_config(rules_path)
        instructions.extend(loaded)

    if mask_list:
        for m in mask_list:
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
    effective_vault_file = Path(vault_path) if vault_path else (Path(toml_vault_file) if toml_vault_file else None)
    effective_salt = salt_str or toml_salt or "fd-harness-default-salt"

    bimap_vault: Optional[BiMapVault] = None
    if effective_vault_file and effective_vault_file.is_file():
        bimap_vault = BiMapVault.load_file(effective_vault_file)
        if effective_salt:
            bimap_vault.salt = effective_salt
    elif effective_vault_file:
        bimap_vault = BiMapVault(salt=effective_salt)

    if command_args:
        engine = HarnessEngine(
            child_cmd=command_args,
            initial_instructions=instructions,
            redirect_stderr=True,
            log_level="dlp" if verbose else None,
            log_target=log_target,
            log_format=log_format,
            user=user,
            group=group,
            umask=umask,
        )
        engine.start()
        dlp = engine.router.get_coprocessor(DlpCoprocessor) if engine.router else None
        if dlp:
            dlp.fail_on_leak = effective_fail_on_leak
            dlp.print_summary = effective_summary
            dlp.verbose = verbose
            dlp.salt = effective_salt
            if bimap_vault:
                dlp.vault = bimap_vault
            if effective_vault_file:
                dlp.vault_file = effective_vault_file
            if verbose:
                dlp.emit_startup_info()

        from harness.core.coordinator import HarnessCoordinator

        coordinator = HarnessCoordinator([engine])
        results = coordinator.run()
        code = results.get(engine, 0)
        if dlp:
            dlp.emit_audit_summary()
            if effective_fail_on_leak and dlp.total_leaks > 0:
                return 1
        return code
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
            dlp.salt = effective_salt
            if bimap_vault:
                dlp.vault = bimap_vault
            if effective_vault_file:
                dlp.vault_file = effective_vault_file

        for inst in instructions:
            router.handle_instruction(inst)

        from harness.core.codec import AnnotationCodec

        codec = AnnotationCodec()

        for line in sys.stdin:
            if "# @harness." in line:
                inst = codec.decode(line)
                if inst:
                    router.handle_instruction(inst)
                    continue
            sanitized = ctx_coproc.apply_stream_filters(line)
            if sanitized is not None:
                sys.stdout.write(sanitized)
                sys.stdout.flush()

        if dlp:
            dlp.emit_audit_summary()

        logger.close()

        if dlp and effective_fail_on_leak and dlp.total_leaks > 0:
            return 1
        return 0


def execute_unmask(vault_file: Union[str, Path], file_path: Optional[Union[str, Path]] = None) -> int:
    vpath = Path(vault_file)
    if not vpath.is_file():
        sys.stderr.write(f"Error: vault file not found: {vpath}\n")
        return 1

    vault_obj = BiMapVault.load_file(vpath)
    if file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                sys.stdout.write(vault_obj.unmask_line(line))
    else:
        for line in sys.stdin:
            sys.stdout.write(vault_obj.unmask_line(line))
            sys.stdout.flush()
    return 0


# ==============================================================================
# Typer CLI Definition
# ==============================================================================
if typer is not None:

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

    dlp_app = typer.Typer(
        name="dlp",
        help="Data Loss Prevention (DLP) and stream pseudonymization tools.",
        no_args_is_help=True,
    )
    app.add_typer(dlp_app, name="dlp")

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
        user: Annotated[
            Optional[str],
            typer.Option(
                "--user",
                "-u",
                help="Run command as specified user (name or UID). Supports 'user:group'.",
            ),
        ] = None,
        group: Annotated[
            Optional[str],
            typer.Option(
                "--group",
                "-g",
                help="Run command as specified group (name or GID).",
            ),
        ] = None,
        umask: Annotated[
            Optional[str],
            typer.Option(
                "--umask",
                help="File creation mode mask in octal (e.g. 027).",
            ),
        ] = None,
    ):
        child_cmd = [script] + list(ctx.args)
        code = execute_run(
            child_cmd=child_cmd,
            strace=strace,
            show_directives=show_directives,
            log_level=log_level,
            log_target=log_target,
            log_format=log_format,
            user=user,
            group=group,
            umask=umask,
        )
        raise typer.Exit(code=code)

    @app.command(name="version")
    def version_command():
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
        code = execute_coord(
            target=target,
            show_directives=show_directives,
            log_level=log_level,
            log_target=log_target,
            log_format=log_format,
        )
        raise typer.Exit(code=code)

    @dlp_app.command(
        name="redact",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def dlp_redact_command(
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
        vault: Annotated[
            Optional[Path],
            typer.Option("--vault", "-V", help="Path to BiMap vault JSON file for pseudonymization."),
        ] = None,
        salt: Annotated[
            Optional[str],
            typer.Option("--salt", help="Secret salt string for HMAC hashing and alias generation."),
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
        user: Annotated[
            Optional[str],
            typer.Option(
                "--user",
                "-u",
                help="Run command as specified user (name or UID). Supports 'user:group'.",
            ),
        ] = None,
        group: Annotated[
            Optional[str],
            typer.Option(
                "--group",
                "-g",
                help="Run command as specified group (name or GID).",
            ),
        ] = None,
        umask: Annotated[
            Optional[str],
            typer.Option(
                "--umask",
                help="File creation mode mask in octal (e.g. 027).",
            ),
        ] = None,
    ):
        cmd_list = [command] + list(ctx.args) if command else None
        code = execute_redact(
            command_args=cmd_list,
            rules_path=rules,
            mask_list=mask,
            vault_path=vault,
            salt_str=salt,
            fail_on_leak=fail_on_leak,
            summary=summary,
            verbose=verbose,
            log_target=log_target,
            log_format=log_format,
            user=user,
            group=group,
            umask=umask,
        )
        raise typer.Exit(code=code)

    @dlp_app.command(name="unmask")
    def dlp_unmask_command(
        vault_file: Annotated[
            Path,
            typer.Option("--vault", "-V", help="Path to BiMap vault JSON file."),
        ],
        file: Annotated[
            Optional[Path],
            typer.Argument(help="Optional file to unmask. If omitted, reads from stdin."),
        ] = None,
    ):
        code = execute_unmask(vault_file=vault_file, file_path=file)
        raise typer.Exit(code=code)

else:
    app = None


# ==============================================================================
# Argparse CLI Fallback (Zero-Dependency Stdlib)
# ==============================================================================
def _add_redact_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("-r", "--rules", help="Rules TOML file")
    parser.add_argument("-m", "--mask", action="append", help="Ad-hoc mask pattern=replacement")
    parser.add_argument("-V", "--vault", help="BiMap vault JSON file")
    parser.add_argument("--salt", help="Secret salt")
    parser.add_argument("--fail-on-leak", action="store_true", help="Exit with 1 on leak")
    parser.add_argument("--no-summary", dest="summary", action="store_false", default=True, help="Disable summary")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose match logs")
    parser.add_argument("-t", "--log-target", default=":stdout", help="Log target")
    parser.add_argument("-f", "--log-format", default="text", help="Log format")
    parser.add_argument("-u", "--user", help="Run command as specified user (name or UID)")
    parser.add_argument("-g", "--group", help="Run command as specified group (name or GID)")
    parser.add_argument("--umask", help="File creation mode mask in octal (e.g. 027)")
    parser.add_argument("command", nargs="?", help="Command to execute")
    parser.add_argument("extra_args", nargs=argparse.REMAINDER, help="Trailing args")


def _add_unmask_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("-V", "--vault", required=True, help="BiMap vault JSON file")
    parser.add_argument("file", nargs="?", help="File to unmask (defaults to stdin)")


def run_argparse_cli(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fd-harness",
        description="Event-driven file descriptor supervisor.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # version
    subparsers.add_parser("version", help="Display version")

    # run
    p_run = subparsers.add_parser("run", help="Run a script under fd-harness")
    p_run.add_argument("script", help="Target script or command")
    p_run.add_argument("--strace", help="Strace log path")
    p_run.add_argument("-v", "--show-directives", action="store_true", help="Show directives")
    p_run.add_argument("--log-level", help="Log level")
    p_run.add_argument("-t", "--log-target", default=":stdout", help="Log target")
    p_run.add_argument("-f", "--log-format", default="text", help="Log format")
    p_run.add_argument("-u", "--user", help="Run command as specified user (name or UID)")
    p_run.add_argument("-g", "--group", help="Run command as specified group (name or GID)")
    p_run.add_argument("--umask", help="File creation mode mask in octal (e.g. 027)")
    p_run.add_argument("extra_args", nargs=argparse.REMAINDER, help="Trailing args")

    # coord
    p_coord = subparsers.add_parser("coord", help="Run coordinator")
    p_coord.add_argument("target", nargs="?", default=".", help="TOML file or directory")
    p_coord.add_argument("-v", "--show-directives", action="store_true", help="Show directives")
    p_coord.add_argument("--log-level", help="Log level")
    p_coord.add_argument("-t", "--log-target", help="Log target")
    p_coord.add_argument("-f", "--log-format", help="Log format")

    # dlp group: fd-harness dlp redact / fd-harness dlp unmask
    p_dlp = subparsers.add_parser("dlp", help="Data Loss Prevention tools")
    dlp_sub = p_dlp.add_subparsers(dest="dlp_subcommand", help="DLP subcommands")
    p_dlp_redact = dlp_sub.add_parser("redact", help="Sanitize secrets in real time")
    _add_redact_arguments(p_dlp_redact)
    p_dlp_unmask = dlp_sub.add_parser("unmask", help="Restore unmasked values via vault")
    _add_unmask_arguments(p_dlp_unmask)

    parsed = parser.parse_args(args)
    if not parsed.subcommand:
        parser.print_help()
        return 0

    if parsed.subcommand == "version":
        print(f"fd-harness {__version__}")
        return 0
    elif parsed.subcommand == "run":
        cmd_list = [parsed.script] + (parsed.extra_args or [])
        return execute_run(
            child_cmd=cmd_list,
            strace=parsed.strace,
            show_directives=parsed.show_directives,
            log_level=parsed.log_level,
            log_target=parsed.log_target,
            log_format=parsed.log_format,
            user=parsed.user,
            group=parsed.group,
            umask=parsed.umask,
        )
    elif parsed.subcommand == "coord":
        return execute_coord(
            target=parsed.target,
            show_directives=parsed.show_directives,
            log_level=parsed.log_level,
            log_target=parsed.log_target,
            log_format=parsed.log_format,
        )
    elif parsed.subcommand == "dlp":
        if getattr(parsed, "dlp_subcommand", None) == "redact":
            cmd_list = [parsed.command] + (parsed.extra_args or []) if parsed.command else None
            return execute_redact(
                command_args=cmd_list,
                rules_path=parsed.rules,
                mask_list=parsed.mask,
                vault_path=parsed.vault,
                salt_str=parsed.salt,
                fail_on_leak=parsed.fail_on_leak,
                summary=parsed.summary,
                verbose=parsed.verbose,
                log_target=parsed.log_target,
                log_format=parsed.log_format,
                user=parsed.user,
                group=parsed.group,
                umask=parsed.umask,
            )
        elif getattr(parsed, "dlp_subcommand", None) == "unmask":
            return execute_unmask(vault_file=parsed.vault, file_path=parsed.file)
        else:
            p_dlp.print_help()
            return 0

    return 0


def main():
    if typer is not None:
        app()
    else:
        code = run_argparse_cli(sys.argv[1:])
        sys.exit(code)


if __name__ == "__main__":
    main()
