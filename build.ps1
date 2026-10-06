# =============================================================================
# ModSideDetector — PyInstaller 打包脚本
# =============================================================================
# 用法（在项目根目录）：
#     .\build.ps1
# 若被执行策略拦住：
#     powershell -ExecutionPolicy Bypass -File build.ps1
#
# 设计取舍：
#   * --onedir 而不是 --onefile：单文件 exe 会被解压到临时目录再执行，
#     行为很像恶意软件的释放器，杀毒误报率明显更高；--onedir 误报率低得多。
#   * --windowed：GUI 程序不要弹黑色控制台窗口。
#   * --noupx：UPX 压缩是杀毒引擎的重点怀疑对象，宁可用大一点的体积换低误报。
#
# ⚠️ 两条不能违反的打包红线：
#   1. cache/ 目录绝对不能打进包 —— 里面是 mcmod.cn 抓来的「运行环境」数据，
#      其许可为 BY-NC-SA 3.0（禁止商用、转载需署名），不许随产物分发。
#      本脚本只把 src 代码作为入口分析，不添加任何 --add-data，天然不含 cache/。
#   2. config.json（含用户本机路径、代理等私人配置）也不打进包。
#      程序运行时会在 exe 所在目录自动新建 config.json。
# -----------------------------------------------------------------------------

[CmdletBinding()]
param(
    # 可选：显式指定 Python 解释器（默认自动探测）
    [string]$Python = "",
    # 开发用：打包后顺手跑一次 --version 检查 exe 能否启动
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"

# ---- 项目根 = 本脚本所在目录 ------------------------------------------------
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== ModSideDetector 打包 ===" -ForegroundColor Cyan
Write-Host "项目根目录: $Root"

# ---- 定位 Python 解释器 -----------------------------------------------------
# 注意：PATH 里的 python 可能是 Microsoft Store 的假 stub（运行会弹应用商店），
# 所以优先使用显式路径，其次 py 启动器，最后才是 python。
$candidates = @()
if ($Python) { $candidates += $Python }
$candidates += "C:\Users\一只屑\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$candidates += (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
$candidates += (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)

$Py = ""
foreach ($candidate in $candidates) {
    if (-not $candidate) { continue }
    if (-not (Test-Path $candidate)) { continue }
    try {
        & $candidate -c "import sys, tkinter; print(sys.version_info[:2])" *> $null
        if ($LASTEXITCODE -eq 0) { $Py = $candidate; break }
    } catch { continue }
}
if (-not $Py) {
    throw "找不到可用的 Python 解释器。请用 -Python <路径> 指定。"
}
Write-Host "Python: $Py"
& $Py -c "import sys; print('  版本:', sys.version.split()[0])"

# ---- 依赖检查 ---------------------------------------------------------------
& $Py -c "import customtkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "缺少 customtkinter。请先运行: & '$Py' -m pip install customtkinter pyinstaller"
}
& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "缺少 PyInstaller。请先运行: & '$Py' -m pip install pyinstaller"
}
$hasDnd = $false
& $Py -c "import tkinterdnd2" 2>$null
if ($LASTEXITCODE -eq 0) { $hasDnd = $true; Write-Host "tkinterdnd2: 已安装（拖拽功能会启用）" }
else { Write-Host "tkinterdnd2: 未安装（打包后无拖拽，属可接受的降级）" -ForegroundColor Yellow }

# ---- 目录准备 ---------------------------------------------------------------
$DistDir = Join-Path $Root "dist"
$WorkDir = Join-Path $Root "build\work"
$SpecDir = Join-Path $Root "build"
$EntryFile = Join-Path $Root "build\_entry_gui.py"

New-Item -ItemType Directory -Force -Path $SpecDir | Out-Null

# PyInstaller 的入口脚本：必须从「仓库根」导入 src.main，这样相对导入才有包上下文。
# 直接把 src\main.py 当入口会让 PyInstaller 把它当成顶层脚本，相对导入会失败。
@'
# 自动生成，请勿手改（由 build.ps1 生成）
import sys
from src.main import main

if __name__ == "__main__":
    raise SystemExit(main())
'@ | Set-Content -Path $EntryFile -Encoding UTF8

