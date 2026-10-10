; Inno Setup script for the K4 Connect Manager central server (Windows).
;
; Build first:  pyinstaller packaging\windows-server\k4cm-server.spec
; Then:         iscc packaging\windows-server\installer.iss
; Produces:     packaging\out\k4cm-server-setup.exe
;
; Site data (database, config, TLS certs, logs, backups) lives in
; C:\ProgramData\k4cm and is never touched by install, upgrade or uninstall.

#define AppName "K4 Connect Manager"
#define AppVersion "0.1.0"
#define AppPublisher "K4 Connect Manager"
#define ServerExe "k4cm-server.exe"
#define TaskServer "K4CM Server"
#define TaskWatchdog "K4CM Server Watchdog"
#define FirewallRule "K4 Connect Manager server"

[Setup]
AppId={{7C2F4E1A-9B3D-4A62-8E51-0F6D2C4A9B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\out
OutputBaseFilename=k4cm-server-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#ServerExe}
WizardStyle=modern

[InstallDelete]
; Upgrading: clear the old runtime so no stale files remain. The running server
; is stopped first, in PrepareToInstall. Site data in ProgramData is untouched.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; The PyInstaller one-folder build (carries its own Python runtime).
Source: "..\..\dist\k4cm-server\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "watchdog.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\k4cm"
Name: "{commonappdata}\k4cm\logs"
Name: "{commonappdata}\k4cm\tls"
Name: "{commonappdata}\k4cm\backups"

[Icons]
Name: "{group}\Initialise K4CM Server"; Filename: "{app}\{#ServerExe}"; Parameters: "init"
Name: "{group}\Server data folder"; Filename: "{commonappdata}\k4cm"

[Run]
; Let members reach the API and the radio relay ports. The rule is scoped to the
; server program, not to ports, so relay ports added later need no new rule.
; Deleted first so a reinstall does not stack duplicates.
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; \
  Flags: runhidden; StatusMsg: "Configuring Windows Firewall..."
Filename: "netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#ServerExe}"" enable=yes profile=any"; \
  Flags: runhidden; StatusMsg: "Configuring Windows Firewall..."
; Register the keep-alive tasks: launch at boot, plus a watchdog every 5 minutes.
Filename: "schtasks.exe"; \
  Parameters: "/Create /TN ""{#TaskServer}"" /TR ""\""{app}\{#ServerExe}\"" run"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F"; \
  Flags: runhidden; StatusMsg: "Registering the startup task..."
Filename: "schtasks.exe"; \
  Parameters: "/Create /TN ""{#TaskWatchdog}"" /TR ""powershell.exe -NoProfile -ExecutionPolicy Bypass -File \""{app}\watchdog.ps1\"""" /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F"; \
  Flags: runhidden; StatusMsg: "Registering the watchdog task..."
; Start it now so the administrator can run 'k4cm-server init' against a live box.
Filename: "schtasks.exe"; Parameters: "/Run /TN ""{#TaskServer}"""; Flags: runhidden; \
  StatusMsg: "Starting the server..."
Filename: "{app}\{#ServerExe}"; Parameters: "init"; Description: "Run first-time setup now (creates config, database, TLS certificate and admin account)"; \
  Flags: postinstall shellexec

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""{#TaskWatchdog}"" /F"; Flags: runhidden; RunOnceId: "DelWatchdog"
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""{#TaskServer}"" /F"; Flags: runhidden; RunOnceId: "DelServerTask"
Filename: "taskkill.exe"; Parameters: "/IM {#ServerExe} /F"; Flags: runhidden; RunOnceId: "StopServer"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; RunOnceId: "DelFirewall"

[Code]
{ ---- Prerequisites -------------------------------------------------------
  The PyInstaller bundle needs the Microsoft VC++ 2015-2022 x64 runtime.
  Everything else it uses (Task Scheduler, PowerShell) ships with Windows.
  If the runtime is missing we download and install it. }

const
  VCRedistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe';

function IsVCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(HKEY_LOCAL_MACHINE,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed)
    and (Installed = 1);
end;

function InstallVCRedist(var NeedsRestart: Boolean): String;
var
  TempFile: String;
  ResultCode: Integer;
  DownloadCmd: String;
begin
  Result := '';
  TempFile := ExpandConstant('{tmp}\vc_redist.x64.exe');

  { Download with PowerShell - present on every supported Windows version. }
  DownloadCmd := Format(
    '-NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = ''Tls12''; ' +
    'Invoke-WebRequest -Uri ''%s'' -OutFile ''%s'' -UseBasicParsing; exit 0 } catch { exit 1 }"', [VCRedistUrl, TempFile]);

  if not Exec('powershell.exe', DownloadCmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Result := 'The Microsoft VC++ runtime is required but could not be downloaded.' + #13#10 +
              'Install it manually from ' + VCRedistUrl + ' and run this installer again.';
    Exit;
  end;

  if not Exec(TempFile, '/install /quiet /norestart', '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'The Microsoft VC++ runtime installer could not be started.';
    Exit;
  end;

  { 3010 means success but a reboot is pending. }
  if ResultCode = 3010 then
    NeedsRestart := True
  else if ResultCode <> 0 then
    Result := Format('The Microsoft VC++ runtime installer failed with code %d.', [ResultCode]);
end;

{ ---- Upgrades -------------------------------------------------------------
  A running server holds its files open, and the watchdog relaunches it every
  five minutes, so both are stopped before files are copied. [Run] re-creates
  the tasks (enabled) and starts the server again afterwards. On a first
  install the tasks do not exist yet and these commands simply fail quietly. }

procedure StopRunningServer;
var
  ResultCode: Integer;
begin
  Exec('schtasks.exe', '/Change /TN "{#TaskWatchdog}" /DISABLE', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('schtasks.exe', '/End /TN "{#TaskServer}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/IM {#ServerExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2000);  { let Windows release the file handles }
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  WizardForm.PreparingLabel.Caption := 'Stopping the running server...';
  StopRunningServer;
  if not IsVCRedistInstalled then
  begin
    WizardForm.PreparingLabel.Caption := 'Installing a required Microsoft runtime...';
    Result := InstallVCRedist(NeedsRestart);
  end;
end;

{ ---- Upgrade handling ----------------------------------------------------
  Stop the running server before its files are replaced. Site data in
  ProgramData is left alone in every case. }

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec('schtasks.exe', '/End /TN "{#TaskServer}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/IM {#ServerExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1500);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Your site data (database, configuration, TLS certificates, logs and backups) has been ' +
           'left in:' + #13#10#13#10 + ExpandConstant('{commonappdata}\k4cm') + #13#10#13#10 +
           'Delete that folder by hand if you want it gone.', mbInformation, MB_OK);
end;
