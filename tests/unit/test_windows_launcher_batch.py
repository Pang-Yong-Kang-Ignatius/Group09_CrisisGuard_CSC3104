"""Exercise the Windows launcher without touching Docker or private configuration."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows CMD and PowerShell")


@pytest.fixture
def launcher(tmp_path):
    source = tmp_path / "fresh GitHub ZIP (copy)"
    source.mkdir()
    shutil.copyfile(ROOT / "start.project.bat", source / "start.project.bat")
    (source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    docker_dir = source / "infrastructure" / "docker"
    docker_dir.mkdir(parents=True)
    for name in ("compose.metaguard.yaml", "compose.dashboard-test-traffic.local.yaml"):
        (docker_dir / name).write_text("services: {}\n", encoding="utf-8")
    scripts_dir = source / "infrastructure" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "init_local_env.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "Path('initializer-called').write_text(sys.executable)\n", encoding="utf-8"
    )
    shutil.copyfile(ROOT / "infrastructure/scripts/local_outage_agent.py", scripts_dir / "local_outage_agent.py")
    venv.EnvBuilder(with_pip=False).create(source / ".venv311")
    # Only launcher tests share dependencies. The separate clean-install test
    # installs real requirements into an empty environment with pip.
    site_packages = source / ".venv311/Lib/site-packages"
    (site_packages / "launcher-dependencies.pth").write_text(
        str(Path(sys.prefix) / "Lib/site-packages") + "\n", encoding="utf-8"
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    # A .cmd shim would replace the running batch script; a renamed Python EXE
    # lets the real launcher continue while each Docker subcommand stays local.
    base_python = Path(sys._base_executable)
    shutil.copyfile(base_python, fake_bin / "docker.exe")
    shutil.copyfile(
        base_python.with_name(f"python{sys.version_info.major}{sys.version_info.minor}.dll"),
        fake_bin / f"python{sys.version_info.major}{sys.version_info.minor}.dll",
    )
    (source / "context").write_text(
        'from pathlib import Path\nPath("docker-called").touch()\n'
        'print("npipe:////./pipe/dockerDesktopLinuxEngine")\n', encoding="ascii"
    )
    (source / "info").write_text("pass\n", encoding="ascii")
    (source / "compose").write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "with Path('docker-calls.txt').open('a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('CG_FAKE_DOCKER_FAILURE') in sys.argv[1:]:\n"
        "    raise SystemExit(7)\n",
        encoding="ascii",
    )
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["PYTHONHOME"] = str(base_python.parent)
    env.pop("PYTHONPATH", None)
    env.pop("DOCKER_HOST", None)
    env.pop("CG_FAKE_DOCKER_FAILURE", None)
    return source, env


def run_launcher(source, env, shell="cmd", *arguments):
    if shell == "cmd":
        command = ["cmd.exe", "/d", "/c", ".\\start.project.bat", *arguments]
    else:
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "& .\\start.project.bat " + " ".join(arguments) + "; exit $LASTEXITCODE"]
    return subprocess.run(command, cwd=source, env=env, capture_output=True, text=True, input="\n", timeout=30)


@pytest.mark.parametrize("shell", ["cmd", "powershell"])
@pytest.mark.parametrize("docker_host", [None, "npipe:////./pipe/dockerDesktopLinuxEngine", "tcp://remote.example:2375"])
def test_launcher_verify_parses_and_uses_isolated_project(launcher, shell, docker_host):
    source, env = launcher
    if docker_host is None:
        env.pop("DOCKER_HOST", None)
    else:
        env["DOCKER_HOST"] = docker_host
    result = run_launcher(source, env, shell, "--verify")
    output = result.stdout + result.stderr
    assert "( was unexpected at this time." not in output
    assert not (source / ".env").exists()
    if docker_host and docker_host.startswith("tcp:"):
        assert result.returncode == 1, output
        assert "A remote DOCKER_HOST is not allowed" in output
        assert not (source / "initializer-called").exists()
        assert not (source / "docker-calls.txt").exists()
    else:
        assert result.returncode == 0, output
        assert "Verification startup completed for cg-launcher-" in output
        assert (source / "initializer-called").exists()
        docker_calls = (source / "docker-calls.txt").read_text()
        assert "config --quiet" in docker_calls
        assert "up -d --build --wait postgres patient emergency admin controller hospital-a hospital-b dashboard" in docker_calls
        assert docker_calls.count(" exec -T hospital-") == 2
        assert docker_calls.count(" cp hospital-") == 2
        assert Path((source / "initializer-called").read_text()) == source / ".venv311/Scripts/python.exe"


@pytest.mark.parametrize("module", ["httpx", "fastapi", "uvicorn", "pydantic"])
@pytest.mark.parametrize("verify", [False, True])
def test_missing_host_dependency_stops_before_docker(launcher, module, verify):
    source, env = launcher
    # Shadow one installed module to reproduce each missing dependency without
    # changing or uninstalling anything in the invoking environment.
    (source / (module + ".py")).write_text(
        f"raise ModuleNotFoundError(\"No module named '{module}'\")\n", encoding="utf-8"
    )
    result = run_launcher(source, env, "cmd", *(["--verify"] if verify else []))
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Host Python dependency check failed" in output
    assert f"No module named '{module}'" in output
    assert str(source / ".venv311/Scripts/python.exe") in output
    assert '-m pip install -r "' + str(source / "requirements.txt") + '"' in output
    assert "-m pip check" in output
    assert not (source / "docker-called").exists()
    assert not (source / "initializer-called").exists()
    assert "Demo Ready" not in output


@pytest.mark.parametrize("failure", ["missing", "wrong_version", "base_python"])
def test_launcher_rejects_invalid_preferred_environment_without_fallback(launcher, failure):
    source, env = launcher
    # Leave a legacy environment available to detect unintended fallback.
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    env["VIRTUAL_ENV"] = str(source / ".venv")
    env["PATH"] = str(source / ".venv/Scripts") + os.pathsep + env["PATH"]
    if failure == "missing":
        # Retain the fixture environment under another name.
        (source / ".venv311").rename(source / "preserved-environment")
    else:
        expression = "sys.version_info = (3, 12)" if failure == "wrong_version" else "sys.prefix = sys.base_prefix"
        (source / ".venv311/Lib/site-packages/sitecustomize.py").write_text(
            "import sys\n" + expression + "\n", encoding="utf-8"
        )
    result = run_launcher(source, env, "cmd", "--verify")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    message = "virtual environment missing" if failure == "missing" else "Python 3.11 virtual environment"
    assert message in output
    assert not (source / "docker-called").exists()
    assert not (source / "initializer-called").exists()


def test_empty_virtual_environment_reports_requirements_installation(launcher):
    source, env = launcher
    dependency_link = source / ".venv311/Lib/site-packages/launcher-dependencies.pth"
    dependency_link.rename(dependency_link.with_suffix(".disabled"))
    result = run_launcher(source, env, "cmd", "--verify")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Host Python dependency check failed" in output
    assert "-m pip install -r" in output
    assert not (source / "docker-called").exists()


def test_launcher_uses_preferred_environment_when_another_is_active(launcher):
    source, env = launcher
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    env["VIRTUAL_ENV"] = str(source / ".venv")
    env["PATH"] = str(source / ".venv/Scripts") + os.pathsep + env["PATH"]
    result = run_launcher(source, env, "cmd", "--verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path((source / "initializer-called").read_text()) == source / ".venv311/Scripts/python.exe"


@pytest.mark.parametrize("stage,message", [("config", "Docker Compose configuration is invalid"),
                                          ("up", "Docker Compose startup failed")])
def test_launcher_reports_compose_failure(launcher, stage, message):
    source, env = launcher
    env["CG_FAKE_DOCKER_FAILURE"] = stage
    result = run_launcher(source, env, "cmd", "--verify")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert message in output
    assert "Verification startup completed" not in output
    assert " exec -T " not in (source / "docker-calls.txt").read_text()


@pytest.mark.parametrize("failure", [None, "missing", "wrong_version"])
def test_powershell_test_runner_uses_same_environment(launcher, failure):
    source, env = launcher
    shutil.copyfile(ROOT / "test.project.ps1", source / "test.project.ps1")
    (source / "infrastructure/scripts/project_verify.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "(Path(__file__).resolve().parents[2] / 'test-runner-python').write_text(sys.executable)\n"
        "raise SystemExit(7)\n", encoding="utf-8"
    )
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    env["VIRTUAL_ENV"] = str(source / ".venv")
    env["PATH"] = str(source / ".venv/Scripts") + os.pathsep + env["PATH"]
    if failure == "missing":
        (source / ".venv311").rename(source / "preserved-environment")
    elif failure == "wrong_version":
        (source / ".venv311/Lib/site-packages/sitecustomize.py").write_text(
            "import sys\nsys.version_info = (3, 12)\n", encoding="utf-8"
        )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(source / "test.project.ps1"), "-Quick"],
        cwd=source.parent, env=env, capture_output=True, text=True, timeout=30,
    )
    marker = source / "test-runner-python"
    if failure:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not marker.exists()
    else:
        assert result.returncode == 7, result.stdout + result.stderr
        assert Path(marker.read_text()) == source / ".venv311/Scripts/python.exe"
