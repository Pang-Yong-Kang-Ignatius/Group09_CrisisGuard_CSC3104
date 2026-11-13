"""Host requirements regressions, including an opt-in real clean installation."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

import pytest


ROOT = Path(__file__).resolve().parents[2]
HOST_IMPORTS = (
    "import httpx, fastapi, uvicorn; "
    "from pydantic import BaseModel, ConfigDict; "
    "from infrastructure.scripts import local_outage_agent"
)


def test_host_requirements_are_declared_in_shared_runtime_file():
    requirements = (ROOT / "requirements.txt").read_text().splitlines()
    assert {"httpx", "fastapi", "uvicorn[standard]", "pydantic"} <= set(requirements)
    assert "-r requirements.txt" in (ROOT / "requirements-test.txt").read_text().splitlines()
    assert "coverage>=7.6,<8" in (ROOT / "requirements-test.txt").read_text().splitlines()


def test_required_host_imports_work_with_current_interpreter():
    result = subprocess.run([sys.executable, "-c", HOST_IMPORTS], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_host_launcher_python_calls_use_the_selected_interpreter():
    launcher = (ROOT / "start.project.bat").read_text()
    assert 'set "PROJECT_PYTHON=.venv311\\Scripts\\python.exe"' in launcher
    assert '"%CD%\\%PROJECT_PYTHON%" -m infrastructure.scripts.local_outage_agent' in launcher
    assert launcher.index("import httpx, fastapi, uvicorn") < launcher.index("docker context inspect")
    assert launcher.index("import httpx, fastapi, uvicorn") < launcher.index("--with-local-test-traffic")
    assert 'set "PROJECT_PYTHON=.venv\\' not in launcher
    assert "Get-Command python" not in (ROOT / "test.project.ps1").read_text()
    for line in launcher.splitlines():
        if " python -c " in line.lower():
            assert line.startswith("docker compose "), line  # Container-side Python.
        if "-m pip install" in line:
            assert line.strip().startswith("echo "), line


@pytest.mark.skipif(os.environ.get("CG_TEST_CLEAN_INSTALL") != "1",
                    reason="set CG_TEST_CLEAN_INSTALL=1 to install packages in a new venv")
def test_clean_virtual_environment_installs_declared_requirements():
    assert sys.version_info[:2] == (3, 11), "Run the clean-install regression using Python 3.11"
    # Keep this path short for Windows installations without long-path support.
    # Retain the new environment so the verified installation can be inspected.
    environment = Path(tempfile.mkdtemp(prefix="cg host "))
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_REQUIRE_VIRTUALENV"] = "true"

    def run(*arguments):
        result = subprocess.run([str(python), *arguments], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    # Prove host dependencies are absent before pip installs either file.
    run("-c", "import importlib.util; assert all(importlib.util.find_spec(name) is None "
        "for name in ('httpx', 'fastapi', 'uvicorn'))")
    run("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt")
    run("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-test.txt")
    assert "No broken requirements found" in run("-m", "pip", "check")
    run("-c", HOST_IMPORTS)
    run("-c", "import sys; assert sys.prefix != sys.base_prefix; "
        "assert sys.version_info[:2] == (3, 11)")
    print(f"Verified clean installation in {environment}")
