param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "请使用 PowerShell 7.x 运行本脚本。当前版本: $($PSVersionTable.PSVersion)"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

Write-Host "==> 工作目录: $scriptDir" -ForegroundColor Cyan

$venvPython = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $pythonExe = $venvPython
} else {
    $pythonExe = "python"
}

Write-Host "==> 使用 Python: $pythonExe" -ForegroundColor Cyan

$buildArgs = @(
    "build.py",
    "--python", $pythonExe,
    "--pip-timeout", "120",
    "--pip-retries", "2",
    "--step-timeout", "3600"
)

if ($env:PIP_INDEX_URL) {
    $buildArgs += @("--pip-index-url", $env:PIP_INDEX_URL)
}
if ($ForwardArgs) {
    $buildArgs += $ForwardArgs
}

Write-Host "`n==> 调用 build.py 进行打包" -ForegroundColor Yellow
& $pythonExe @buildArgs
if ($LASTEXITCODE -ne 0) {
    throw "打包失败，详见上方日志。"
}

Write-Host "`n✅ 打包完成！输出目录: $scriptDir\dist" -ForegroundColor Green
