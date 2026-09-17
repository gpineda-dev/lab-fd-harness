"""
dlp.py - In-flight Data Loss Prevention (DLP) and stream redaction coprocessor.
Intercepts FilterMask instructions and dynamically attaches/updates
sanitizing filters to the CoprocessorContext stream pipeline.
Supports mask, hash, and stateful 1:1 alias policies with BiMap vault.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
from typing import Any, ClassVar, Dict, List, Optional, TextIO, Tuple, Union

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

_VAR_REGEX = re.compile(r"\{([a-zA-Z0-9_.]+)(?::([^}]+))?\}")


def render_template(
    template: str,
    raw: str,
    match: Optional[re.Match],
    action: str,
    vault: "BiMapVault",
    rule_salt: Optional[str] = None,
    default_salt: str = "fd-harness-default-salt",
) -> str:
    """Renders a format template string for a matched token."""
    if "{" not in template:
        return template

    salt = rule_salt or default_salt
    groupdict = match.groupdict() if match else {}
    groups = match.groups() if match else ()

    def replace_var(m: re.Match) -> str:
        var_name = m.group(1)
        fmt = m.group(2)

        if var_name == "raw":
            val = raw
        elif var_name == "hash":
            digest = hmac.new(
                salt.encode("utf-8"),
                raw.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if fmt and fmt.isdigit():
                return digest[: int(fmt)]
            return digest
        elif var_name == "seq" or var_name.startswith("seq."):
            ns = "default" if var_name == "seq" else var_name.split(".", 1)[1]
            vault.counters[ns] = vault.counters.get(ns, 0) + 1
            val = vault.counters[ns]
            if fmt:
                try:
                    return f"{val:{fmt}}"
                except ValueError:
                    return str(val)
            return str(val)
        elif var_name in groupdict:
            val = groupdict[var_name]
        elif var_name.startswith("g") and var_name[1:].isdigit():
            idx = int(var_name[1:]) - 1
            val = groups[idx] if 0 <= idx < len(groups) else ""
        else:
            return m.group(0)

        if fmt:
            try:
                return f"{val:{fmt}}"
            except Exception:
                return str(val)
        return str(val)

    return _VAR_REGEX.sub(replace_var, template)


import time


class BiMapVault:
    """
    Bijective 1:1 in-memory vault mapping original sensitive tokens to aliases.
    Maintains forward and reverse mappings, metadata, and namespaced sequence counters.
    Persists as a standardized event-stream JSONL WAL (vault_settings, mapping_item).
    Reconstructs reverse mapping and counter states dynamically upon loading.
    """

    def __init__(self, salt: str = "fd-harness-default-salt"):
        self.forward: Dict[str, str] = {}
        self.reverse: Dict[str, str] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self.counters: Dict[str, int] = {}
        self.salt: str = salt
        self._reverse_regex: Optional[re.Pattern] = None
        self._reverse_dirty: bool = False

    def get_or_create(self, raw: str, rule: "DlpRule", match: Optional[re.Match]) -> str:
        """Returns existing alias or creates a new one deterministically."""
        if raw in self.forward:
            return self.forward[raw]

        alias_tpl = rule.template or (f"{rule.name}_{{seq:03d}}" if rule.name else "alias_{seq:03d}")
        alias = render_template(
            template=alias_tpl,
            raw=raw,
            match=match,
            action=rule.action,
            vault=self,
            rule_salt=rule.salt,
            default_salt=self.salt,
        )
        self.forward[raw] = alias
        self.reverse[alias] = raw
        self.metadata[raw] = {
            "rule_id": rule.name,
            "created": round(time.time(), 3),
        }
        self._reverse_dirty = True
        return alias

    def unmask_line(self, line: str) -> str:
        """Reverses all known aliases in a line back to their original values."""
        if not self.reverse:
            return line

        if self._reverse_dirty or self._reverse_regex is None:
            sorted_aliases = sorted(self.reverse.keys(), key=len, reverse=True)
            escaped = [re.escape(k) for k in sorted_aliases if k]
            if escaped:
                self._reverse_regex = re.compile("|".join(escaped))
            else:
                self._reverse_regex = None
            self._reverse_dirty = False

        if self._reverse_regex is None:
            return line

        return self._reverse_regex.sub(lambda m: self.reverse.get(m.group(0), m.group(0)), line)

    def to_dict(self) -> Dict[str, Any]:
        """Returns a minimal JSON-serializable dictionary (non-redundant)."""
        return {
            "forward": dict(self.forward),
            "metadata": dict(self.metadata),
            "counters": dict(self.counters),
            "salt": self.salt,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BiMapVault":
        vault = cls(salt=data.get("salt", "fd-harness-default-salt"))
        vault.forward = dict(data.get("forward", {}))
        vault.metadata = dict(data.get("metadata", {}))
        vault.counters = dict(data.get("counters", {}))
        # Reconstruct reverse mapping in RAM
        vault.reverse = {v: k for k, v in vault.forward.items()}
        # If legacy dict had reverse, merge any extra
        if "reverse" in data and not vault.reverse:
            vault.reverse = dict(data["reverse"])
            vault.forward = {v: k for k, v in vault.reverse.items()}
        vault._reverse_dirty = True
        return vault

    def save_file(self, path: Union[str, Path]) -> None:
        """Saves vault to disk as standardized JSONL event stream or JSON dict."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".json":
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
        else:
            # Standardized JSONL Event Stream
            with open(p, "w", encoding="utf-8") as f:
                header = {
                    "type": "vault_settings",
                    "properties": {
                        "version": 1,
                        "salt": self.salt,
                        "counters": dict(self.counters),
                    },
                }
                f.write(json.dumps(header) + "\n")
                for raw, alias in self.forward.items():
                    props = {"raw": raw, "alias": alias}
                    if raw in self.metadata:
                        props.update(self.metadata[raw])
                    item = {"type": "mapping_item", "properties": props}
                    f.write(json.dumps(item) + "\n")

    def save_json(self, path: Union[str, Path]) -> None:
        self.save_file(path)

    @classmethod
    def load_file(cls, path: Union[str, Path]) -> "BiMapVault":
        """Loads vault from JSONL or JSON file, reconstructing reverse index in memory."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Vault file not found: {p}")

        with open(p, "r", encoding="utf-8") as f:
            content = f.read()

        if not content.strip():
            return cls()

        # Try parsing as JSON dictionary first
        if content.strip().startswith("{") and content.strip().endswith("}") and "\n" not in content.strip():
            try:
                data = json.loads(content)
                if isinstance(data, dict) and ("forward" in data or "salt" in data):
                    return cls.from_dict(data)
            except json.JSONDecodeError:
                pass

        if content.strip().startswith("{") and content.strip().endswith("}"):
            try:
                data = json.loads(content)
                if isinstance(data, dict) and ("forward" in data or "salt" in data):
                    return cls.from_dict(data)
            except json.JSONDecodeError:
                pass

        # Parse as standardized JSONL event stream
        vault = cls()
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec_type = record.get("type", record.get("t"))
            props = record.get("properties", record.get("props", record))

            if rec_type in ("vault_settings", "meta"):
                vault.salt = props.get("salt", vault.salt)
                vault.counters = dict(props.get("counters", {}))
            else:
                raw = props.get("raw")
                alias = props.get("alias")
                if raw and alias:
                    vault.forward[raw] = alias
                    vault.reverse[alias] = raw
                    meta = {k: v for k, v in props.items() if k not in ("raw", "alias")}
                    if meta:
                        vault.metadata[raw] = meta

        vault._reverse_dirty = True
        return vault

    @classmethod
    def load_json(cls, path: Union[str, Path]) -> "BiMapVault":
        return cls.load_file(path)


@dataclass
class DlpRule:
    name: str
    pattern: str
    action: str = "mask"  # "mask", "hash", "alias"
    template: Optional[str] = None
    replacement: Optional[str] = None
    salt: Optional[str] = None
    regex: re.Pattern = field(init=False)

    def __post_init__(self):
        self.regex = re.compile(self.pattern)
        if not self.template:
            if self.action == "mask":
                self.template = self.replacement or "[REDACTED]"
            elif self.action == "hash":
                self.template = self.replacement or "{hash:8}"
            elif self.action == "alias":
                self.template = self.replacement or (f"{self.name}_{{seq:03d}}" if self.name else "alias_{seq:03d}")


@register_coprocessor
class DlpCoprocessor(BaseCoprocessor):
    """
    DLP coprocessor managing regex redaction rules across mask, hash, and alias policies.
    Attaches a stream filter to CoprocessorContext to sanitize child stdout in real-time.
    """

    handled_instructions: ClassVar[Tuple[type[Instruction], ...]] = (FilterMask,)

    def __init__(
        self,
        ctx: CoprocessorContext,
        fail_on_leak: bool = False,
        print_summary: bool = True,
        verbose: bool = False,
        vault: Optional[BiMapVault] = None,
        vault_file: Optional[Union[str, Path]] = None,
        salt: str = "fd-harness-default-salt",
    ):
        super().__init__(ctx)
        self.fail_on_leak = fail_on_leak
        self.print_summary = print_summary
        self.verbose = verbose
        self.salt = salt
        self.vault = vault or BiMapVault(salt=salt)
        self.vault_file = Path(vault_file) if vault_file else None
        self.rules: List[DlpRule] = []
        self.match_counts: Dict[str, int] = {}
        self.total_leaks: int = 0
        self.line_count: int = 0
        self._filter_attached: bool = False

    def add_rule(
        self,
        pattern: str,
        replacement: Optional[str] = None,
        name: Optional[str] = None,
        action: str = "mask",
        template: Optional[str] = None,
        salt: Optional[str] = None,
    ) -> None:
        """Registers a redaction regex rule and ensures the filter is attached to stdout."""
        rule_name = name or pattern
        try:
            rule = DlpRule(
                name=rule_name,
                pattern=pattern,
                action=action,
                template=template,
                replacement=replacement,
                salt=salt,
            )
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
            self.add_rule(
                pattern=inst.pattern,
                replacement=inst.replacement,
                name=inst.name,
                action=inst.action,
                template=inst.template,
                salt=inst.salt,
            )

    def mask_line(self, line: str) -> Optional[str]:
        """Sanitizes line across all active DLP rules, counting violations."""
        self.line_count += 1
        sanitized = line
        is_verbose = self.verbose or self.ctx.is_log_enabled("dlp")
        raw_logged = False

        for rule in self.rules:
            matches = list(rule.regex.finditer(sanitized))
            if not matches:
                continue

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
                            replacement=rule.template or rule.replacement or "[MUTATED]",
                            action=rule.action,
                        )
                    )

            def repl(m: re.Match) -> str:
                raw = m.group(0)
                if rule.action == "alias":
                    return self.vault.get_or_create(raw, rule, m)
                elif rule.action == "hash":
                    return render_template(
                        template=rule.template or "{hash:8}",
                        raw=raw,
                        match=m,
                        action=rule.action,
                        vault=self.vault,
                        rule_salt=rule.salt,
                        default_salt=self.salt,
                    )
                else:  # mask
                    if "{" not in (rule.template or ""):
                        return rule.template or "[REDACTED]"
                    return render_template(
                        template=rule.template or "[REDACTED]",
                        raw=raw,
                        match=m,
                        action=rule.action,
                        vault=self.vault,
                        rule_salt=rule.salt,
                        default_salt=self.salt,
                    )

            sanitized = rule.regex.sub(repl, sanitized)

        return sanitized

    def save_vault(self) -> None:
        """Persists the BiMap vault to disk if vault_file was configured."""
        if self.vault_file:
            self.vault.save_file(self.vault_file)

    def load_from_toml(self, toml_path: Union[str, Path]) -> None:
        """Loads DLP configuration, vault, and rules from a TOML file."""
        instructions, fail_on_leak, summary, vault_file, salt = load_dlp_config(toml_path)
        self.fail_on_leak = fail_on_leak
        self.print_summary = summary
        if salt:
            self.salt = salt
            self.vault.salt = salt
        if vault_file:
            self.vault_file = Path(vault_file)
            if self.vault_file.is_file():
                self.vault = BiMapVault.load_file(self.vault_file)
        for inst in instructions:
            self.handle_instruction(inst)

    def emit_startup_info(self, target: Optional[TextIO] = None) -> None:
        """Prints loaded rules overview when verbose mode is enabled."""
        if not (self.verbose or self.ctx.is_log_enabled("dlp")):
            return
        self.ctx.log(DlpInit(rules=[r.name for r in self.rules]))

    def emit_audit_summary(self, target: Optional[TextIO] = None) -> None:
        """Prints a structured summary of redacted violations to stderr (or target stream)."""
        self.save_vault()
        if self.verbose or self.ctx.is_log_enabled("dlp"):
            self.ctx.log(DlpAudit(violations=self.total_leaks, lines=self.line_count))

        if not self.print_summary:
            return

        out = target or sys.stderr
        if self.verbose or self.ctx.is_log_enabled("dlp"):
            out.write(
                f"\n[DLP AUDIT] Security report: {self.total_leaks} violation(s) intercepted across {self.line_count} line(s):\n"
            )
            for rule in self.rules:
                count = self.match_counts.get(rule.name, 0)
                status = "[VIOLATION]" if count > 0 else "[CLEAN]    "
                out.write(f"  - {status} {rule.name:14s}: {count} occurrence(s) masked\n")
            if self.fail_on_leak:
                out.write("[DLP AUDIT] Enforcement: build failed due to secret leak (--fail-on-leak).\n")
            out.flush()
        elif self.total_leaks > 0:
            out.write(f"\n[DLP AUDIT] Security alert: {self.total_leaks} sensitive pattern(s) redacted:\n")
            for rule in self.rules:
                count = self.match_counts.get(rule.name, 0)
                if count > 0:
                    out.write(f"  - {rule.name}: {count} occurrence(s) masked\n")
            if self.fail_on_leak:
                out.write("[DLP AUDIT] Enforcement: build failed due to secret leak (--fail-on-leak).\n")
            out.flush()


def load_dlp_config(
    toml_path: Union[str, Path],
) -> Tuple[List[FilterMask], bool, bool, Optional[str], Optional[str]]:
    """
    Parses a DLP TOML file and converts rules into FilterMask instructions,
    returning (instructions, fail_on_leak, summary, vault_file, salt).
    """
    path = Path(toml_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DLP configuration file not found: {path}")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    dlp_section = data.get("dlp", {})
    fail_on_leak = bool(dlp_section.get("fail_on_leak", False))
    summary = bool(dlp_section.get("summary", True))
    vault_file = dlp_section.get("vault_file")
    salt = dlp_section.get("salt")

    instructions: List[FilterMask] = []
    for item in data.get("rules", []):
        pattern = item.get("pattern")
        replacement = item.get("replacement")
        template = item.get("template")
        action = item.get("action", "mask")
        name = item.get("name")
        rule_salt = item.get("salt")
        if pattern:
            instructions.append(
                FilterMask(
                    pattern=pattern,
                    replacement=replacement,
                    template=template,
                    action=action,
                    name=name,
                    salt=rule_salt,
                )
            )

    return instructions, fail_on_leak, summary, vault_file, salt
