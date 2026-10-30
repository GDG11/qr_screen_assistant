# ============================================================================
# 屏幕区域二维码实时捕获助手 - PyInstaller 打包脚本
#
# 使用前：
#   py -3.12 -m venv .venv
#   .\.venv\Scripts\Activate.ps1
#   python -m pip install -r requirements.txt
#   python -m pip install "pyinstaller==6.14.2"
#
# 默认生成单目录版本（启动快、兼容性通常更好）：
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#
# 生成单文件 exe：
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile
#
# 保留控制台用于排查启动错误：
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Console
# ============================================================================

[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$Console
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $pythonExe = $venvPython
} else {
    $pythonExe = "python"
}

Write-Host "使用 Python: $pythonExe"
& $pythonExe -c "import PyInstaller, sys; print('PyInstaller', PyInstaller.__version__); print(sys.version)"
if ($LASTEXITCODE -ne 0) {
    throw '无法导入 PyInstaller。请先执行: python -m pip install "pyinstaller==6.14.2"'
}

$iconPath = Join-Path $PSScriptRoot "assets\app.ico"
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "缺少图标文件: $iconPath"
}

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--name", "QrScreenCaptureAssistant",
    "--icon", $iconPath
)

if ($Console) {
    $pyInstallerArgs += "--console"
} else {
    $pyInstallerArgs += "--windowed"
}

if ($OneFile) {
    $pyInstallerArgs += "--onefile"
}

$pyInstallerArgs += (Join-Path $PSScriptRoot "main.py")

Write-Host "开始打包..."
Write-Host ("PyInstaller 参数: " + ($pyInstallerArgs -join " "))
& $pythonExe @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，退出码: $LASTEXITCODE"
}

Write-Host ""
if ($OneFile) {
    Write-Host "打包完成: dist\QrScreenCaptureAssistant.exe"
    Write-Host "captures 文件夹会创建在 exe 所在目录旁（若不可写则回退到用户目录）。"
} else {
    Write-Host "打包完成: dist\QrScreenCaptureAssistant\QrScreenCaptureAssistant.exe"
    Write-Host "请保留整个 dist\QrScreenCaptureAssistant 目录，不要只移动 exe。"
    Write-Host "captures 文件夹会创建在 exe 所在目录旁（若不可写则回退到用户目录）。"
}
Write-Host "如需可选的 WeChatQRCode 模型，请把 models\wechat_qrcode 放在 exe 旁边；详见 README。"
