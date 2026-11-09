$ErrorActionPreference = 'Stop'

$repo = Join-Path $PSScriptRoot 'laya'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$model = Join-Path $repo 'models\laya-multilingual'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Laya Python environment is missing: $python"
}
if (-not (Test-Path -LiteralPath (Join-Path $model 'model.safetensors'))) {
    throw "Laya multilingual checkpoint is missing: $model"
}

$env:LAYA_HOST = '127.0.0.1'
$env:LAYA_PORT = '18080'
$env:LAYA_DEVICE = 'cuda'
$env:LAYA_PRELOAD = '1'
$env:LAYA_MODELS = 'multilingual'
$env:LAYA_MAX_LOADED = '1'
$env:LAYA_DEFAULT_MODEL = 'multilingual'
$env:LAYA_EXTRA_MODELS = @{ multilingual = $model } | ConvertTo-Json -Compress
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_CACHE = Join-Path $repo '.hf-cache'

Write-Host 'Laya: http://127.0.0.1:18080 (Ctrl+C to stop)'
& $python -m laya.serve
