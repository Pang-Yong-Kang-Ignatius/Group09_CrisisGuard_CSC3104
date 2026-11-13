[CmdletBinding()]
param(
    [switch]$Quick,
    [switch]$Full,
    [switch]$WithDocker,
    [switch]$WithIntegration,
    [switch]$EndToEnd,
    [string]$E2EResume,
    [string]$ConfirmIsolation,
    [switch]$E2ECleanupOnly,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
if ($Help) {
    @'
CRISISGUARD PROJECT VERIFICATION
Usage: .\test.project.ps1 [-Quick | -Full] [-WithDocker] [-WithIntegration] [-EndToEnd]
Default/Full: safe Python/API/security and Node suites, coverage and audit.
Quick: the same regression suites without coverage/audit generation.
WithDocker: read-only local Compose/container/HTTP health checks.
WithIntegration: ONLY the isolated PostgreSQL allowlist, after isolation proof.
  Requires CG_TEST_POSTGRES_CONTAINER (see docs/testing.md).
EndToEnd: provision a unique disposable stack and run real event-flow smoke.
  Faults/cleanup require explicit confirmation of its isolation manifest.
  Resume: -EndToEnd -E2EResume <results directory> -ConfirmIsolation <project:UUID>
  Add -E2ECleanupOnly to clean an owned run without further fault scenarios.
Quick/Full never start resources. EndToEnd builds containers; no AWS/experiments.
Results: .project-test-results/<unique-run>/summary.json
Missing optional tools/checks are SKIP or NOT VERIFIED, never PASS.
'@ | Write-Output
    exit 0
}
if ($Quick -and $Full) { throw 'Choose either -Quick or -Full.' }
if ($Quick -and $EndToEnd) { throw '-EndToEnd cannot be combined with -Quick.' }
if (($E2EResume -or $ConfirmIsolation -or $E2ECleanupOnly) -and -not $EndToEnd) { throw 'E2E options require -EndToEnd.' }
if (($ConfirmIsolation -or $E2ECleanupOnly) -and -not $E2EResume) { throw 'Confirmation/cleanup requires -E2EResume for a concrete reviewed stack.' }
$python = Join-Path $PSScriptRoot '.venv311\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Missing .venv311. Run py -3.11 -m venv .venv311, then install requirements-test.txt using that environment.'
}
& $python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and sys.prefix != sys.base_prefix else 1)'
if ($LASTEXITCODE -ne 0) { throw 'A Python 3.11 virtual environment is required at .venv311.' }
$arguments = @((Join-Path $PSScriptRoot 'infrastructure\scripts\project_verify.py'))
if ($Quick) { $arguments += '--quick' }
if ($WithDocker) { $arguments += '--with-docker' }
if ($WithIntegration) { $arguments += '--with-integration' }
if ($Full) { $arguments += '--full' }
if ($EndToEnd) { $arguments += '--end-to-end' }
if ($E2EResume) { $arguments += @('--e2e-resume', $E2EResume) }
if ($ConfirmIsolation) { $arguments += @('--confirm-isolation', $ConfirmIsolation) }
if ($E2ECleanupOnly) { $arguments += '--e2e-cleanup-only' }
& $python @arguments
exit $LASTEXITCODE
