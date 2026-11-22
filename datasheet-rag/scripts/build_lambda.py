"""Build the Lambda packages: build/lambda.zip (the API function) and build/ingest.zip (the ingest function, step 9a),
each with handler.py, the rag/ modules it needs, and Linux wheels; and build/mcp.zip (the MCP server function
`datasheet-mcp`, Project 3 Layer 7) with mcp_handler.py, datasheet_mcp/, its rag/ modules and Linux wheels.

Run from datasheet-rag:  python scripts\\build_lambda.py

pip downloads the Linux (manylinux, x86_64, CPython 3.14) builds of psycopg-binary and boto3, not the Windows ones
in .venv. boto3 is packed too, so the version the tests ran with is the one Lambda runs. Nothing is uploaded: the
zip is put into the function by hand in the console.
"""
import ast
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
STAGE = BUILD / "lambda"
ZIP = BUILD / "lambda.zip"
INGEST_STAGE = BUILD / "ingest"
INGEST_ZIP = BUILD / "ingest.zip"

PYTHON_VERSION = "3.14"  # the Lambda runtime python3.14
PLATFORMS = ("manylinux2014_x86_64", "manylinux_2_28_x86_64")  # the x86_64 runtime is Amazon Linux 2023, glibc 2.34
# Packed with the pins of requirements.txt, so Lambda runs the versions the tests ran with.
PACKAGES = ("psycopg[binary]", "boto3")
# The modules the query path imports (handler -> api -> pipeline -> retrieve -> search, rerank; pipeline ->
# generate; api -> kb, uploads). Not parse, chunk, load, ingest, server or eval: they need pdfplumber or the PDFs.
RAG_MODULES = ("__init__", "api", "chunk_base", "config", "corrections", "costlog", "db", "embed", "generate", "kb",
               "pipeline", "records", "rerank", "retrieve", "review", "search", "uploads", "usage")
# Imported inside handler.ingest_handler only, which runs in the ingest function, never in the API function.
LAZY_MODULES = ("ingest",)
# The ingest function (step 9a) also parses and chunks: pdfplumber and the Layer 1 to 3 modules.
INGEST_PACKAGES = (*PACKAGES, "pdfplumber")
INGEST_MODULES = (*RAG_MODULES, "chunk", "chunk_tables", "chunk_text", "ingest", "load", "parse")
# The MCP function (Project 3 Layer 7). pip also brings what mcp needs (starlette, uvicorn, pydantic-core, ...).
MCP_STAGE = BUILD / "mcp"
MCP_ZIP = BUILD / "mcp.zip"
MCP_PACKAGES = (*PACKAGES, "mcp", "pydantic", "pyjwt[crypto]", "cryptography", "mangum")
# The modules datasheet_mcp imports (tools -> embed, rerank; server -> db, retrieve -> search; limits -> usage).
MCP_MODULES = ("__init__", "chunk_base", "config", "costlog", "db", "embed", "rerank", "retrieve", "search", "usage")
ENTRIES = ("handler.py",)
MCP_ENTRIES = ("mcp_handler.py", *sorted(f"datasheet_mcp/{path.name}" for path in (ROOT / "datasheet_mcp").glob("*.py")))
MAX_ZIP_MB = 50  # the console's direct upload limit
MAX_UNZIPPED_MB = 250  # Lambda's limit for a .zip package


