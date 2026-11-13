param(
    [ValidateSet('preview', 'package', 'plan', 'launch', 'status', 'stop', 'resume', 'cleanup-plan')]
    [string]$Command = 'preview',
    [string]$SshCidr = '',
    [string]$AccountId = '',
    [string]$Stack = 'crisisguard',
    [ValidateSet('us-east-1', 'us-west-2')][string]$Region = 'us-east-1',
    [string]$KeyName = 'vockey',
    [string]$InstanceProfile = 'LabInstanceProfile',
    [string]$SubnetId = '',
    [Alias('Profile')][string]$AwsProfile = 'crisisguard-lab',
    [string]$ExpectedAccount = '565337149326',
    [string]$SshKeyFile = '',
    [string]$OutputFile = '',
    [string]$CredentialsFile = (Join-Path $env:USERPROFILE '.aws\credentials')
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repoRoot
$reportStream = $null
try {
    if ($OutputFile) {
        if ($Command -ne 'cleanup-plan') { throw '-OutputFile is supported only for cleanup-plan.' }
        # Reserve before Docker/AWS access. Never replace another run's inventory.
        $reportPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)
        $reportStream = [System.IO.File]::Open($reportPath, [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    }
    $runtimeArgs = @('run', '--rm', '-e', 'AWS_EC2_METADATA_DISABLED=true')
    if ($Command -eq 'launch') {
        if (-not $SshKeyFile -or -not (Test-Path -LiteralPath $SshKeyFile -PathType Leaf)) {
            throw 'Supply -SshKeyFile for your EC2 key pair in unencrypted OpenSSH/PEM format.'
        }
        $keyPath = (Resolve-Path -LiteralPath $SshKeyFile).Path
        $runtimeArgs += @('--mount', "type=bind,source=$keyPath,target=/run/operator-key,readonly")
    }
    if ($Command -notin @('preview', 'package')) {
        if (-not (Test-Path -LiteralPath $CredentialsFile -PathType Leaf)) {
            throw "Create or select a temporary Learner Lab credentials file with the named profile; see docs/runbooks/AWS_LAUNCHER.md."
        }
        $credentialPath = (Resolve-Path -LiteralPath $CredentialsFile).Path
        $runtimeArgs += @('--mount', "type=bind,source=$credentialPath,target=/run/aws-credentials,readonly", '-e', 'AWS_SHARED_CREDENTIALS_FILE=/run/aws-credentials')
    }
    if ($Command -in @('preview', 'plan', 'launch') -and -not $SshCidr) {
        throw 'Supply -SshCidr YOUR_PUBLIC_IP/32.'
    }
    if ($Command -in @('launch', 'stop', 'resume') -and -not $AccountId) {
        throw 'Supply -AccountId using the account printed by plan/status.'
    }
    foreach ($required in @('infrastructure/aws/.dockerignore', 'infrastructure/aws/launcher.Dockerfile',
            'infrastructure/aws/launch_lab.py', 'infrastructure/aws/package_source.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $required) -PathType Leaf)) {
            throw "Required launcher file is missing: $required"
        }
    }
    & docker build -f infrastructure/aws/launcher.Dockerfile -t crisisguard-aws-launcher:dev infrastructure/aws
    if ($LASTEXITCODE -ne 0) { throw 'Launcher image build failed; check Docker Desktop is running.' }
    if ($Command -in @('package', 'plan', 'launch')) {
        $bundleDir = Join-Path $repoRoot '.venv\aws-launcher'
        New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null
        $archivePath = Join-Path $bundleDir 'source.tar'
        $packageOutput = & docker run --rm --entrypoint python `
            --mount "type=bind,source=$repoRoot,target=/repo,readonly" `
            --mount "type=bind,source=$bundleDir,target=/bundle" `
            crisisguard-aws-launcher:dev -u package_source.py --source /repo --output /bundle/source.tar
        if ($LASTEXITCODE -ne 0) { throw 'Source packaging failed; no AWS resources were changed.' }
        $packageResult = ($packageOutput -join [Environment]::NewLine) | ConvertFrom-Json
        $actualSha = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($packageResult.sha256 -ne $actualSha) { throw 'Source archive checksum mismatch.' }
        Write-Output "Source archive: $archivePath"
        Write-Output "Source SHA-256: $actualSha"
        if ($Command -eq 'package') { return }
        $runtimeArgs += @('--mount', "type=bind,source=$archivePath,target=/source.tar,readonly")
    }
    $runtimeArgs += @('crisisguard-aws-launcher:dev', $Command, '--stack', $Stack,
        '--region', $Region, '--key-name', $KeyName, '--instance-profile', $InstanceProfile,
        '--profile', $AwsProfile, '--expected-account', $ExpectedAccount)
    if ($Command -eq 'launch') { $runtimeArgs += @('--ssh-key', '/run/operator-key') }
    if ($SshCidr) { $runtimeArgs += @('--ssh-cidr', $SshCidr) }
    if ($AccountId) { $runtimeArgs += @('--confirm-account', $AccountId) }
    if ($SubnetId) { $runtimeArgs += @('--subnet-id', $SubnetId) }
    if ($Command -eq 'cleanup-plan') {
        $inventory = & docker @runtimeArgs
        if ($LASTEXITCODE -ne 0) { throw 'Inventory failed. Inspect the selected stacks; no cleanup was performed.' }
        $parsedInventory = ($inventory -join [Environment]::NewLine) | ConvertFrom-Json
        if ($parsedInventory.result -ne 'inventory_only' -or $parsedInventory.changes_made -ne $false) {
            throw 'Unexpected cleanup inventory response.'
        }
        if ($null -ne $reportStream) {
            $reportBytes = [System.Text.Encoding]::UTF8.GetBytes(($inventory -join [Environment]::NewLine) + [Environment]::NewLine)
            $reportStream.Write($reportBytes, 0, $reportBytes.Length)
        }
        Write-Output $inventory
    } else {
        & docker @runtimeArgs
        if ($LASTEXITCODE -ne 0) { throw 'Launcher failed. Inspect output and existing AWS resources before retrying.' }
    }
} finally {
    if ($null -ne $reportStream) { $reportStream.Dispose() }
    Pop-Location
}
