; VEXT's installer hooks (tauri.conf.json > bundle > windows > nsis > installerHooks).
;
; After the files are in place, make sure the PC has what VEXT needs from
; Microsoft, and install only what's missing, with ONE permission prompt:
;
;  - WebView2, which draws VEXT's window. Tauri's own WebView2 step is off
;    (webviewInstallMode "skip"): it only looks at EdgeUpdate's registry entry and
;    aborts the WHOLE install when Microsoft's installer refuses, which is what
;    happens where Windows ships WebView2 without that entry (Windows Sandbox,
;    some company images: 0x80040902, nothing installed). Here WebView2 counts as
;    present when the registry says so OR its files are there; the offline
;    installer is embedded and only unpacked when it's really missing, and a
;    failure never stops VEXT from installing.
;  - Microsoft's Visual C++ runtimes: VEXT and every plugin it opens share one
;    copy per process, so an old or missing one crashes plugins. The installers
;    ship in $INSTDIR\redist (tools\fetch_redist.ps1).
;
; One elevated PowerShell script (redist\install-runtimes.ps1) does the work.

; This file's own folder, taken now: inside a macro ${__FILEDIR__} would be the
; folder of the generated installer.nsi that inserts it, not this one.
!define VEXT_HOOKS_DIR "${__FILEDIR__}"

!macro VEXT_FIND_WEBVIEW2 DIR
  ; $R7 = "1" when DIR\<version>\msedgewebview2.exe exists.
  ${If} $R7 != "1"
    FindFirst $R1 $R2 "${DIR}\*"
    ${DoWhile} $R2 != ""
      ${If} ${FileExists} "${DIR}\$R2\msedgewebview2.exe"
        StrCpy $R7 "1"
        ${Break}
      ${EndIf}
      FindNext $R1 $R2
    ${Loop}
    FindClose $R1
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  Push $R7
  Push $R8
  Push $R9
  SetRegView 64

  ; ---- WebView2: the registry, else the files ----
  StrCpy $R7 "0"
  ReadRegStr $R0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $R0 == ""
    ReadRegStr $R0 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $R0 != ""
  ${AndIf} $R0 != "0.0.0.0"
    StrCpy $R7 "1"
  ${EndIf}
  !insertmacro VEXT_FIND_WEBVIEW2 "$PROGRAMFILES32\Microsoft\EdgeWebView\Application"
  !insertmacro VEXT_FIND_WEBVIEW2 "$LOCALAPPDATA\Microsoft\EdgeWebView\Application"
  StrCpy $R8 ""
  ${If} $R7 != "1"
    DetailPrint "Unpacking WebView2…"
    SetOutPath "$TEMP"
    File "/oname=$TEMP\vext-webview2-setup.exe" "${VEXT_HOOKS_DIR}\..\webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    StrCpy $R8 '-WebView2 "$TEMP\vext-webview2-setup.exe"'
    SetOutPath "$INSTDIR"
  ${EndIf}

  ; ---- Visual C++ runtimes: missing, or older than the copy shipped ----
  StrCpy $R9 "0"
  ReadRegDWORD $R0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
  ReadRegDWORD $R1 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Minor"
  ReadRegDWORD $R2 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Bld"
  ; The shipped copy's version (14.minor.build): high/low words of its file version.
  GetDLLVersion "$INSTDIR\redist\vc_redist.x64.exe" $R3 $R4
  IntOp $R5 $R3 & 0xFFFF
  IntOp $R6 $R4 >> 16
  ${If} $R0 != 1
    StrCpy $R9 "1"
  ${ElseIf} $R1 < $R5
    StrCpy $R9 "1"
  ${ElseIf} $R1 == $R5
  ${AndIf} $R2 < $R6
    StrCpy $R9 "1"
  ${EndIf}
  ; The 32-bit 14.x runtime (the 32-bit plugin host, host32\): registered in the 32-bit view.
  ReadRegDWORD $R0 HKLM "SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86" "Installed"
  ${If} $R0 != 1
    StrCpy $R9 "1"
  ${EndIf}
  ; The older runtimes: their DLL in the 64-bit System32 (this installer is a 32-bit
  ; program: Sysnative is how it sees that folder). Not the registry: their x64
  ; installers register under WOW6432Node, so a 64-bit lookup misses them.
  ${IfNot} ${FileExists} "$WINDIR\Sysnative\msvcr120.dll"
    StrCpy $R9 "1"
  ${EndIf}
  ${IfNot} ${FileExists} "$WINDIR\Sysnative\msvcr110.dll"
    StrCpy $R9 "1"
  ${EndIf}
  ${IfNot} ${FileExists} "$WINDIR\Sysnative\msvcr100.dll"
    StrCpy $R9 "1"
  ${EndIf}
  SetRegView lastused

  ${If} $R9 == "1"
  ${OrIf} $R8 != ""
    DetailPrint "Installing what VEXT needs from Microsoft (WebView2, Visual C++ runtimes)…"
    ExecShellWait "runas" "powershell.exe" '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\redist\install-runtimes.ps1" $R8 -Log "$INSTDIR\redist\install-runtimes.log"' SW_HIDE
  ${EndIf}
  ${If} $R8 != ""
    Delete "$TEMP\vext-webview2-setup.exe"
  ${EndIf}

  Pop $R9
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
!macroend
