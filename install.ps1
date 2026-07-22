[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Get-Setting {
    param(
        [string]$Primary,
        [string]$Legacy,
        [string]$Default
    )

    $value = [Environment]::GetEnvironmentVariable($Primary)
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
    }
    $value = [Environment]::GetEnvironmentVariable($Legacy)
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
    }
    return $Default
}

function Require-Command {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Required command not found: $Name"
    }
    return $command.Source
}

function Assert-NativeSuccess {
    param([string]$Message)

    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

$userHome = [Environment]::GetFolderPath("UserProfile")
if ([string]::IsNullOrWhiteSpace($userHome)) {
    $userHome = $HOME
}
if ([string]::IsNullOrWhiteSpace($userHome)) {
    throw "A user home directory is required."
}

$repository = Get-Setting "CBS_REPOSITORY" "HJ_REPOSITORY" "DUBSOpenHub/copilot-builder-showcase"
$ref = Get-Setting "CBS_REF" "HJ_REF" "main"
$repositoryUrl = Get-Setting "CBS_REPOSITORY_URL" "HJ_REPOSITORY_URL" ""
$installDir = Get-Setting "CBS_INSTALL_DIR" "HJ_INSTALL_DIR" (Join-Path $userHome ".local\share\copilot-builder-showcase")
$binDir = Get-Setting "CBS_BIN_DIR" "HJ_BIN_DIR" (Join-Path $userHome ".local\bin")
$venvDir = Get-Setting "CBS_VENV_DIR" "HJ_VENV_DIR" (Join-Path $installDir ".venv")
$textualRequirement = Get-Setting "CBS_TEXTUAL_REQUIREMENT" "HJ_TEXTUAL_REQUIREMENT" "textual>=8,<9"
$skipOptionalMonitor = Get-Setting "CBS_SKIP_OPTIONAL_MONITOR" "HJ_SKIP_OPTIONAL_MONITOR" "0"

$gitPath = Require-Command "git"
$pythonPath = Require-Command "python"

& $pythonPath -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
Assert-NativeSuccess "Copilot Builder Showcase requires Python 3.11 or newer."

$gitDirectory = Join-Path $installDir ".git"
if (Test-Path -LiteralPath $gitDirectory -PathType Container) {
    Write-Host "Updating Copilot Builder Showcase..."
    & $gitPath -C $installDir fetch --quiet --depth 1 origin $ref
    Assert-NativeSuccess "Could not fetch the requested repository ref."
    & $gitPath -C $installDir checkout --quiet $ref
    Assert-NativeSuccess "Could not check out the requested repository ref."
    & $gitPath -C $installDir pull --ff-only --quiet origin $ref
    Assert-NativeSuccess "Could not update the installed checkout."
}
elseif (Test-Path -LiteralPath $installDir) {
    throw "Install directory exists but is not a Copilot Builder Showcase checkout: $installDir"
}
else {
    Write-Host "Installing Copilot Builder Showcase..."
    $parent = Split-Path -Parent $installDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null

    if (-not [string]::IsNullOrWhiteSpace($repositoryUrl)) {
        & $gitPath clone --quiet --depth 1 --branch $ref -- $repositoryUrl $installDir
        Assert-NativeSuccess "Could not clone Copilot Builder Showcase."
    }
    else {
        $gh = Get-Command "gh" -ErrorAction SilentlyContinue
        $ghReady = $false
        if ($gh) {
            & $gh.Source auth status --hostname github.com *> $null
            $ghReady = $LASTEXITCODE -eq 0
        }
        if ($ghReady) {
            & $gh.Source repo clone $repository $installDir -- --depth 1 --branch $ref
            Assert-NativeSuccess "Could not clone Copilot Builder Showcase with GitHub CLI."
        }
        else {
            & $gitPath clone --quiet --depth 1 --branch $ref -- "https://github.com/$repository.git" $installDir
            Assert-NativeSuccess "Could not clone Copilot Builder Showcase."
        }
    }
}

$enginePath = Join-Path $installDir "builder_showcase.py"
$launcherPath = Join-Path $installDir "showcase_launcher.py"
$projectConfig = Join-Path $installDir "pyproject.toml"
if (-not (Test-Path -LiteralPath $enginePath -PathType Leaf)) {
    throw "Installed checkout is missing builder_showcase.py."
}
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Installed checkout is missing showcase_launcher.py."
}
if (-not (Test-Path -LiteralPath $projectConfig -PathType Leaf)) {
    throw "Installed checkout is missing pyproject.toml."
}

Write-Host "Preparing Copilot Builder Showcase..."
& $pythonPath -m venv $venvDir
Assert-NativeSuccess "Could not create the Python virtual environment at $venvDir."

$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Virtual environment is missing its Python executable: $venvPython"
}

& $venvPython -m pip install --quiet --disable-pip-version-check --no-deps $installDir
Assert-NativeSuccess "Could not install the Windows command launchers."

if ($skipOptionalMonitor -ne "1") {
    Write-Host "Adding the optional run monitor..."
    & $venvPython -m pip install --quiet --disable-pip-version-check $textualRequirement
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "The optional run monitor could not be installed. The showcase is still ready."
    }
    else {
        & $venvPython -c "import textual"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "The optional run monitor could not be loaded. The showcase is still ready."
        }
    }
}

New-Item -ItemType Directory -Force -Path $binDir | Out-Null

$commands = @(
    "showcase",
    "copilot-builder-showcase",
    "hackathon",
    "hackathon-judge"
)
foreach ($command in $commands) {
    $source = Join-Path $venvDir "Scripts\$command.exe"
    $destination = Join-Path $binDir "$command.exe"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Installed environment is missing command launcher: $source"
    }
    $legacyShim = Join-Path $binDir "$command.cmd"
    if (Test-Path -LiteralPath $legacyShim -PathType Leaf) {
        Remove-Item -Force -LiteralPath $legacyShim
    }
    Copy-Item -Force -LiteralPath $source -Destination $destination
}

Write-Host ""
Write-Host "Copilot Builder Showcase is ready."
Write-Host "   Type: showcase"
Write-Host "   Then paste project links, one per line."
Write-Host "   Practice first: showcase --demo"
Write-Host "   Compatibility aliases: hackathon, hackathon-judge"

$separator = [System.IO.Path]::DirectorySeparatorChar
$normalizedBin = [System.IO.Path]::GetFullPath($binDir).TrimEnd($separator)
$pathContainsBin = $false
foreach ($entry in ($env:PATH -split ";")) {
    if ([string]::IsNullOrWhiteSpace($entry)) {
        continue
    }
    try {
        $expandedEntry = [Environment]::ExpandEnvironmentVariables($entry.Trim())
        $normalizedEntry = [System.IO.Path]::GetFullPath($expandedEntry).TrimEnd($separator)
    }
    catch {
        continue
    }
    if ($normalizedEntry -ieq $normalizedBin) {
        $pathContainsBin = $true
        break
    }
}

if (-not $pathContainsBin) {
    Write-Host ""
    Write-Host "One final setup step for this PowerShell session:"
    Write-Host "   `$env:PATH = `"$binDir;`$env:PATH`""
    Write-Host ""
    Write-Host "Or start immediately with:"
    Write-Host "   & `"$binDir\showcase.exe`" --demo"
}
