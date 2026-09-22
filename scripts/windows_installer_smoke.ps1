<#
Windows installer smoke (spec 19), run by release.yml on a clean hosted runner. Shaped on Anki Miner's
scripts/windows_installer_smoke_lib.ps1 (legs 1 and 2), for one install.

  1. Installs the Setup.exe silently for the current user and checks the files and the uninstall entry.
  2. Runs the installed AnkiMinerGame.exe in its bundle-smoke mode (ANKI_MINER_GAME_SMOKE=1, see
     anki_miner_game/runtime/bundle_smoke.py) offscreen, with every home in a throwaway folder, and
     checks its exit code, config.json and the self-check's marker in its log.
  3. Uninstalls silently and checks that the program folder and the uninstall entry are gone.

Prints INSTALLER_SMOKE_PASS at the end; scripts/release_dryrun.sh looks for it in the job log. Any
failure throws, after printing the setup, uninstall and app logs.

Usage: ./scripts/windows_installer_smoke.ps1 -Installer dist\AnkiMinerGame-X.Y.Z-Windows-x86_64-Setup.exe -Version X.Y.Z
#>
param(
  [Parameter(Mandatory)] [string] $Installer,
  [Parameter(Mandatory)] [string] $Version
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# From packaging/innosetup/anki_miner_game.iss: the AppId, and DefaultDirName={autopf}\AnkiMinerGame,
# which a per-user install (PrivilegesRequired=lowest) puts under %LOCALAPPDATA%\Programs.
$AppId = '{C390E8D8-9789-4827-A794-046E5E3B4123}'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\AnkiMinerGame'
$Exe = Join-Path $InstallDir 'AnkiMinerGame.exe'
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($AppId)_is1"

$Root = Join-Path ([System.IO.Path]::GetTempPath()) "amg-installer-smoke-$([guid]::NewGuid())"
$UserHome = Join-Path $Root 'home'
$SmokeHome = Join-Path $Root 'app-home'
$SetupLog = Join-Path $Root 'setup.log'
$UninstallLog = Join-Path $Root 'uninstall.log'
$AppLog = Join-Path $SmokeHome 'anki_miner_game.log'

function Assert-That([bool] $Condition, [string] $Message) {
  if (-not $Condition) { throw $Message }
}

function Invoke-Bounded {
  param(
    [string] $Label,
    [string] $FilePath,
    [string] $Arguments,
    [int] $TimeoutSeconds,
    [hashtable] $Environment = @{}
  )
  $info = [System.Diagnostics.ProcessStartInfo]::new($FilePath, $Arguments)
  $info.UseShellExecute = $false
  foreach ($name in $Environment.Keys) {
    $info.Environment[$name] = $Environment[$name]  # the child's environment only
  }
  $process = [System.Diagnostics.Process]::Start($info)
  try {
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      $process.Kill($true)
      throw "$Label did not finish within $TimeoutSeconds s"
    }
    Assert-That ($process.ExitCode -eq 0) "$Label exited $($process.ExitCode)"
  } finally {
    $process.Dispose()
  }
}

function Test-Installed {
  (Test-Path -LiteralPath $InstallDir) -or (Test-Path -LiteralPath $UninstallKey)
}

$setupExe = (Resolve-Path -LiteralPath $Installer).Path
New-Item -ItemType Directory -Force -Path $Root, $UserHome | Out-Null
try {
  Assert-That (-not (Test-Installed)) "Anki Miner Game is already installed on this runner ($InstallDir)"

  # 1. Install. Setup.exe waits for its setup process and returns its exit code.
  Invoke-Bounded -Label 'Setup' -FilePath $setupExe -TimeoutSeconds 300 `
    -Arguments "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /LOG=`"$SetupLog`""
  Assert-That (Test-Path -LiteralPath $Exe -PathType Leaf) "No $Exe after the install"
  Assert-That (Test-Path -LiteralPath $UninstallKey) "No uninstall entry $UninstallKey after the install"
  $entry = Get-ItemProperty -LiteralPath $UninstallKey
  Assert-That ($entry.DisplayVersion -eq $Version) "The uninstall entry has DisplayVersion '$($entry.DisplayVersion)', not '$Version'"
  Assert-That ($entry.InstallLocation.TrimEnd('\') -ieq $InstallDir) "The uninstall entry points at '$($entry.InstallLocation)'"
  Write-Host "Installed $Version in $InstallDir"

  # 2. The installed app's self-check, in a throwaway home.
  Invoke-Bounded -Label 'The installed app' -FilePath $Exe -Arguments '' -TimeoutSeconds 180 -Environment @{
    'ANKI_MINER_GAME_SMOKE' = '1'
    'QT_QPA_PLATFORM'       = 'offscreen'
    'ANKI_MINER_GAME_HOME'  = $SmokeHome
    'HOME'                  = $UserHome
    'USERPROFILE'           = $UserHome
    'APPDATA'               = (Join-Path $UserHome 'AppData\Roaming')
    'LOCALAPPDATA'          = (Join-Path $UserHome 'AppData\Local')
  }
  $config = Join-Path $SmokeHome 'config.json'
  Assert-That ((Test-Path -LiteralPath $config -PathType Leaf) -and (Get-Item -LiteralPath $config).Length -gt 0) "The installed app wrote no $config"
  Assert-That (Test-Path -LiteralPath $AppLog -PathType Leaf) "The installed app wrote no $AppLog"
  Assert-That ([bool](Select-String -LiteralPath $AppLog -SimpleMatch 'BUNDLED_SMOKE_PASS')) 'The self-check left no BUNDLED_SMOKE_PASS in the log'
  Assert-That (-not (Select-String -LiteralPath $AppLog -SimpleMatch 'BUNDLED_SMOKE_FAIL')) 'The self-check logged BUNDLED_SMOKE_FAIL'
  Write-Host 'The installed app passed its self-check'

  # 3. Uninstall. The uninstaller hands over to a copy of itself in TEMP and returns before that copy
  # has removed the folder, so wait for the end state.
  $uninstallers = @(Get-ChildItem -LiteralPath $InstallDir -Filter 'unins*.exe' -File)
  Assert-That ($uninstallers.Count -eq 1) "Expected one unins*.exe in $InstallDir, found $($uninstallers.Count)"
  Invoke-Bounded -Label 'Uninstall' -FilePath $uninstallers[0].FullName -TimeoutSeconds 180 `
    -Arguments "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$UninstallLog`""
  $deadline = [DateTime]::UtcNow.AddSeconds(120)
  while ((Test-Installed) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 500
  }
  Assert-That (-not (Test-Path -LiteralPath $InstallDir)) "$InstallDir survived the uninstall"
  Assert-That (-not (Test-Path -LiteralPath $UninstallKey)) "The uninstall entry survived the uninstall"
  Write-Host 'Uninstalled'
} catch {
  foreach ($log in @($SetupLog, $UninstallLog, $AppLog)) {
    if (Test-Path -LiteralPath $log) {
      Write-Host "=== $log ==="
      Get-Content -LiteralPath $log | Write-Host
    }
  }
  throw
}

Write-Host 'INSTALLER_SMOKE_PASS'
