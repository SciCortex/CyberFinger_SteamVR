; ── CyberFinger Bridge + SteamVR driver installer ──
; Inno Setup 6.3+ script
; Build everything with tools\build_installer.cmd, or compile with: iscc setup.iss (from installer/ directory)
; after building the SteamVR driver (tools\build_driver.cmd) and the bridge (bridge\build.bat).

#define MyAppName "CyberFinger Bridge"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "SciCortex Technologies Corp."
#define MyAppURL "https://github.com/DrSciCortex/CyberFinger"
#define MyAppExeName "CyberFingerBridge.exe"
#define ViGEmSetup "ViGEmBus_1.22.0_x64_x86_arm64.exe"
#define HaveViGEm FileExists(AddBackslash(SourcePath) + ViGEmSetup)
#define DriverSource AddBackslash(SourcePath) + "..\..\out\build\x64-Release\driver\cyberfinger"

#if !FileExists(DriverSource + "\bin\win64\driver_cyberfinger.dll")
  #error The SteamVR driver is not built. Run tools\build_driver.cmd first.
#endif
#if !FileExists(AddBackslash(SourcePath) + "..\dist\" + MyAppExeName)
  #error The bridge is not built. Run bridge\build.bat first.
#endif
#if !HaveViGEm
  #pragma message "ViGEmBus installer not found next to setup.iss: building without it (Gamepad mode users install ViGEmBus themselves)."
