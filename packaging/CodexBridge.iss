; Build with ISCC /DAppVersion=2.1.31 /DSourceDir=... /DOutputDir=...

#ifndef AppVersion
  #define AppVersion "2.1.31"
#endif
#ifndef SourceDir
#define SourceDir "..\dist"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

[Setup]
AppId={{F7B2F7D4-6B1E-4A6C-9C6C-5A6A2D4E9C41}
AppName=CodexBridge
AppVersion={#AppVersion}
AppPublisher=Cvencent
AppPublisherURL=https://github.com/Cvencent/CodexBridge
AppSupportURL=https://github.com/Cvencent/CodexBridge/issues
DefaultDirName={autopf}\CodexBridge
DefaultGroupName=CodexBridge
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=CodexBridge-Setup-{#AppVersion}-win64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
UninstallDisplayName=CodexBridge
ChangesEnvironment=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\CodexBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\CodexBridge"; Filename: "{app}\CodexBridge.exe"
Name: "{group}\打开 CodexBridge 设置"; Filename: "{app}\CodexBridge.exe"; Parameters: "--settings"
Name: "{autodesktop}\CodexBridge"; Filename: "{app}\CodexBridge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CodexBridge.exe"; Description: "启动 CodexBridge"; Flags: nowait postinstall skipifsilent
[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { The tray app does not always answer Restart Manager close requests. }
  { Force-stop the previous build before Setup replaces its executable. }
  Exec('taskkill.exe', '/F /T /IM CodexBridge.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(700);
  Result := '';
end;
