; Inno Setup script for the Monteur Windows installer.
;
;   1) Build the shell:   python scripts/build_exe.py   (produces dist/Monteur-<ver>-windows.exe)
;   2) Compile this:      iscc packaging/monteur.iss     (Inno Setup 6, on Windows)
;                         or: python scripts/build_installer.py
;
; Produces dist/Monteur-Setup-<version>.exe — a real installer: Start-menu +
; Desktop shortcuts, an Add/Remove-Programs entry with an uninstaller, and a
; per-user install (no admin prompt), which matches the self-update model
; (payloads land in %USERPROFILE%\.monteur, writable without elevation).
;
; DATA IS NEVER TOUCHED. All projects/settings/payloads live in
; %USERPROFILE%\.monteur, entirely outside the install folder — so installing,
; updating and even UNINSTALLING never removes your projects.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppExe
  ; the shell built by scripts/build_exe.py; override with /DMyAppExe=... if renamed
  #define MyAppExe "..\dist\Monteur-" + MyAppVersion + "-windows.exe"
#endif

#define MyAppName "Monteur"
#define MyAppPublisher "Monteur"

[Setup]
AppId={{B7A1F3C2-9E4D-4B7A-9C21-4D0E2A6F1B90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; per-user install: no admin, lands in %LOCALAPPDATA%\Programs\Monteur
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Monteur.exe
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=Monteur-Setup-{#MyAppVersion}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; the single self-contained shell (Python + deps are inside it); it carries a
; baseline payload and updates itself into %USERPROFILE%\.monteur\payloads
Source: "{#MyAppExe}"; DestDir: "{app}"; DestName: "Monteur.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\Monteur.exe"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\Monteur.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Monteur.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] for %USERPROFILE%\.monteur — the user's projects,
; settings and downloaded payloads are deliberately left in place on uninstall.
