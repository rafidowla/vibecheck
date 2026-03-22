; --------------------------------------------------------------------------
; vibecheck.nsi — NSIS installer script for VibeCheck (Windows)
;
; Purpose:
;     Creates a standard Windows installer with Start Menu shortcut,
;     desktop shortcut, and uninstaller.
;
; Prerequisites:
;     - PyInstaller output at dist\VibeCheck\
;     - NSIS (https://nsis.sourceforge.io/)
;
; Usage:
;     makensis build\vibecheck.nsi
;
; Side Effects:
;     Produces dist\VibeCheckSetup.exe
; --------------------------------------------------------------------------

!include "MUI2.nsh"

; --- General ---
Name "VibeCheck"
OutFile "..\dist\VibeCheckSetup.exe"
InstallDir "$LOCALAPPDATA\VibeCheck"
InstallDirRegKey HKCU "Software\VibeCheck" ""
RequestExecutionLevel user

; --- UI ---
!define MUI_ICON "..\assets\icon.ico"
!define MUI_UNICON "..\assets\icon.ico"
!define MUI_ABORTWARNING

; --- Pages ---
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; --- Install Section ---
Section "Install"
    SetOutPath "$INSTDIR"

    ; Copy all PyInstaller output
    File /r "..\dist\VibeCheck\*.*"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; Registry key for Add/Remove Programs
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeCheck" \
        "DisplayName" "VibeCheck"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeCheck" \
        "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeCheck" \
        "DisplayIcon" "$INSTDIR\VibeCheck.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeCheck" \
        "Publisher" "VibeCheck"

    ; Start Menu shortcut
    CreateDirectory "$SMPROGRAMS\VibeCheck"
    CreateShortCut "$SMPROGRAMS\VibeCheck\VibeCheck.lnk" "$INSTDIR\VibeCheck.exe" "" "$INSTDIR\VibeCheck.exe"
    CreateShortCut "$SMPROGRAMS\VibeCheck\Uninstall.lnk" "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut
    CreateShortCut "$DESKTOP\VibeCheck.lnk" "$INSTDIR\VibeCheck.exe" "" "$INSTDIR\VibeCheck.exe"
SectionEnd

; --- Uninstall Section ---
Section "Uninstall"
    ; Remove files
    RMDir /r "$INSTDIR"

    ; Remove shortcuts
    Delete "$SMPROGRAMS\VibeCheck\VibeCheck.lnk"
    Delete "$SMPROGRAMS\VibeCheck\Uninstall.lnk"
    RMDir "$SMPROGRAMS\VibeCheck"
    Delete "$DESKTOP\VibeCheck.lnk"

    ; Remove registry keys
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeCheck"
    DeleteRegKey HKCU "Software\VibeCheck"
SectionEnd
