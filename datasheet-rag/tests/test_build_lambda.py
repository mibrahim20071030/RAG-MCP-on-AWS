"""Layer 7 checks of scripts/build_lambda.py that need no pip and no network: every package zip names the rag modules
its files import, and every package it installs has a pinned line in requirements.txt."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_lambda.py"
spec = importlib.util.spec_from_file_location("build_lambda", SCRIPT)
build_lambda = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_lambda)


@pytest.mark.parametrize("modules, allowed, entries", [
    (build_lambda.RAG_MODULES, build_lambda.LAZY_MODULES, build_lambda.ENTRIES),
    (build_lambda.INGEST_MODULES, (), build_lambda.ENTRIES),
    (build_lambda.MCP_MODULES, (), build_lambda.MCP_ENTRIES),
])
def test_each_zip_packs_the_rag_modules_its_files_import(modules, allowed, entries):
    build_lambda.check_modules(modules, allowed=allowed, entries=entries)  # sys.exit on a missing module


def test_a_missing_module_stops_the_build():
    without_usage = tuple(name for name in build_lambda.MCP_MODULES if name != "usage")
    with pytest.raises(SystemExit, match="usage"):
        build_lambda.check_modules(without_usage, allowed=(), entries=build_lambda.MCP_ENTRIES)


def test_the_mcp_zip_holds_the_handler_and_every_datasheet_mcp_file():
    entries = set(build_lambda.MCP_ENTRIES)
    assert "mcp_handler.py" in entries
    package = {f"datasheet_mcp/{path.name}" for path in (SCRIPT.parent.parent / "datasheet_mcp").glob("*.py")}
    assert package and package <= entries


def test_every_mcp_package_is_pinned_without_its_comment():
    # pip reads each line as one requirement, so a comment after the pin must not reach it.
    lines = build_lambda.pinned(build_lambda.MCP_PACKAGES)
    assert all("==" in line and "#" not in line and " " not in line for line in lines)
    assert "mangum==0.22.0" in lines and "pyjwt[crypto]==2.15.1" in lines


def test_the_linux_closure_of_mcp_leaves_out_windows_only_packages():
    # mcp asks for pywin32 only when sys_platform == "win32"; the Lambda is Linux.
    names = {line.split("==")[0].lower() for line in build_lambda.linux_closure(["mcp==2.3.0", "pyjwt[crypto]"])}
    assert {"mcp", "starlette", "pydantic", "pyjwt", "cryptography"} <= names
    assert "pywin32" not in names


def test_the_linux_closure_stops_on_a_version_the_venv_does_not_have():
    with pytest.raises(SystemExit, match="mcp"):
        build_lambda.linux_closure(["mcp==0.0.1"])


def test_a_trailing_comment_is_cut_and_a_comment_line_is_skipped(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# mcp==9.9.9 is not this pin\nmcp==2.3.0  # why\n", encoding="utf-8")
    assert build_lambda.pinned(("mcp",), requirements) == ["mcp==2.3.0"]