# ---- 组装参数 ---------------------------------------------------------------
# 注意：变量名不要用 $args（PowerShell 的自动变量）
$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",                       # 优先 onedir：杀毒误报率低于 onefile
    "--windowed",                     # GUI 程序，不带控制台窗口
    "--noupx",                        # 不用 UPX 压缩，降低杀软误报
    "--name", "ModSideDetector",
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $SpecDir,
    "--paths", $Root,                 # 让 PyInstaller 能找到 src 包
    # customtkinter 的主题 JSON / 字体资源是运行时按包路径加载的，必须整包收集
    "--collect-all", "customtkinter",
    "--hidden-import", "darkdetect",
    # 明确排除用不到的大件，避免把几百 MB 的无关依赖拖进包
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--exclude-module", "PIL",
    "--exclude-module", "cv2",
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2",
    "--exclude-module", "PySide6",
    "--exclude-module", "wx",
    "--exclude-module", "IPython",
    "--exclude-module", "jupyter",
    "--exclude-module", "notebook",
    "--exclude-module", "pytest",
    "--exclude-module", "sphinx",
    "--exclude-module", "tkinter.test",
    "--exclude-module", "test"
)

if ($hasDnd) {
    # tkdnd 的二进制在包目录里，必须整包收集，否则拖拽会在运行时静默失效
    $pyArgs += @("--collect-all", "tkinterdnd2")
}

# ---- 图标（有就用，没有就不加；不凭空造二进制文件）--------------------------
$iconCandidates = @(
    (Join-Path $Root "assets\icon.ico"),
    (Join-Path $Root "icon.ico"),
    (Join-Path $Root "src\ui\icon.ico")
)
foreach ($icon in $iconCandidates) {
    if (Test-Path $icon) {
        Write-Host "图标: $icon"
        $pyArgs += @("--icon", $icon)
        break
    }
}

$pyArgs += $EntryFile

# ---- 执行打包 ---------------------------------------------------------------
Write-Host ""
Write-Host "执行: $Py $($pyArgs -join ' ')" -ForegroundColor DarkGray
$ErrorActionPreference = "Continue"
# PyInstaller 把 INFO 日志写到 stderr；若此时 $ErrorActionPreference = "Stop"，
# PowerShell 会把它当成终止错误、脚本半路退出（实测踩过）。这里临时放宽。
& $Py @pyArgs
$pyExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pyExitCode -ne 0) {
    throw "PyInstaller 打包失败（退出码 $pyExitCode）"
}

# ---- 结果 -------------------------------------------------------------------
$ExePath = Join-Path $DistDir "ModSideDetector\ModSideDetector.exe"
if (-not (Test-Path $ExePath)) {
    throw "打包命令返回成功，但没有找到产物：$ExePath"
}

$sizeMb = [math]::Round(((Get-ChildItem (Split-Path -Parent $ExePath) -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host ""
Write-Host "=== 打包完成 ===" -ForegroundColor Green
Write-Host "产物目录: $(Split-Path -Parent $ExePath)"
Write-Host "可执行文件: $ExePath"
Write-Host "目录总大小: $sizeMb MB"
Write-Host ""
Write-Host "提醒：" -ForegroundColor Yellow
Write-Host "  * exe 未做代码签名，部分杀毒软件可能误报。缓解：用 onedir（已采用）、"
Write-Host "    不用 UPX（已采用）、把目录加入白名单，或自行做代码签名。"
Write-Host "  * cache\ 与 config.json 都没有打进包；程序首次运行会在 exe 目录生成 config.json。"

if (-not $SkipSmokeTest) {
    Write-Host ""
    Write-Host "冒烟测试: ModSideDetector.exe --version" -ForegroundColor Cyan
    # 用 Start-Process -Wait：exe 是 GUI（windowed）子系统程序，
    # 直接用 & 调用时 PowerShell 不会等它跑完，拿不到输出和退出码。
    $smoke = Start-Process -FilePath $ExePath -ArgumentList "--version" -Wait -PassThru -NoNewWindow
    Write-Host "  退出码: $($smoke.ExitCode)"
    Write-Host "  （--windowed 的 exe 没有自带控制台，中文/任何输出都走父控制台；没看到文字属预期现象）"
}

# 打包成功就显式返回 0。PyInstaller 会大量往 stderr 写日志，PowerShell 5.1 的
# 原生命令错误记录可能把脚本退出码带成非 0 —— 用 [Environment]::Exit 收口，
# 保证 CI 只按「产物是否产出」判断成败。
[Environment]::Exit(0)
