#define MyAppName "Focale"
#define MyAppPublisher "Arcsecond"
#define MyAppExeName "focale.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif

[Setup]
AppId={{11A3125E-D7EA-487D-9998-67E95343F4A5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Arcsecond\Focale
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
Compression=lzma
SolidCompression=yes
OutputDir=dist\windows
OutputBaseFilename=Focale-Setup-{#MyAppVersion}
WizardStyle=modern

[Tasks]
Name: "modifypath"; Description: "Add Focale to PATH"; GroupDescription: "Additional tasks:"

[Files]
Source: "dist\focale\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Focale"; Filename: "{app}\{#MyAppExeName}"

[Code]
const
  EnvironmentKey = 'Environment';

function NeedsAddPath(Param: String): boolean;
var
  OrigPath: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
    OrigPath := '';
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure AddPath(Param: String);
var
  OrigPath: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
    OrigPath := '';
  if (OrigPath = '') then
    OrigPath := Param
  else
    OrigPath := OrigPath + ';' + Param;
  RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('modifypath') then
  begin
    if NeedsAddPath(ExpandConstant('{app}')) then
      AddPath(ExpandConstant('{app}'));
  end;
end;
