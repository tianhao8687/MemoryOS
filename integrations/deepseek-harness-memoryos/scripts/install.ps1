param(
    [string]$Profile = "memoryos"
)

$ErrorActionPreference = "Stop"
$dsh = Get-Command dsh -ErrorAction Stop
$pnpm = Get-Command pnpm -ErrorAction Stop
$version = (& $dsh.Source --version 2>&1 | Out-String).Trim()
if ($version -notmatch "0\.1\.0-rc\.5") {
    throw "DeepSeek Harness 0.1.0-rc.5 is required; found: $version"
}
$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packRoot = Join-Path ([IO.Path]::GetTempPath()) ("dsh-memoryos-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $packRoot | Out-Null
try {
    Push-Location $packageRoot
    try {
        & $pnpm.Source pack --pack-destination $packRoot
        if ($LASTEXITCODE -ne 0) {
            throw "DeepSeek Harness plugin packaging failed"
        }
    }
    finally {
        Pop-Location
    }
    $tarballs = @(Get-ChildItem -LiteralPath $packRoot -Filter "*.tgz")
    if ($tarballs.Count -ne 1) {
        throw "Expected exactly one DeepSeek Harness plugin tarball"
    }
    & $dsh.Source plugin --profile $Profile add $tarballs[0].FullName
    if ($LASTEXITCODE -ne 0) {
        throw "DeepSeek Harness plugin installation failed"
    }
}
finally {
    $resolvedPackRoot = [IO.Path]::GetFullPath($packRoot)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedPackRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a plugin packaging directory outside the system temp directory"
    }
    Remove-Item -LiteralPath $resolvedPackRoot -Recurse -Force
}
