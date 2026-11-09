param(
    [Parameter(Mandatory=$true)][string]$TargetRoot,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $TargetRoot).Path
$ai = Join-Path $target 'LingYi\intelligent\ai'
foreach ($required in @('agent\config.py', 'engine')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ai $required))) {
        throw "Not a compatible LingYi SDK layout: $ai (missing $required)"
    }
}
$overlay = Join-Path $PSScriptRoot 'overlay'
$entries = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'SOURCE_MANIFEST.json') -Raw | ConvertFrom-Json
foreach ($entry in $entries) {
    $source = Join-Path $PSScriptRoot $entry.path
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
        throw "Source manifest mismatch: $($entry.path)"
    }
}
$backup = Join-Path $target ('codex_laya_backups\' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
foreach ($entry in $entries) {
    $relative = $entry.path.Substring('overlay/'.Length)
    $source = Join-Path $overlay $relative
    $destination = Join-Path $target $relative
    Write-Host "$source -> $destination"
    if ($CheckOnly) { continue }
    if (Test-Path -LiteralPath $destination) {
        $saved = Join-Path $backup $relative
        New-Item -ItemType Directory -Path (Split-Path $saved -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $destination -Destination $saved
    }
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}
if (-not $CheckOnly) {
    Write-Host "Installed. Existing files, if any, were backed up under $backup"
    Write-Host 'Configure agent/config.py local mode and lypath, then follow README.md.'
}
