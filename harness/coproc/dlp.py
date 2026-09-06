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
from harness.core.model import FilterMask, Instruction


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
    ):
        super().__init__(ctx)
        self.fail_on_leak = fail_on_leak
        self.print_summary = print_summary
        self.rules: List[DlpRule] = []
        self.match_counts: Dict[str, int] = {}
        self.total_leaks: int = 0
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
            self.add_rule(pattern=inst.pattern, replacement=inst.replacement)

    def mask_line(self, line: str) -> Optional[str]:
        """Sanitizes line across all active DLP rules, counting violations."""
        sanitized = line
        for rule in self.rules:
            matches = len(rule.regex.findall(sanitized))
            if matches > 0:
                self.match_counts[rule.name] += matches
                self.total_leaks += matches
                sanitized = rule.regex.sub(rule.replacement, sanitized)
        return sanitized

    def load_from_toml(self, toml_path: Union[str, Path]) -> None:
        """Loads DLP configuration and rules from a TOML file."""
        instructions, fail_on_leak, summary = load_dlp_config(toml_path)
        self.fail_on_leak = fail_on_leak
        self.print_summary = summary
        for inst in instructions:
            self.handle_instruction(inst)

    def emit_audit_summary(self, target: Optional[TextIO] = None) -> None:
        """Prints a structured summary of redacted violations to stderr (or target stream)."""
        if not self.print_summary:
            return

        out = target or sys.stderr
        if self.total_leaks > 0:
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
        if pattern:
            instructions.append(FilterMask(pattern=pattern, replacement=replacement))

    return instructions, fail_on_leak, summary
