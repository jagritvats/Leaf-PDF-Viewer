# Pack the PyInstaller bundle (dist\Leaf) as an MSIX for the Microsoft Store or sideloading. From the repo root:
#   pwsh packaging\msix\build_msix.ps1                                   # unsigned, placeholder identity
#   pwsh packaging\msix\build_msix.ps1 -IdentityName 12345Owner.Leaf -Publisher "CN=XXXXXXXX-..." `
#        -PublisherDisplayName "Owner" -DisplayName "Leaf PDF Editor" -NoCli  # Store upload (the Store signs it);
#        -DisplayName = the reserved name, -NoCli = no console twin (Partner Center rejects headless apps)
#   pwsh packaging\msix\build_msix.ps1 -Publisher "CN=Leaf Dev" -CertPath dev.pfx -CertPassword ...  # sideload test
# Needs the Windows 10/11 SDK (makeappx.exe; makepri.exe and signtool.exe optional), nothing is installed here.
# Output: dist\Leaf-<version>-windows-x64<suffix>.msix
param(
    [string]$SourceDir = "dist\Leaf",
    [string]$Version = "",
    [string]$IdentityName = "OWNER.Leaf",
    [string]$Publisher = "CN=Leaf Dev",
    [string]$PublisherDisplayName = "Leaf",
    [string]$DisplayName = "Leaf",
    [string]$Suffix = "",
    [string]$OutDir = "dist",
    [string]$CertPath = "",
    [string]$CertPassword = "",
    [string]$Python = "",
    [switch]$NoCli
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

function Find-SdkTool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (-not (Test-Path $kits)) { return $null }
    $hit = Get-ChildItem $kits -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName "x64\$name" } | Where-Object { Test-Path $_ } | Select-Object -First 1
    return $hit
}

$makeappx = Find-SdkTool "makeappx.exe"
if (-not $makeappx) { throw "makeappx.exe not found: install the Windows SDK (App packaging tools)" }
$makepri = Find-SdkTool "makepri.exe"
if (-not (Test-Path (Join-Path $SourceDir "Leaf.exe"))) { throw "$SourceDir\Leaf.exe not found: build with packaging\leaf.spec first" }

if (-not $Version) {
    $Version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
}
$parts = @($Version.Split(".") | ForEach-Object { [int]($_ -replace '[^0-9].*$', '') })
while ($parts.Count -lt 3) { $parts += 0 }
$msixVersion = "{0}.{1}.{2}.0" -f $parts[0], $parts[1], $parts[2]   # Store: fourth part must be 0

if (-not $Python) { $Python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" } }

$stage = Join-Path $root "build\msix\stage$Suffix"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item (Join-Path $SourceDir "*") $stage -Recurse
if ($NoCli) {
    # Microsoft Store: the console twin is a second, list-less Application (a "headless app"), which Partner Center
    # rejects without a HeadlessAppBypass waiver. Store builds leave it out; the zip and installer keep leaf-cli.exe.
    Remove-Item (Join-Path $stage "leaf-cli.exe") -ErrorAction SilentlyContinue
}
& $Python packaging\msix\make_assets.py (Join-Path $stage "Assets")
if ($LASTEXITCODE) { throw "make_assets.py failed" }

$manifest = Get-Content packaging\msix\AppxManifest.xml -Raw
$manifest = $manifest.Replace("{{IDENTITY_NAME}}", $IdentityName).Replace("{{PUBLISHER}}", $Publisher)
$manifest = $manifest.Replace("{{PUBLISHER_DISPLAY_NAME}}", $PublisherDisplayName).Replace("{{VERSION}}", $msixVersion)
$manifest = $manifest.Replace("{{DISPLAY_NAME}}", $DisplayName)
if (-not (Test-Path (Join-Path $stage "leaf-cli.exe"))) {
    $manifest = [regex]::Replace($manifest, '(?s)\s*<!-- CLI-BEGIN.*?<!-- CLI-END -->', '')
    Write-Host "no leaf-cli.exe in the bundle: console alias left out"
}
[IO.File]::WriteAllText((Join-Path $stage "AppxManifest.xml"), $manifest, (New-Object Text.UTF8Encoding $false))

if ($makepri) {
    # index the scale-200 / targetsize assets so Windows picks the sharpest one
    $cfg = Join-Path $root "build\msix\priconfig$Suffix.xml"
    & $makepri createconfig /cf $cfg /dq en-US /pv 10.0.0 /o | Out-Null
    # one package, not a bundle: keep every scale in resources.pri (no split resources.scale-200.pri)
    [xml]$x = Get-Content $cfg
    $pk = $x.SelectSingleNode("//packaging")
    if ($pk) { [void]$pk.ParentNode.RemoveChild($pk); $x.Save($cfg) }
    & $makepri new /pr $stage /cf $cfg /mn (Join-Path $stage "AppxManifest.xml") /of (Join-Path $stage "resources.pri") /o | Out-Null
    if ($LASTEXITCODE) { throw "makepri failed" }
} else {
    Write-Host "makepri.exe not found: only the unqualified (scale-100) assets will be used"
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$out = Join-Path $OutDir ("Leaf-{0}-windows-x64{1}.msix" -f $Version, $Suffix)
$log = & $makeappx pack /d $stage /p $out /o 2>&1
if ($LASTEXITCODE) { $log | Write-Host; throw "makeappx failed" }

if ($CertPath) {
    $signtool = Find-SdkTool "signtool.exe"
    if (-not $signtool) { throw "signtool.exe not found" }
    $sargs = @("sign", "/fd", "SHA256", "/a", "/f", $CertPath)
    if ($CertPassword) { $sargs += @("/p", $CertPassword) }
    & $signtool @sargs $out
    if ($LASTEXITCODE) { throw "signtool failed" }
    Write-Host "signed $out"
} else {
    Write-Host "unsigned (Store submissions are signed by the Store; sideloading needs -CertPath)"
}
Write-Host "built $out (version $msixVersion, identity $IdentityName, publisher $Publisher)"
