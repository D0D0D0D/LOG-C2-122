"""PB — vendored part packages must resolve without the editable-install shim.

`pip install -e .` puts `<repo>/src` on sys.path, which silently makes a
top-level absolute import inside a vendored part package (`from output_envelope
.envelope_node import ...` living at `src/output_envelope/`) resolve. Any
deployment path that does NOT editable-install the repo then fails at import
time with ModuleNotFoundError — a runtime blocker that CI green and local pytest
both miss.

Two independent checks:
  1. AST scan — no part package imports itself by absolute top-level name.
  2. Subprocess import with the `<repo>/src` shim stripped from sys.path.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# Vendored common-part packages (each has a `# PART:` marker in at least one module).
PART_PACKAGES = ("cat2_graph_skeleton", "evidence_gate", "output_envelope", "payload_store")


def _part_package_dirs() -> list[Path]:
    return [SRC / name for name in PART_PACKAGES if (SRC / name).is_dir()]


def test_part_packages_do_not_self_import_by_absolute_name() -> None:
    """A part package must reference its own modules relatively, not top-level."""
    offenders: list[str] = []
    for pkg_dir in _part_package_dirs():
        pkg_name = pkg_dir.name
        for py in pkg_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                # `from <pkg>[.x] import ...` with level == 0 is an absolute self-import.
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    root = node.module.split(".")[0]
                    if root == pkg_name:
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno} -> from {node.module}")
                # `import <pkg>[.x]`
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == pkg_name:
                            offenders.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno} -> import {alias.name}")
    assert not offenders, (
        "vendored part packages must use relative imports for their own modules "
        "(absolute self-import only resolves via the editable-install src shim): " + "; ".join(offenders)
    )


def test_consumers_do_not_import_parts_as_top_level_packages() -> None:
    """src/ code must reference parts as `src.<part>`, not bare top-level."""
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        if py.parent.name in PART_PACKAGES:
            continue  # the packages themselves are covered by the relative-import test
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in PART_PACKAGES:
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno} -> from {node.module}")
    assert not offenders, (
        "import vendored parts as `src.<part>` so they resolve without the editable src shim: "
        + "; ".join(offenders)
    )


@pytest.mark.parametrize("module", ["src.graph.graph", "src.api.server"])
def test_agent_imports_without_editable_src_shim(module: str) -> None:
    """Import the agent in a subprocess whose sys.path has no `<repo>/src` entry."""
    program = textwrap.dedent(
        f"""
        import sys
        shim = {str(SRC)!r}
        sys.path = [p for p in sys.path if p.rstrip("/") != shim.rstrip("/")]
        sys.path.insert(0, {str(REPO_ROOT)!r})
        import {module}  # noqa: F401
        print("OK")
        """
    )
    proc = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"{module} failed to import without the editable src shim:\n{proc.stderr}"
    )
