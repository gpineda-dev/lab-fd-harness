"""
config.py - TOML configuration loader for multi-engine HarnessCoordinator.
Allows declarative specification of engines, commands, arguments, and coordination options.
Treats the TOML file location as the Workspace root directory (Docker Compose style).
"""
from pathlib import Path
import shlex
from typing import Any, Dict, List, Union

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from harness.core.coordinator import HarnessCoordinator
from harness.core.engine import HarnessEngine


def load_coordinator_from_toml(config_target: Union[str, Path]) -> HarnessCoordinator:
    """
    Parses a TOML file (or a workspace directory containing harness.toml)
    and initializes a HarnessCoordinator with configured engines.
    
    All relative paths inside the TOML file are resolved relative to the TOML file's directory (Workspace),
    and each engine executes with that directory as its cwd (Docker Compose semantics).
    """
    target = Path(config_target).resolve()

    if target.is_dir():
        # Look for harness.toml or coordinator.toml inside the workspace directory
        if (target / "harness.toml").is_file():
            toml_path = target / "harness.toml"
        elif (target / "coordinator.toml").is_file():
            toml_path = target / "coordinator.toml"
        else:
            raise FileNotFoundError(
                f"No 'harness.toml' or 'coordinator.toml' found in workspace directory: {target}"
            )
        workspace_dir = target
    elif target.is_file():
        toml_path = target
        workspace_dir = target.parent
    else:
        raise FileNotFoundError(f"Coordinator config path not found: {target}")

    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    coordinator_section = data.get("coordinator", {})
    global_show_directives = coordinator_section.get("show_directives", False)

    coordinator = HarnessCoordinator()

    engines_data = data.get("engines", [])

    # Support either [[engines]] (list) or [engines.<name>] (dict)
    engine_specs: List[Dict[str, Any]] = []
    if isinstance(engines_data, list):
        engine_specs = engines_data
    elif isinstance(engines_data, dict):
        for name, spec in engines_data.items():
            if isinstance(spec, dict):
                spec_copy = dict(spec)
                spec_copy.setdefault("name", name)
                engine_specs.append(spec_copy)

    for spec in engine_specs:
        raw_cmd = spec.get("command")
        if not raw_cmd:
            raise ValueError(f"Engine spec missing required 'command': {spec}")

        if isinstance(raw_cmd, str):
            cmd_list = shlex.split(raw_cmd)
        elif isinstance(raw_cmd, list):
            cmd_list = [str(x) for x in raw_cmd]
        else:
            raise TypeError(f"Engine command must be string or list of strings, got {type(raw_cmd)}")

        # Resolve relative script paths against workspace_dir (Docker Compose style)
        if cmd_list:
            candidate = workspace_dir / cmd_list[0]
            if candidate.is_file():
                cmd_list[0] = str(candidate.resolve())

        name = spec.get("name", cmd_list[0] if cmd_list else "engine")
        attach_stdin = spec.get("attach_stdin", False)
        show_directives = spec.get("show_directives", global_show_directives)
        # Custom cwd if specified, otherwise defaults to workspace_dir
        engine_cwd = spec.get("cwd", str(workspace_dir))

        engine = HarnessEngine(
            child_cmd=cmd_list,
            name=name,
            attach_stdin=attach_stdin,
            show_directives=show_directives,
            cwd=engine_cwd,
            bus=coordinator.bus,
            shared_variables=coordinator.shared_variables,
        )
        coordinator.add_engine(engine)

    return coordinator
