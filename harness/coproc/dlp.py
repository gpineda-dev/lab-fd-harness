"""
dlp.py - In-flight Data Loss Prevention (DLP) and stream redaction coprocessor.
Intercepts FilterMask instructions and dynamically attaches/updates
sanitizing filters to the CoprocessorContext stream pipeline.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from typing import ClassVar, Dict, List, Optional, TextIO, Tuple, Union

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.model import (
    DlpAudit,
    DlpInit,
    DlpMutate,
    FilterMask,
    IORead,
    Instruction,
)


@dataclass
class DlpRule:
    name: str
    pattern: str
    replacement: str
    regex: re.Pattern = field(init=False)

    def __post_init__(self):
        self.regex = re.compile(self.pattern)


@register_coprocessor
class DlpCoprocessor(BaseCoprocessor):
    """
    DLP coprocessor managing regex redaction rules.
    Attaches a stream filter to CoprocessorContext to sanitize child stdout in real-time.
    """
    handled_instructions: ClassVar[Tuple[type[Instruction], ...]] = (FilterMask,)

    def __init__(
        self,
        ctx: CoprocessorContext,
        fail_on_leak: bool = False,
        print_summary: bool = True,
        verbose: bool = False,
    ):
        super().__init__(ctx)
        self.fail_on_leak = fail_on_leak
        self.print_summary = print_summary
        self.verbose = verbose
        self.rules: List[DlpRule] = []
        self.match_counts: Dict[str, int] = {}
        self.total_leaks: int = 0
        self.line_count: int = 0
        self._filter_attached: bool = False

    def add_rule(self, pattern: str, replacement: str = "[REDACTED]", name: Optional[str] = None) -> None:
        """Registers a redaction regex rule and ensures the filter is attached to stdout."""
        rule_name = name or pattern
        try:
            rule = DlpRule(name=rule_name, pattern=pattern, replacement=replacement)
        except re.error:
            return

        self.rules.append(rule)
        self.match_counts.setdefault(rule_name, 0)
        self._ensure_filter_attached()

    def _ensure_filter_attached(self) -> None:
        if not self._filter_attached:
            self.ctx.attach_stdout_filter("dlp", self.mask_line, priority=50)
            self._filter_attached = True

    def detach_filter(self) -> None:
        """Detaches the DLP filter from CoprocessorContext."""
        if self._filter_attached:
            self.ctx.detach_stdout_filter("dlp")
            self._filter_attached = False

    def handle_instruction(self, inst: Instruction) -> None:
        """Handles in-band # @harness.filter:mask instructions."""
        if isinstance(inst, FilterMask):
            self.add_rule(pattern=inst.pattern, replacement=inst.replacement, name=inst.name)

    def mask_line(self, line: str) -> Optional[str]:
        """Sanitizes line across all active DLP rules, counting violations."""
        self.line_count += 1
        sanitized = line
        is_verbose = self.verbose or self.ctx.is_log_enabled("dlp")
        raw_logged = False

        for rule in self.rules:
            matches = list(rule.regex.finditer(sanitized))
            if matches:
                count = len(matches)
                self.match_counts[rule.name] += count
                self.total_leaks += count
                if is_verbose:
                    if not raw_logged:
                        self.ctx.log(IORead(fd=1, msg=line.strip()))
                        raw_logged = True
                    for m in matches:
                        raw = m.group(0)
                        if len(raw) > 12:
                            preview = f"{raw[:6]}...{raw[-4:]}"
                        elif len(raw) > 4:
                            preview = f"{raw[:3]}...{raw[-1:]}"
                        else:
                            preview = "****"
                        self.ctx.log(
                            DlpMutate(
                                line=self.line_count,
                                rule=rule.name,
                                token=preview,
                                replacement=rule.replacement,
                            )
                        )
                sanitized = rule.regex.sub(rule.replacement, sanitized)
        return sanitized

    def load_from_toml(self, toml_path: Union[str, Path]) -> None:
        """Loads DLP configuration and rules from a TOML file."""
        instructions, fail_on_leak, summary = load_dlp_config(toml_path)
        self.fail_on_leak = fail_on_leak
        self.print_summary = summary
        for inst in instructions:
            self.handle_instruction(inst)

    def emit_startup_info(self, target: Optional[TextIO] = None) -> None:
        """Prints loaded rules overview when verbose mode is enabled."""
        if not (self.verbose or self.ctx.is_log_enabled("dlp")):
            return
        self.ctx.log(DlpInit(rules=[r.name for r in self.rules]))

    def emit_audit_summary(self, target: Optional[TextIO] = None) -> None:
        """Prints a structured summary of redacted violations to stderr (or target stream)."""
        if self.verbose or self.ctx.is_log_enabled("dlp"):
            self.ctx.log(DlpAudit(violations=self.total_leaks, lines=self.line_count))

        if not self.print_summary:
            return

        out = target or sys.stderr
        if self.verbose or self.ctx.is_log_enabled("dlp"):
            out.write(f"\n[DLP AUDIT] Security report: {self.total_leaks} violation(s) intercepted across {self.line_count} line(s):\n")
            for rule in self.rules:
                count = self.match_counts.get(rule.name, 0)
                status = "[VIOLATION]" if count > 0 else "[CLEAN]    "
                out.write(f"  - {status} {rule.name:14s}: {count} occurrence(s) masked\n")
            if self.fail_on_leak:
                out.write("[DLP AUDIT] Enforcement: build failed due to secret leak (--fail-on-leak).\n")
            out.flush()
        elif self.total_leaks > 0:
            out.write(f"\n[DLP AUDIT] Security alert: {self.total_leaks} sensitive pattern(s) redacted:\n")
            for name, count in self.match_counts.items():
                if count > 0:
                    out.write(f"  - {name}: {count} occurrence(s) masked\n")
            if self.fail_on_leak:
                out.write("[DLP AUDIT] Enforcement: build failed due to secret leak (--fail-on-leak).\n")
            out.flush()


def load_dlp_config(toml_path: Union[str, Path]) -> Tuple[List[FilterMask], bool, bool]:
    """
    Parses a DLP TOML file and converts rules into FilterMask instructions,
    returning (instructions, fail_on_leak, summary).
    """
    path = Path(toml_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DLP configuration file not found: {path}")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    dlp_section = data.get("dlp", {})
    fail_on_leak = bool(dlp_section.get("fail_on_leak", False))
    summary = bool(dlp_section.get("summary", True))

    instructions: List[FilterMask] = []
    for item in data.get("rules", []):
        pattern = item.get("pattern")
        replacement = item.get("replacement", "[REDACTED]")
        name = item.get("name")
        if pattern:
            instructions.append(FilterMask(pattern=pattern, replacement=replacement, name=name))

    return instructions, fail_on_leak, summary
