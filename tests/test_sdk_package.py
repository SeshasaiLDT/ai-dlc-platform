"""SDK packaging: build, wheel contents, standalone install, dependency boundary."""

import ast
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk" / "agent-harness"
sys.path.insert(0, str(SDK))
import build_sdk  # noqa: E402

pytest.importorskip("build")
CORE_DEPENDENCY_MODULES = (
    "pydantic",
    "pydantic_core",
    "annotated_types",
    "typing_extensions",
    "typing_inspection",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rpds",
    "attrs",
    "attr",
)


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("sdk-dist")
    isolated = importlib.util.find_spec("setuptools") is None
    built = build_sdk.build(out, isolation=isolated)
    return {
        "wheel": next(p for p in built if p.suffix == ".whl"),
        "sdist": next(p for p in built if p.name.endswith(".tar.gz")),
    }


def wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def test_artifacts_are_versioned_independently(artifacts: dict[str, Path]) -> None:
    project = tomllib.loads((SDK / "pyproject.toml").read_text())["project"]
    assert project["name"] == "ai-dlc-agent-harness" and project["version"] == "0.1.0"
    assert artifacts["wheel"].name.startswith("ai_dlc_agent_harness-0.1.0-")
    platform = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert platform["name"] == "ai-dlc-platform" and platform["version"] == "0.1.0"
    from ai_dlc.application.agent_harness import INTERFACE_VERSION

    assert INTERFACE_VERSION == "1.5.0"  # SDK version and interface version are independent


def test_wheel_contents_include_boundary_and_exclude_everything_else(
    artifacts: dict[str, Path],
) -> None:
    names = wheel_names(artifacts["wheel"])
    for expected in (
        "ai_dlc/application/agent_harness/__init__.py",
        "ai_dlc/application/agent_harness/resilience.py",
        "ai_dlc/application/agent_harness/context_budget.py",
        "ai_dlc/domain/identity/models.py",
        "ai_dlc/domain/authorization.py",
        "ai_dlc/domain/approval.py",
    ):
        assert expected in names
    shared_namespace_inits = {
        "ai_dlc/__init__.py",
        "ai_dlc/application/__init__.py",
        "ai_dlc/domain/__init__.py",
    }
    assert not names & shared_namespace_inits
    forbidden = (
        "authorization/",
        "approval/",
        "gateway",
        "adapters",
        "tool_policy",
        "initiative",
        "tests",
        "examples",
        "infrastructure",
    )
    assert not [n for n in names if n.startswith("ai_dlc/") and any(f in n for f in forbidden)]
    assert not [n for n in names if n.startswith(("tests", "examples", "sdk"))]


def test_wheel_metadata_separates_core_and_optional_dependencies(
    artifacts: dict[str, Path],
) -> None:
    with zipfile.ZipFile(artifacts["wheel"]) as archive:
        meta = archive.read(next(n for n in archive.namelist() if n.endswith("METADATA"))).decode()
    requires = [
        line.split(": ", 1)[1] for line in meta.splitlines() if line.startswith("Requires-Dist")
    ]
    core = [r for r in requires if "extra ==" not in r]
    assert sorted(re.split(r"[<>=!~ ]", r)[0] for r in core) == ["jsonschema", "pydantic"]
    extra = [r for r in requires if 'extra == "a2a"' in r]
    assert any(r.startswith("a2a-sdk") for r in extra) and any(r.startswith("httpx") for r in extra)
    assert "Provides-Extra: a2a" in meta and "boto3" not in meta.lower()
    assert not any("PyYAML" in r for r in requires)


def test_sdist_contains_sources_and_build_config(artifacts: dict[str, Path]) -> None:
    import tarfile

    with tarfile.open(artifacts["sdist"]) as archive:
        names = archive.getnames()
    assert any(n.endswith("/pyproject.toml") for n in names)
    assert any(n.endswith("src/ai_dlc/application/agent_harness/mcp.py") for n in names)
    assert not any("/tests/" in n or "approval/service" in n for n in names)


