"""Resolve Windows npm Pi shims without invoking a shell or changing config."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Sequence


def resolve_pi_command(command: Sequence[str], *, windows: bool | None = None) -> tuple[str, ...]:
    """Leave custom commands alone; turn a Windows npm pi.cmd into node + bin.pi.

    Only inspect the package beside the discovered shim. Never execute/parse an
    arbitrary batch file, scan unrelated installations, or interpolate a shell.
    No environment/config overrides are made, so Pi retains its normal config.
    """
    if not command:
        raise ValueError("Pi command must be nonempty")
    command = tuple(str(part) for part in command)
    windows = os.name == "nt" if windows is None else windows
    if not windows or Path(command[0]).name.lower() not in {"pi", "pi.cmd", "pi.bat", "pi.exe"}:
        return command
    executable = shutil.which(command[0])
    if executable is None:
        raise FileNotFoundError(
            "Pi was not found on the server PATH. Install Pi/add its npm directory to PATH "
            "and restart the server, or specify --pi-cli <installed CLI JS path>."
        )
    shim = Path(executable)
    if shim.suffix.lower() == ".exe":
        return (str(shim), *command[1:])
    # npm global prefixes contain the shim and node_modules side by side.
    # Local node_modules/.bin installs have the packages one directory above.
    roots = [shim.parent / "node_modules"]
    if shim.parent.name == ".bin":
        roots.insert(0, shim.parent.parent)
    for root in roots:
        for package in ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent"):
            directory = root / package
            manifest = directory / "package.json"
            if not manifest.is_file():
                continue
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
                entry = info.get("bin", {})
                entry = entry.get("pi") if isinstance(entry, dict) else entry
                if not isinstance(entry, str) or not entry:
                    raise ValueError("package has no bin.pi")
                cli = (directory / entry).resolve()
                if not cli.is_relative_to(directory.resolve()) or not cli.is_file():
                    raise ValueError("bin.pi is missing or outside package")
            except (OSError, ValueError, AttributeError) as exc:
                raise OSError(f"Invalid Pi installation at {manifest}: {exc}") from exc
            # Match npm's preference for a node.exe next to the shim.
            local_node = shim.parent / "node.exe"
            node = str(local_node) if local_node.is_file() else shutil.which("node")
            if not node:
                raise FileNotFoundError("Pi was found, but node.exe is not on the server PATH.")
            return (node, str(cli), *command[1:])
    raise FileNotFoundError(
        f"Found Pi shim at {shim}, but could not locate its npm package/bin.pi. "
        "Specify --pi-cli <installed CLI JS path>; batch files are not run through a shell."
    )
