; Installer-time USB naming is deliberately best-effort. The main application
; must remain installable when no MKLink is connected or PnP is still settling.
!macro MKLINK_REMOVE_OLD_INSTALL ROOT KEY
  ReadRegStr $0 ${ROOT} "${KEY}" "UninstallString"
  ${If} $0 != ""
    DetailPrint "Removing the previous Mklink AI Probe installation..."
    nsExec::ExecToLog '$0 /S'
    Pop $1
    DetailPrint "Previous installer cleanup finished with code $1."
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  ; Older builds can be per-user or per-machine and may be installed on any
  ; drive. Resolve their real uninstaller from all standard registry views;
  ; never assume %LOCALAPPDATA% or C:\Program Files.
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mklink AI Probe"
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mklink-AI-Probe"
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mklink AI Probe"
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mklink-AI-Probe"
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKLM "Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Mklink AI Probe"
  !insertmacro MKLINK_REMOVE_OLD_INSTALL HKLM "Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Mklink-AI-Probe"
  ; Very old current-user installers did not register an uninstall key.
  ${If} ${FileExists} "$LOCALAPPDATA\Mklink AI Probe\uninstall.exe"
    nsExec::ExecToLog '"$LOCALAPPDATA\Mklink AI Probe\uninstall.exe" /S'
    Pop $0
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; Apply naming immediately when a probe is already present. When no probe is
  ; connected, the helper still runs successfully and the next explicit rename
  ; action can handle the device after Windows creates its PnP registry entry.
  DetailPrint "Initializing MKLink USB serial port naming..."
  nsExec::ExecToLog '"$INSTDIR\mklink-ai-probe.exe" --manage-usb-port-names apply'
  Pop $0
  ${If} $0 == 0
    DetailPrint "MKLink USB serial port naming initialization completed."
  ${Else}
    DetailPrint "MKLink USB serial port naming initialization returned code $0."
  ${EndIf}
!macroend
