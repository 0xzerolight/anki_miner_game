; Inno Setup script for Anki Miner Game. Shaped on Anki Miner's packaging/innosetup/anki_miner.iss
; (commit 9959edc9).
; Compile from the repo root after pyinstaller built dist\AnkiMinerGame\:
;   iscc /DAppVersion=X.Y.Z packaging\innosetup\anki_miner_game.iss
; The release pipeline passes a plain X.Y.Z (release.yml checks the tag against __version__), which
; is also the numeric version Setup.exe carries.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{C390E8D8-9789-4827-A794-046E5E3B4123}
AppName=Anki Miner Game
AppVersion={#AppVersion}
AppVerName=Anki Miner Game {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=Anki Miner Game Contributors
AppPublisherURL=https://github.com/0xzerolight/anki_miner_game
DefaultDirName={autopf}\AnkiMinerGame
DefaultGroupName=Anki Miner Game
UninstallDisplayIcon={app}\AnkiMinerGame.exe
OutputDir=..\..\dist
OutputBaseFilename=AnkiMinerGame-{#AppVersion}-Windows-x86_64-Setup
LicenseFile=..\..\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Installer diagnostics always land in the user's TEMP directory.
SetupLogging=yes
SetupMutex=AnkiMinerGameSetup-C390E8D8-9789-4827-A794-046E5E3B4123
; Per-user install: no elevation prompt, and {autopf} is the user's Programs folder.
PrivilegesRequired=lowest
; Setup's RedirectionGuard is inherited, whatever Inno's help says: the app the Finish page starts,
; and every program it starts, refuse to follow a junction the user made. uv reaches its managed
; Python through one, so both add-ons fail to install and every VAD pass fails. A per-user Setup
; writes nothing the user could not write already.
RedirectionGuard=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; _internal is the PyInstaller one-folder runtime and belongs to the installer: an upgrade over an
; older install must not keep modules, DLLs or dist-info the new build no longer has. Never touch
; {app} itself.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\AnkiMinerGame\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Anki Miner Game"; Filename: "{app}\AnkiMinerGame.exe"
Name: "{group}\{cm:UninstallProgram,Anki Miner Game}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Anki Miner Game"; Filename: "{app}\AnkiMinerGame.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AnkiMinerGame.exe"; Description: "{cm:LaunchProgram,Anki Miner Game}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    MsgBox(
      'Anki Miner Game settings, game profiles and add-ons were kept at ' +
      '%USERPROFILE%\.anki_miner_game, and your recordings in their output folder. ' +
      'Remove them by hand if you no longer need them.',
      mbInformation,
      MB_OK);
end;
