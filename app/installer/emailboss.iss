; Inno Setup script for Email Boss (per-user install, no admin rights).
; Built by .github/workflows/release.yml after PyInstaller produces
; app/dist/EmailBoss/. Local build: iscc /DAppVersion=1.2.0 emailboss.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Email Boss"
#define AppExe "EmailBoss.exe"
#define AppPublisher "Jeff Deuel"
#define SourceDir "..\dist\EmailBoss"

[Setup]
AppId={{1D26C490-6CD7-4639-9B39-9977978D12E2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\EmailBoss
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
; No "dialog" override: an admin-mode install would make every silent in-app
; update raise a UAC prompt (Inno reuses the previous install's privilege level).
OutputDir=..\dist
OutputBaseFilename=EmailBoss-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
CloseApplications=yes
RestartApplications=no
LicenseFile=..\..\docs\EULA.md
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart

[Run]
; WebView2 runtime is on every Windows 10/11 box with Edge; the exe shows a message with the
; Microsoft download link if it is missing (desktop.ensure_webview2).
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
; In-app update (updater.py runs: Setup.exe /SILENT /CLOSEAPPLICATIONS /RELAUNCH=1): start the new version.
Filename: "{app}\{#AppExe}"; Flags: nowait skipifnotsilent; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
begin
  if CurStep = ssInstall then
    // Restart Manager closed the window; the tray helper is the same EmailBoss.exe and has no
    // window to close. Per-user install: this only ends the signed-in user's own processes.
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
