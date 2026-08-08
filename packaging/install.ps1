<#
.SYNOPSIS
    Install chip-agent from an extracted release bundle.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -InstallDir "D:\tools\chip-agent"

.NOTES
    Uninstall: Remove-Item -Recurse -Force <InstallDir>, then drop it from PATH.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\chip-agent"),
    [switch]$NoPathUpdate
)

$ErrorActionPreference = "Stop"

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Executable = Join-Path $SourceDir "chip-agent.exe"

if (-not (Test-Path $Executable)) {
    Write-Error "chip-agent.exe not found next to this script. Run install.ps1 from inside the extracted bundle folder."
}

Write-Host "Installing chip-agent -> $InstallDir"

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Copy-Item -Path (Join-Path $SourceDir "*") -Destination $InstallDir -Recurse -Force
Remove-Item (Join-Path $InstallDir "install.ps1") -ErrorAction SilentlyContinue

# Files fetched via a browser carry a zone marker that can trip SmartScreen.
Get-ChildItem -Path $InstallDir -Recurse -File |
    Unblock-File -ErrorAction SilentlyContinue

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$InstallDir*") {
        $updated = if ([string]::IsNullOrEmpty($userPath)) { $InstallDir } else { "$userPath;$InstallDir" }
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
        Write-Host "Added $InstallDir to your user PATH."
        Write-Host "Open a new terminal for it to take effect."
    }
}

Write-Host ""
Write-Host "Done. Try: chip-agent --help"
Write-Host ""
Write-Host "Note: real tool execution additionally needs Docker Desktop with the"
Write-Host "pinned IIC-OSIC-TOOLS image. Tool-backed steps auto-skip without it."
