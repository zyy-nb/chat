param(
    [string]$Config = "$PSScriptRoot\config.json",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".codex-bridge-venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "运行环境不存在：$venvPython。请先安装 requirements.txt。"
}
if (-not (Test-Path -LiteralPath $Config)) {
    throw "配置文件不存在：$Config。请复制 config.example.json 为 config.json 并修改。"
}
if (-not $env:CODEX_BRIDGE_TOKEN) {
    throw "请先设置环境变量 CODEX_BRIDGE_TOKEN。"
}

& $venvPython "$PSScriptRoot\server.py" --config $Config --port $Port