#endif

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\CyberFinger Bridge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=CyberFingerBridge_Setup_{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; The SteamVR driver is 64-bit only. Setup itself stays in 32-bit install mode, as in earlier versions:
; 64-bit mode would look for the previous install in the other registry view, miss it, and install a
; second copy next to it instead of upgrading it.
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "steamvrdriver"; Description: "Install the CyberFinger SteamVR driver (controllers + hand skeleton)"; GroupDescription: "SteamVR:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
#if HaveViGEm
Name: "installvigem"; Description: "Install ViGEmBus driver (required for Gamepad mode)"; GroupDescription: "Drivers:"; Flags: checkedonce
#endif

[Files]
; Main application
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Icon
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.png"; DestDir: "{app}"; Flags: ignoreversion

; SteamVR driver folder (registered with SteamVR below)
Source: "{#DriverSource}\*"; DestDir: "{app}\SteamVR\cyberfinger"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: steamvrdriver

#if HaveViGEm
; ViGEmBus installer — bundled into the setup, extracted on demand
; Download from: https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe
; Place in: installer/ViGEmBus_1.22.0_x64_x86_arm64.exe
Source: "{#ViGEmSetup}"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall; Tasks: installvigem
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; Register the SteamVR driver for the user who started setup (vrpathreg writes %LOCALAPPDATA%\openvr).
; Any other "cyberfinger" registration (e.g. a development build) is removed first.
Filename: "{code:GetVRPathReg}"; Parameters: "removedriverswithname cyberfinger"; Flags: runhidden waituntilterminated runasoriginaluser; Tasks: steamvrdriver; Check: HaveVRPathReg
Filename: "{code:GetVRPathReg}"; Parameters: "adddriver ""{app}\SteamVR\cyberfinger"""; StatusMsg: "Registering the SteamVR driver..."; Flags: runhidden waituntilterminated runasoriginaluser; Tasks: steamvrdriver; Check: HaveVRPathReg

#if HaveViGEm
; Install ViGEmBus driver if the task is selected
Filename: "{tmp}\{#ViGEmSetup}"; Parameters: "/qn /norestart"; StatusMsg: "Installing ViGEmBus driver..."; Tasks: installvigem; Flags: waituntilterminated shellexec
#endif

; Launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{code:GetVRPathReg}"; Parameters: "removedriver ""{app}\SteamVR\cyberfinger"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "UnregisterSteamVRDriver"; Tasks: steamvrdriver

[Code]
// ── SteamVR ────────────────────────────────────────────────────────────────

// First path of the Key list ("runtime", "config") in %LOCALAPPDATA%\openvr\openvrpaths.vrpath, or ''.
function ReadOpenVRPath(Key: String): String;
var
  Json: AnsiString;
  Text, Path: String;
  P, Q: Integer;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{localappdata}\openvr\openvrpaths.vrpath'), Json) then
    Exit;
  Text := Utf8Decode(Json);
  P := Pos('"' + Key + '"', Text);
  if P = 0 then
    Exit;
  Delete(Text, 1, P + Length(Key) + 1);   // drop everything up to and including "<Key>"
  P := Pos('"', Text);                    // opening quote of the first path
  if P = 0 then
    Exit;
  Delete(Text, 1, P);
  Q := Pos('"', Text);
  if Q = 0 then
    Exit;
  Path := Copy(Text, 1, Q - 1);
  StringChangeEx(Path, '\\', '\', True);
  Result := Path;
end;

// SteamVR's install folder, or ''.
function FindSteamVR(): String;
var
  Candidate, SteamPath: String;
begin
  Result := '';
  Candidate := ReadOpenVRPath('runtime');
  if (Candidate <> '') and FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
  begin
    Result := Candidate;
    Exit;
  end;
  if RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', SteamPath) then
  begin
    StringChangeEx(SteamPath, '/', '\', True);
    Candidate := SteamPath + '\steamapps\common\SteamVR';
    if FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
    begin
      Result := Candidate;
      Exit;
    end;
  end;
  Candidate := ExpandConstant('{commonpf32}\Steam\steamapps\common\SteamVR');
  if FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
    Result := Candidate;
end;

function FindVRPathReg(): String;
begin
  Result := FindSteamVR();
  if Result <> '' then
    Result := Result + '\bin\win64\vrpathreg.exe';
end;

function HaveVRPathReg(): Boolean;
begin
  Result := FindVRPathReg() <> '';
end;

function GetVRPathReg(Param: String): String;
begin
  Result := FindVRPathReg();
  if Result = '' then
    Result := 'vrpathreg.exe';            // not found: the run entry fails harmlessly
end;

function IsSteamVRRunning(): Boolean;
var
  Locator, Service, Items: Variant;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''vrserver.exe''');
    Result := Items.Count > 0;
  except
    Result := False;
  end;
end;

// SteamVR holds the driver DLL and reads the registration at startup: it must be closed.
function WaitForSteamVRClosed(): Boolean;
begin
  Result := True;
  while IsSteamVRRunning() do
  begin
    if SuppressibleMsgBox('SteamVR is running. Please quit SteamVR, then click Retry.',
                          mbError, MB_RETRYCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := WaitForSteamVRClosed();
end;

function InitializeUninstall(): Boolean;
begin
  Result := WaitForSteamVRClosed();
end;

// ── Leftovers of the proof-of-concept driver ───────────────────────────────
// Its README had users copy the driver into SteamVR\drivers\cyberfinger and add TrackingOverrides for
// "/devices/cyberfinger/..." to steamvr.vrsettings. That copy would clash with this driver, and the
// overrides still match its devices: whichever device holds a hand role would get the CyberFinger pose,
// so the hands would vanish whenever the driver is switched off.

const
  OldOverride = '"/devices/cyberfinger/';

procedure RemoveDriverInsideSteamVR();
var
  SteamVR, Old: String;
begin
  SteamVR := FindSteamVR();
  if SteamVR = '' then
    Exit;
  Old := SteamVR + '\drivers\cyberfinger';
  if not DirExists(Old) then
    Exit;
  if SuppressibleMsgBox('An older CyberFinger driver is installed inside SteamVR:' + #13#10 + Old + #13#10 + #13#10 +
                        'Remove it? It would conflict with the driver being installed.',
                        mbConfirmation, MB_YESNO, IDYES) = IDNO then
    Exit;
  if DelTree(Old, True, True, True) then
    Log('Removed the old driver folder ' + Old)
  else
    SuppressibleMsgBox('Could not remove ' + Old + '. Please delete that folder by hand.', mbError, MB_OK, IDOK);
end;

// The user's steamvr.vrsettings, or ''.
function FindSteamVRSettings(): String;
var
  SteamPath: String;
begin
  Result := ReadOpenVRPath('config');
  if (Result <> '') and FileExists(Result + '\steamvr.vrsettings') then
  begin
    Result := Result + '\steamvr.vrsettings';
    Exit;
  end;
  Result := '';
  if RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', SteamPath) then
  begin
    StringChangeEx(SteamPath, '/', '\', True);
    if FileExists(SteamPath + '\config\steamvr.vrsettings') then
      Result := SteamPath + '\config\steamvr.vrsettings';
  end;
end;

// True for a trimmed line holding just one  "/devices/cyberfinger/<serial>" : "<path>"  entry.
function IsOldOverrideLine(Line: String): Boolean;
var
  T: String;
  P: Integer;
begin
  Result := False;
  T := Line;
  if Pos(OldOverride, T) <> 1 then
    Exit;
  if T[Length(T)] = ',' then
    T := TrimRight(Copy(T, 1, Length(T) - 1));
  P := Pos('"', Copy(T, 2, Length(T)));        // closing quote of the key, at P + 1
  if P = 0 then
    Exit;
  T := TrimLeft(Copy(T, P + 2, Length(T)));    // : "<path>"
  if Copy(T, 1, 1) <> ':' then
    Exit;
  T := TrimLeft(Copy(T, 2, Length(T)));        // "<path>"
  Result := (Length(T) >= 2) and (T[1] = '"') and (Pos('"', Copy(T, 2, Length(T))) = Length(T) - 1);
end;

// Drops those entries from the TrackingOverrides section of a steamvr.vrsettings text, line by line so
// that everything else stays as it was. Returns how many; Unhandled counts entries laid out otherwise.
function StripOldOverrides(var Text: String; var Unhandled: Integer): Integer;
var
  Rest, Line, T, Output: String;
  P, K: Integer;
  InSection, JustRemoved: Boolean;
begin
  Result := 0;
  Unhandled := 0;
  Output := '';
  Rest := Text;
  InSection := False;
  JustRemoved := False;
  while Rest <> '' do
  begin
    P := Pos(#10, Rest);
    if P = 0 then
      P := Length(Rest);
    Line := Copy(Rest, 1, P);
    Delete(Rest, 1, P);
    T := Trim(Line);
    if Pos('"TrackingOverrides"', T) > 0 then
    begin
      InSection := Pos('}', T) = 0;
      if Pos(OldOverride, T) > 0 then
        Unhandled := Unhandled + 1;
    end
    else if InSection and IsOldOverrideLine(T) then
    begin
      Result := Result + 1;
      JustRemoved := True;
      Continue;
    end
    else if InSection and (Pos(OldOverride, T) > 0) then
      Unhandled := Unhandled + 1;
    if T <> '' then
    begin
      if JustRemoved and (T[1] = '}') then
      begin
        // The section's last entry went: drop the comma now left before the closing brace.
        K := Length(Output);
        while (K > 0) and (Output[K] <= ' ') do
          K := K - 1;
        if (K > 0) and (Output[K] = ',') then
          Delete(Output, K, 1);
      end;
      JustRemoved := False;
      if InSection and (T[1] = '}') then
        InSection := False;
    end;
    Output := Output + Line;
  end;
  Text := Output;
end;

procedure RemoveOldTrackingOverrides();
var
  FileName, Text: String;
  Raw: AnsiString;
  Removed, Unhandled: Integer;
begin
  FileName := FindSteamVRSettings();
  if (FileName = '') or not LoadStringFromFile(FileName, Raw) then
    Exit;
  Text := Utf8Decode(Raw);
  if Pos(OldOverride, Text) = 0 then
    Exit;
  Removed := 0;
  Unhandled := 1;
  if Utf8Encode(Text) = Raw then          // edit only what is written back byte for byte
    Removed := StripOldOverrides(Text, Unhandled);
  if Removed > 0 then
  begin
    if CopyFile(FileName, FileName + '.cyberfinger-backup', False) and
       SaveStringToFile(FileName, Utf8Encode(Text), False) then
      Log('Removed ' + IntToStr(Removed) + ' CyberFinger TrackingOverrides from ' + FileName +
          ' (backup: ' + FileName + '.cyberfinger-backup)')
    else
      Unhandled := Unhandled + Removed;
  end;
  if Unhandled > 0 then
    SuppressibleMsgBox('Your SteamVR settings contain TrackingOverrides for the old CyberFinger driver. Please remove ' +
                       'the "/devices/cyberfinger/..." entries from "TrackingOverrides" in' + #13#10 + FileName + #13#10 +
                       'while SteamVR is closed: they would hide your hands whenever the CyberFinger driver is off.',
                       mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssInstall) and WizardIsTaskSelected('steamvrdriver') then
  begin
    RemoveDriverInsideSteamVR();
    RemoveOldTrackingOverrides();
  end;
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('steamvrdriver') and not HaveVRPathReg() then
    SuppressibleMsgBox('SteamVR was not found, so the CyberFinger driver is installed but not registered.' + #13#10 +
                       'Install SteamVR, start it once, then run:' + #13#10 +
                       'vrpathreg adddriver "' + ExpandConstant('{app}\SteamVR\cyberfinger') + '"',
                       mbInformation, MB_OK, IDOK);
end;

// ── ViGEmBus ───────────────────────────────────────────────────────────────

function IsViGEmInstalled: Boolean;
var
  RegistryStr: String;
begin
  // Check if ViGEmBus is already installed via registry
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B37B7390-6E44-4D67-8FC6-565B8A1E58FF}_is1',
     'DisplayName', RegistryStr) then
  begin
    Result := True;
  end;
  // Also check alternative registry path
  if not Result then
  begin
    if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{B37B7390-6E44-4D67-8FC6-565B8A1E58FF}_is1',
       'DisplayName', RegistryStr) then
    begin
      Result := True;
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectTasks then
  begin
    if IsViGEmInstalled then
    begin
      Log('ViGEmBus already installed');
    end;
  end;
end;

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%n{#MyAppName} connects CyberFinger BLE controllers to your PC as SteamVR controllers or an Xbox 360 gamepad. It includes the CyberFinger SteamVR driver, which you can switch off in SteamVR Settings > Startup/Shutdown > Manage Add-ons. An existing installation is upgraded in place.%n%nPlease quit SteamVR before continuing.
