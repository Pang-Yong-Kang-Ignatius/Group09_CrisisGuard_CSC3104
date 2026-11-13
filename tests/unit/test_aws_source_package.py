"""Git-independent AWS source packaging and archive safety checks."""

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import zipfile

import pytest

from infrastructure.aws import package_source
from infrastructure.aws.launch_lab import check_archive


ROOT = Path(__file__).resolve().parents[2]


def test_launcher_build_contexts_contain_dockerfile_copy_sources():
    dockerfile = ROOT / "infrastructure/aws/launcher.Dockerfile"
    sources = [shlex.split(line)[1] for line in dockerfile.read_text().splitlines()
               if line.startswith("COPY ")]
    for entrypoint in (ROOT / ".github/workflows/ci.yml", ROOT / "infrastructure/aws/run-lab.ps1"):
        commands = [line.strip().removeprefix("& ") for line in entrypoint.read_text().splitlines()
                    if "docker build " in line and "launcher.Dockerfile" in line]
        assert commands, f"No launcher build command in {entrypoint.name}"
        for command in commands:
            context = ROOT / shlex.split(command)[-1]
            assert (context / ".dockerignore").is_file()
            for source in sources:
                assert (context / source).is_file(), f"{entrypoint.name}: {source} missing from {context}"


def extracted_source(tmp_path):
    """Create a GitHub-style source ZIP with no .git directory."""
    zip_path = tmp_path / "CrisisGuard-main.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        for name in (*package_source.SOURCE_FILES, "infrastructure/aws/.dockerignore", "infrastructure/aws/run-lab.ps1",
                     "infrastructure/aws/launcher.Dockerfile", "infrastructure/aws/launch_lab.py",
                     "infrastructure/aws/package_source.py"):
            bundle.write(ROOT / name, arcname="CrisisGuard-main/" + name)
    with zipfile.ZipFile(zip_path) as bundle:
        bundle.extractall(tmp_path / "download with spaces")
    return tmp_path / "download with spaces" / "CrisisGuard-main"


def test_zip_source_is_reproducible_and_excludes_private_files(tmp_path):
    source = extracted_source(tmp_path)
    assert not (source / ".git").exists()
    (source / ".env").write_text("PRIVATE=do-not-package\n")
    (source / "infrastructure/aws/cloud.env").write_text("PRIVATE=do-not-package\n")
    (source / "dashboard/private.py").write_text("PRIVATE=do-not-package\n")
    (source / "cloud/hospital.db").write_bytes(b"private database")
    (source / ".venv").mkdir()
    (source / ".venv/secret.txt").write_text("do-not-package")
    first, second = tmp_path / "first.tar", tmp_path / "second.tar"
    one = package_source.package(source, first)
    two = package_source.package(source, second)
    assert one["sha256"] == two["sha256"]
    assert first.read_bytes() == second.read_bytes()
    assert one["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_name("first.tar.sha256").read_text() == f"{one['sha256']}  first.tar\n"
    with tarfile.open(first) as bundle:
        assert set(bundle.getnames()) == set(package_source.SOURCE_FILES)
        assert all(member.mtime == 0 and member.uid == 0 and member.gid == 0 for member in bundle)
    assert check_archive(first) == one["sha256"]


def test_missing_required_file_fails_before_creating_archive(tmp_path):
    source = extracted_source(tmp_path)
    (source / "infrastructure/aws/bootstrap_lab.sh").unlink()
    archive = tmp_path / "source.tar"
    with pytest.raises(ValueError, match="bootstrap_lab.sh"):
        package_source.package(source, archive)
    assert not archive.exists()


def test_backup_tooling_cannot_be_omitted_from_aws_source(tmp_path):
    helper = "infrastructure/scripts/database_backup.py"
    assert helper in package_source.SOURCE_FILES
    source = extracted_source(tmp_path)
    (source / helper).unlink()
    archive = tmp_path / "missing-backup-tool.tar"
    with pytest.raises(ValueError, match="database_backup.py"):
        package_source.package(source, archive)
    assert not archive.exists()

    with tarfile.open(archive, "w") as bundle:
        for name in package_source.SOURCE_FILES:
            if name != helper:
                bundle.add(ROOT / name, arcname=name)
    with pytest.raises(ValueError, match="missing required deployment files"):
        check_archive(archive)


def test_current_checkout_can_package_without_git_metadata(tmp_path):
    archive = tmp_path / "source.tar"
    result = package_source.package(ROOT, archive)
    assert result["files"] == len(package_source.SOURCE_FILES)
    assert check_archive(archive) == result["sha256"]


@pytest.mark.skipif(os.name != "nt", reason="PowerShell wrapper requires Windows")
def test_powershell_package_command_accepts_zip_without_git(tmp_path):
    source = extracted_source(tmp_path)
    command = r'''
function docker {
    if ($args[0] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($args[0] -eq 'run' -and $args -contains 'package_source.py') {
        $archive = Join-Path (Get-Location).Path '.venv/aws-launcher/source.tar'
        [IO.File]::WriteAllText($archive, 'fixture')
        $sha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        $global:LASTEXITCODE = 0
        return ('{"sha256":"' + $sha + '"}')
    }
    throw 'Unexpected Docker operation'
}
& .\infrastructure\aws\run-lab.ps1 -Command package
if (-not $?) { exit 1 }
'''
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Source SHA-256:" in result.stdout
    assert (source / ".venv/aws-launcher/source.tar").is_file()