def rag_imports(path: Path) -> set[str]:
    """The rag modules a file imports, read from its source."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "rag":
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("rag."):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {alias.name.split(".")[1] for alias in node.names if alias.name.startswith("rag.")}
    return found


def check_modules(modules=RAG_MODULES, allowed=LAZY_MODULES, entries=ENTRIES) -> None:
    """Stop when a packed file (an entry file or a packed rag module) imports a rag module that is not packed
    (`allowed` names the lazy imports that run only in the other function)."""
    files = [ROOT / entry for entry in entries] + [ROOT / "rag" / f"{name}.py" for name in modules]
    missing = {(path.name, name) for path in files for name in rag_imports(path)
               if name not in modules and name not in allowed}
    if missing:
        sys.exit(f"Packed files import rag modules that are not packed: {sorted(missing)}. Add them to the list.")


def pinned(packages=PACKAGES, requirements: Path = ROOT / "requirements.txt") -> list[str]:
    """The requirements.txt lines of `packages`, for example "boto3==1.43.111". Stops when one is missing or
    unpinned."""
    # A comment after the pin ("mcp==2.3.0  # why") is cut off: pip would read it as part of the requirement.
    pins = (line.split("#", 1)[0].strip() for line in requirements.read_text(encoding="utf-8").splitlines())
    lines = {pin.split("==")[0].strip(): pin for pin in pins if "==" in pin}
    missing = [name for name in packages if name not in lines]
    if missing:
        sys.exit(f"requirements.txt has no pinned line (name==version) for {missing}.")
    return [lines[name] for name in packages]


# The Lambda's environment, for the conditions (markers) in package requirements, such as
# `pywin32; sys_platform == "win32"`. pip's --platform picks Linux wheels but checks markers against this Windows
# machine, so for mcp (which has such a marker) it asks for pywin32, which has no Linux build.
# Every marker variable is set, so none is filled in from this machine. The exact 3.14.x and kernel of the Lambda
# runtime are not known here: ".0" and empty release strings stand in for them.
LINUX_MARKERS = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                 "platform_machine": "x86_64", "platform_release": "", "platform_version": "",
                 "implementation_name": "cpython", "implementation_version": f"{PYTHON_VERSION}.0",
                 "platform_python_implementation": "CPython", "python_version": PYTHON_VERSION,
                 "python_full_version": f"{PYTHON_VERSION}.0"}
VENV = ROOT / ".venv"


def linux_closure(requirements: list[str]) -> list[str]:
    """`name==version` for each of `requirements` and everything they need on Linux, at the versions installed in
    this venv (the ones the tests ran with). Stops when a package is missing from the venv or its installed version
    does not satisfy what asks for it."""
    from importlib import metadata

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pins, expanded = {}, set()
    todo = [Requirement(line) for line in requirements]
    while todo:
        requirement = todo.pop()
        key = canonicalize_name(requirement.name)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            sys.exit(f"{requirement.name} is needed on Linux but is not installed in .venv; pip install it first.")
        if not requirement.specifier.contains(dist.version, prereleases=True):
            sys.exit(f"{requirement} is asked for, but .venv has {requirement.name} {dist.version}.")
        pins[key] = f"{dist.metadata['Name']}=={dist.version}"
        for extra in ("", *sorted(requirement.extras)):
            if (key, extra) in expanded:
                continue
            expanded.add((key, extra))
            for line in dist.requires or []:
                dep = Requirement(line)
                # A dependency without a marker is taken once (with no extra); one with a marker when it holds.
                wanted = (not extra) if dep.marker is None else dep.marker.evaluate({**LINUX_MARKERS, "extra": extra})
                if wanted:
                    todo.append(dep)
    return sorted(pins.values(), key=str.lower)


def size_mb(paths) -> float:
    return sum(path.stat().st_size for path in paths if path.is_file()) / 1_000_000


def build(stage: Path, zip_path: Path, packages, modules, entries=ENTRIES, from_venv: bool = False) -> None:
    """Install the Linux wheels of `packages` and copy the `entries` files and the rag `modules` into `stage`,
    then zip it.

    from_venv: install the exact list from `linux_closure` with --no-deps, instead of letting pip resolve the
    dependencies (needed for mcp; see LINUX_MARKERS)."""
    print(f"Packing {zip_path.name}:", ", ".join(pinned(packages)))
    stage.mkdir(parents=True)
    platforms = [arg for platform in PLATFORMS for arg in ("--platform", platform)]
    requirements = pinned(packages)
    extra_args = []
    if from_venv:
        if Path(sys.prefix).resolve() != VENV.resolve():
            sys.exit(f"Run this with {VENV}\\Scripts\\python.exe: the MCP zip takes its versions from .venv, "
                     f"and this is {sys.prefix}.")
        requirements = linux_closure(requirements)
        extra_args = ["--no-deps"]
        print(f"  {len(requirements)} packages at the .venv versions: {', '.join(requirements)}")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--target", str(stage), "--implementation", "cp", "--python-version", PYTHON_VERSION,
                    "--only-binary=:all:", *platforms, *extra_args, *requirements], check=True)

    for entry in entries:
        (stage / entry).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / entry, stage / entry)
    (stage / "rag").mkdir()
    for name in modules:
        shutil.copy2(ROOT / "rag" / f"{name}.py", stage / "rag" / f"{name}.py")

    files = sorted(path for path in stage.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(stage).as_posix())

    unzipped, zipped = size_mb(files), zip_path.stat().st_size / 1_000_000
    libpq = [path.name for path in files if path.name.startswith("libpq")]
    print(f"{zip_path}\n  {len(files)} files, {zip_path.stat().st_size:,} bytes ({zipped:.1f} MB) zipped, "
          f"{unzipped:.1f} MB unzipped")
    print(f"  Linux libpq: {', '.join(libpq) or 'NOT FOUND'}")
    if not libpq:
        sys.exit("psycopg-binary's Linux library is missing from the package.")
    if zipped > MAX_ZIP_MB or unzipped > MAX_UNZIPPED_MB:
        sys.exit(f"Too large for a console upload ({MAX_ZIP_MB} MB zipped, {MAX_UNZIPPED_MB} MB unzipped).")


def main() -> None:
    check_modules()
    check_modules(INGEST_MODULES, allowed=())
    check_modules(MCP_MODULES, allowed=(), entries=MCP_ENTRIES)
    if BUILD.exists():
        shutil.rmtree(BUILD)
    build(STAGE, ZIP, PACKAGES, RAG_MODULES)
    build(INGEST_STAGE, INGEST_ZIP, INGEST_PACKAGES, INGEST_MODULES)
    build(MCP_STAGE, MCP_ZIP, MCP_PACKAGES, MCP_MODULES, entries=MCP_ENTRIES, from_venv=True)


if __name__ == "__main__":
    main()
