; Inno Setup script for the Listing Cannon PSD Framer desktop worker.
; Wraps the PyInstaller one-folder build (dist\ListingCannon) into a normal
; Windows installer: installs to Program Files, adds Start Menu + optional
; desktop shortcuts, and registers an uninstaller in Add/Remove Programs.
;
; Build order:
;   1. python -m PyInstaller --noconfirm --clean ListingCannon.spec
;   2. "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\ListingCannon.iss
; Output: installer\Output\ListingCannonSetup.exe

#define AppName "Listing Cannon PSD Framer"
#define AppVersion "1.0.0"
#define AppPublisher "Samila Home"
#define AppExe "ListingCannon.exe"

[Setup]
AppId={{9F3C1B4A-7E52-4C8D-9B1F-2A6D5E0C7A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=Output
OutputBaseFilename=ListingCannonSetup
SetupIconFile=..\local_worker\listing_cannon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Pull in the entire PyInstaller output folder.
Source: "..\dist\ListingCannon\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; No installer-managed desktop icon: the user keeps a custom-named shortcut
; ("Listing Cannon - SHOPIFY"), and silent upgrades were re-creating a
; duplicate via the remembered desktopicon task selection.

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