def _standalone_env(tmp_path: Path, wheel: Path) -> Path:
    env = tmp_path / "env"
    venv.EnvBuilder(with_pip=True, clear=True).create(env)
    python = env / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", "-q", str(wheel)],
        check=True,
    )
    # Offline stand-in for resolving the declared core dependencies: expose ONLY those packages.
    site = Path(
        subprocess.run(
            [str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    deps = tmp_path / "core-deps"
    deps.mkdir()
    for name in CORE_DEPENDENCY_MODULES:
        spec = importlib.util.find_spec(name)
        origin = Path(spec.origin)
        target = origin.parent if origin.name == "__init__.py" else origin
        (deps / target.name).symlink_to(target)
    (site / "core-deps.pth").write_text(str(deps) + "\n")
    return python


def test_standalone_install_imports_and_runs_reference_agent(
    artifacts: dict[str, Path], tmp_path: Path
) -> None:
    python = _standalone_env(tmp_path, artifacts["wheel"])
    probe = """
import importlib.util, json, sys
import ai_dlc.application.agent_harness as h
assert h.INTERFACE_VERSION == "1.5.0"
assert len(h.RoleProfiles.defaults().profiles) == 4
for name in ("ai_dlc.application.authorization", "ai_dlc.application.approval",
             "ai_dlc.application.gateway", "ai_dlc.adapters", "yaml", "a2a", "httpx"):
    try:
        found = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        found = None
    assert found is None, name
assert "ai_dlc.application.authorization" not in sys.modules
try:
    h.A2AClient
except ImportError as error:
    assert "[a2a]" in str(error)
else:
    raise SystemExit("A2A must be optional")
print(json.dumps(sorted(n for n in h.__all__ if n != "A2AClient")[:3]))
"""
    done = subprocess.run([str(python), "-c", probe], capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    demo = subprocess.run(
        [str(python), str(SDK / "examples" / "reference_agent.py")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert demo.returncode == 0, demo.stderr
    assert json.loads(demo.stdout)["status"] == "succeeded"


ALLOWED_PLATFORM_IMPORTS = (
    "ai_dlc.application.agent_harness",
    "ai_dlc.domain.identity",
    "ai_dlc.domain.authorization",
    "ai_dlc.domain.approval",
)
OPTIONAL_ONLY_IN_A2A = ("a2a", "httpx", "google")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_extraction_boundary_and_optional_dependency_isolation() -> None:
    files = [
        path
        for entry in build_sdk.INCLUDE
        for path in (
            (build_sdk.SRC / entry).rglob("*.py")
            if (build_sdk.SRC / entry).is_dir()
            else [build_sdk.SRC / entry]
        )
    ]
    assert files
    for path in files:
        for module in _imports(path):
            if module.startswith("ai_dlc"):
                assert module.startswith(ALLOWED_PLATFORM_IMPORTS), (path.name, module)
            top = module.split(".")[0]
            if top in OPTIONAL_ONLY_IN_A2A:
                assert path.name == "a2a.py", (path.name, module)
            assert top not in {"yaml", "boto3", "botocore"}, (path.name, module)


def test_backward_compatible_import_paths_are_identical_objects() -> None:
    import ai_dlc.application.agent_harness.mcp as mcp
    import ai_dlc.application.approval as approval
    import ai_dlc.application.authorization as authorization
    import ai_dlc.domain.approval as domain_approval
    import ai_dlc.domain.authorization as domain_authorization
    from ai_dlc.application.agent_harness import RetryPolicy

    assert (
        authorization.ResolvedAuthorizationContext
        is domain_authorization.ResolvedAuthorizationContext
    )
    assert authorization.LogicalScopes is domain_authorization.LogicalScopes
    assert approval.ApprovalStatus is domain_approval.ApprovalStatus
    assert mcp.RetryPolicy is RetryPolicy


def test_public_names_resolve_including_lazy_a2a() -> None:
    import ai_dlc.application.agent_harness as harness

    for name in harness.__all__:
        assert getattr(harness, name) is not None, name
    with pytest.raises(AttributeError):
        _ = harness.NotAThing
    assert len(set(harness.__all__)) == len(harness.__all__)
