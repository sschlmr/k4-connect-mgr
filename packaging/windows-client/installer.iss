; Inno Setup script for the K4 Connect Manager client (Windows).
;
; Build first:  pyinstaller packaging\windows-client\k4cm.spec
; Then:         iscc packaging\windows-client\installer.iss
; Produces:     packaging\out\k4cm-client-setup.exe
;
; User data (config.toml and the pinned server certificate) lives in
; %APPDATA%\k4cm and is untouched by install, upgrade and uninstall.

#define AppName "K4 Connect Manager Client"
#define AppVersion "0.1.0"
#define ClientExe "k4cm.exe"

[Setup]
AppId={{2E8B6D34-71C5-4F09-9A77-5B1E3D8C6A22}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=K4 Connect Manager
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\out
OutputBaseFilename=k4cm-client-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin
ChangesEnvironment=yes
UninstallDisplayIcon={app}\{#ClientExe}
WizardStyle=modern

[InstallDelete]
; Installing over an older version: clear the old runtime so no stale files
; remain. User data in %APPDATA%\k4cm is elsewhere and untouched.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\k4cm\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\docs\k4cm-user-guide.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\k4cm (command prompt)"; Filename: "{cmd}"; Parameters: "/K ""{app}\{#ClientExe}"" --help"
; Notepad, because Windows has no default program for .md files.
Name: "{group}\K4CM User Guide"; Filename: "{win}\notepad.exe"; Parameters: """{app}\k4cm-user-guide.md"""

[Run]
Filename: "{cmd}"; Parameters: "/K ""{app}\{#ClientExe}"" config"; \
  Description: "Configure the client now (server address, callsign, API key)"; Flags: postinstall

[Code]
{ Add the install directory to the system PATH so 'k4cm' works in any shell. }

const
  EnvironmentKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function PathContains(const Needle: String): Boolean;
var
  CurrentPath: String;
begin
  Result := False;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', CurrentPath) then
    Result := Pos(Lowercase(';' + Needle + ';'), Lowercase(';' + CurrentPath + ';')) > 0;
end;

procedure AddToPath(const Directory: String);
var
  CurrentPath: String;
begin
  if PathContains(Directory) then
    Exit;
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', CurrentPath) then
    CurrentPath := '';
  if (CurrentPath <> '') and (Copy(CurrentPath, Length(CurrentPath), 1) <> ';') then
    CurrentPath := CurrentPath + ';';
  RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', CurrentPath + Directory);
end;

procedure RemoveFromPath(const Directory: String);
var
  CurrentPath: String;
  Position: Integer;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', CurrentPath) then
    Exit;
  Position := Pos(Lowercase(';' + Directory), Lowercase(';' + CurrentPath));
  if Position = 0 then
    Exit;
  Delete(CurrentPath, Position, Length(Directory) + 1);
  RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', CurrentPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}'));
    MsgBox('Your settings and the pinned server certificate remain in:' + #13#10#13#10 +
           ExpandConstant('{userappdata}\k4cm') + #13#10#13#10 +
           'Delete that folder by hand if you want them gone.', mbInformation, MB_OK);
  end;
end;
