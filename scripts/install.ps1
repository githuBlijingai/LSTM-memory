# 使用项目固化的国内镜像安装 Python 依赖。
# 项目基线 Python 为 3.12（.python312），不要使用系统 3.15 解释器安装 ML 依赖，
# 因为 PyYAML 等包在 3.15 上没有可用 wheel，会回落到源码编译并失败。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1            # 基础+dev
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -WithMl     # 追加 ML 依赖
param(
    [switch]$WithMl
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pipConfig = Join-Path $root "infra/pip.ini"
$venvPython = Join-Path $root ".venv312/Scripts/python.exe"

if (-not (Test-Path $venvPython)) {
    throw "未找到 $venvPython，请先使用项目内置 Python 3.12 创建 .venv312。"
}

# 将镜像配置写入 venv 级 pip.ini，后续任何 pip 命令自动生效，无需临时加参数。
Copy-Item $pipConfig (Join-Path $root ".venv312/pip.ini") -Force
$env:PIP_CONFIG_FILE = $pipConfig

& $venvPython -m pip install --upgrade pip setuptools wheel
$extras = ".[dev]"
if ($WithMl) {
    $extras = ".[dev,ml]"
}
& $venvPython -m pip install -e $extras

Write-Host "依赖安装完成：$extras（镜像：$pipConfig）"
