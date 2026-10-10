#ifndef AppVersion
  #define AppVersion "0.9.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\windows\main.dist"
#endif
[Setup]
AppId={{3D7FA804-5A76-4714-B2D2-423501DA7809}
AppName=Крест
AppVersion={#AppVersion}
AppPublisher=Krest
DefaultDirName={localappdata}\Programs\Krest
DefaultGroupName=Крест
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\assets\krest.ico
UninstallDisplayIcon={app}\Krest.exe
OutputBaseFilename=Krest-Setup-{#AppVersion}
Compression=lzma2/normal
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked
[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Крест"; Filename: "{app}\Krest.exe"
Name: "{autodesktop}\Крест"; Filename: "{app}\Krest.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\Krest.exe"; Description: "Запустить Крест"; Flags: nowait postinstall skipifsilent
[UninstallRun]
Filename: "{app}\Krest.exe"; Parameters: "--stop-kernel"; Flags: runhidden waituntilterminated; RunOnceId: "StopKernel"
[UninstallDelete]
Type: files; Name: "{app}\Krest.exe"
; User data, models, screenshots and licenses in LocalAppData\Krest\Data are preserved.
