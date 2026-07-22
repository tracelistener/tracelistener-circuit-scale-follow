param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$Version = "0.4.1"
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ReleaseRoot = Join-Path $RepositoryRoot "release-build"
$ApplicationOutput = Join-Path $ReleaseRoot "application"
$WorkOutput = Join-Path $ReleaseRoot "work"
$SpecOutput = Join-Path $ReleaseRoot "spec"
$PackageRoot = Join-Path $ReleaseRoot "package"
$ArtifactRoot = Join-Path $ReleaseRoot "artifacts"
$PackageName = "CircuitScaleFollowTool-v$Version-Windows-x64"
$PackageDirectory = Join-Path $PackageRoot $PackageName
$ZipPath = Join-Path $ArtifactRoot "$PackageName.zip"
$HashPath = "$ZipPath.sha256.txt"

Push-Location $RepositoryRoot
try {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python environment not found at $Python"
    }

    foreach ($Path in @($ApplicationOutput, $WorkOutput, $SpecOutput, $PackageRoot, $ArtifactRoot)) {
        $FullPath = [System.IO.Path]::GetFullPath($Path)
        $RequiredPrefix = $RepositoryRoot.TrimEnd('\') + '\'
        if (-not $FullPath.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside the repository: $Path"
        }
        if (Test-Path -LiteralPath $Path) {
            Remove-Item -LiteralPath $Path -Recurse -Force
        }
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name CircuitScaleFollowTool `
        --paths tools `
        --collect-all keystone `
        --collect-all rtmidi `
        --distpath $ApplicationOutput `
        --workpath $WorkOutput `
        --specpath $SpecOutput `
        tools\circuit_scale_follow_tool.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    New-Item -ItemType Directory -Path $PackageDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $ApplicationOutput "CircuitScaleFollowTool.exe") -Destination $PackageDirectory
    Copy-Item -LiteralPath "WINDOWS_TOOL.md" -Destination $PackageDirectory
    Copy-Item -LiteralPath "LICENSE" -Destination $PackageDirectory
    Copy-Item -LiteralPath "NOTICE.md" -Destination $PackageDirectory

    Compress-Archive -LiteralPath $PackageDirectory -DestinationPath $ZipPath -CompressionLevel Optimal
    $Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $PackageName.zip" | Set-Content -LiteralPath $HashPath -Encoding ascii

    Write-Host "Release package: $ZipPath"
    Write-Host "SHA-256: $Hash"
    Write-Host "Checksum file: $HashPath"
}
finally {
    Pop-Location
}
