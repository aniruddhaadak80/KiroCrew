; Kiro Crew's Windows installer remains an electron-builder assisted NSIS
; installer. This include replaces the installer's first-download pages with a
; full-window, theme-aware surface while preserving the generated extraction,
; update, UAC, registry, shortcut, and uninstall machinery.

!include LogicLib.nsh
!include FileFunc.nsh
!include WinMessages.nsh
!include nsDialogs.nsh
!include x64.nsh
!include installer-messages.nsh

!define KIRO_DESIGN_WIDTH 1280
!define KIRO_DESIGN_HEIGHT 860
!define KIRO_PREF_DESKTOP "KiroInstallerDesktopShortcut"
!define KIRO_PREF_STARTUP "KiroInstallerStartWithWindows"
!define KIRO_RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE 20
!define KIRO_DWMWA_SYSTEMBACKDROP_TYPE 38
!define KIRO_DWMSBT_TRANSIENTWINDOW 3
!define KIRO_GWL_STYLE -16
!define KIRO_STYLE_MASK_NO_CHROME 0xFF3BFFFF
!define KIRO_SWP_FRAMECHANGED 0x0020
!define KIRO_SWP_NOACTIVATE 0x0010
!define KIRO_HWND_BOTTOM 1
!define KIRO_HWND_TOP 0
!define KIRO_PBM_SETBARCOLOR 0x0409
!define KIRO_PBM_SETBKCOLOR 0x2001

!ifndef BUILD_UNINSTALLER

Var KiroTheme
Var KiroWindowWidth
Var KiroWindowHeight
Var KiroPage
Var KiroBackground
Var KiroBackgroundHandle
Var KiroProgressPage
Var KiroProgressBackground
Var KiroProgressGhostTopLeft
Var KiroProgressGhostLeft
Var KiroProgressGhostLarge
Var KiroProgressGhostRight
Var KiroProgressGhostBottom
Var KiroProgressGhostSmall
Var KiroProgressGhostSmallLeft
Var KiroProgressGhostBottomRight
Var KiroProgressGhostTopLeftHandle
Var KiroProgressGhostLeftHandle
Var KiroProgressGhostLargeHandle
Var KiroProgressGhostRightHandle
Var KiroProgressGhostBottomHandle
Var KiroProgressGhostSmallHandle
Var KiroProgressGhostSmallLeftHandle
Var KiroProgressGhostBottomRightHandle
Var KiroProgressFrame
Var KiroOpeningSettled
Var KiroOpeningBobFrame
Var KiroTimerRunning
Var KiroPrimaryFont
Var KiroTitleFont
Var KiroButtonFont
Var KiroPrimaryColor
Var KiroMutedColor
Var KiroControlBackground
Var KiroScope
Var KiroScopeSelect
Var KiroScopeNote
Var KiroLocationInput
Var KiroDesktopCheckbox
Var KiroStartupCheckbox
Var KiroCreateDesktopShortcut
Var KiroStartWithWindows
Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroSkipOptions
Var KiroNativeNext
Var KiroNativeCancel
Var KiroActionButton
Var KiroExitButton
Var KiroActionLabel
Var KiroFinishLaunchCheckbox

Function KiroDetectTheme
  StrCpy $KiroTheme "light"
  ClearErrors
  ReadRegDWORD $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" "AppsUseLightTheme"
  ${IfNot} ${Errors}
  ${AndIf} $0 == 0
    StrCpy $KiroTheme "dark"
  ${EndIf}

  ${If} $KiroTheme == "dark"
    StrCpy $KiroPrimaryColor 0xFFFFFF
    StrCpy $KiroMutedColor 0xE3D9F1
    StrCpy $KiroControlBackground 0x482878
  ${Else}
    StrCpy $KiroPrimaryColor 0x24143C
    StrCpy $KiroMutedColor 0x5C4D6D
    StrCpy $KiroControlBackground 0xF9F5FF
  ${EndIf}
FunctionEnd

; The design caps at the approved 1280x860 composition and shrinks to a small
; display instead of rendering off-screen. The bitmap and percentage layout
; scale together, including at non-100% Windows DPI.
Function KiroConfigureWindow
  System::Call "user32::GetSystemMetrics(i 0)i.r0"
  System::Call "user32::GetSystemMetrics(i 1)i.r1"
  StrCpy $KiroWindowWidth $0
  StrCpy $KiroWindowHeight $1
  ${If} $KiroWindowWidth > ${KIRO_DESIGN_WIDTH}
    StrCpy $KiroWindowWidth ${KIRO_DESIGN_WIDTH}
  ${EndIf}
  ${If} $KiroWindowHeight > ${KIRO_DESIGN_HEIGHT}
    StrCpy $KiroWindowHeight ${KIRO_DESIGN_HEIGHT}
  ${EndIf}

  IntOp $2 $0 - $KiroWindowWidth
  IntOp $2 $2 / 2
  IntOp $3 $1 - $KiroWindowHeight
  IntOp $3 $3 / 2
  ; NSIS is a 32-bit process, so use the concrete 32-bit exports rather than
  ; the pointer-sized SDK aliases, which do not have matching exports there.
  System::Call "user32::GetWindowLongW(p $HWNDPARENT, i ${KIRO_GWL_STYLE})i.r4"
  IntOp $4 $4 & ${KIRO_STYLE_MASK_NO_CHROME}
  System::Call "user32::SetWindowLongW(p $HWNDPARENT, i ${KIRO_GWL_STYLE}, i r4)i"
  System::Call "user32::SetWindowPos(p $HWNDPARENT, p 0, i r2, i r3, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_FRAMECHANGED})i"

  ; Windows 11 supplies the real system backdrop; the raster is the frosted
  ; fallback on older releases.
  StrCpy $5 ${KIRO_DWMSBT_TRANSIENTWINDOW}
  System::Call "dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i ${KIRO_DWMWA_SYSTEMBACKDROP_TYPE}, *i r5, i 4)i"
  StrCpy $5 0
  ${If} $KiroTheme == "dark"
    StrCpy $5 1
  ${EndIf}
  System::Call "dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i ${KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE}, *i r5, i 4)i"
FunctionEnd

Function KiroHideNativeChrome
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  GetDlgItem $KiroNativeCancel $HWNDPARENT 2
  GetDlgItem $0 $HWNDPARENT 3
  ShowWindow $KiroNativeNext ${SW_HIDE}
  ShowWindow $KiroNativeCancel ${SW_HIDE}
  ShowWindow $0 ${SW_HIDE}

  ; Hide MUI header/branding siblings so they cannot overlap or retain focus.
  GetDlgItem $0 $HWNDPARENT 1028
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1034
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1035
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1036
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1037
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1038
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1039
  ShowWindow $0 ${SW_HIDE}
FunctionEnd

Function KiroStyleControl
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  ${If} $KiroTheme == "dark"
    System::Call 'uxtheme::SetWindowTheme(p r0, w "DarkMode_Explorer", p 0)i'
  ${Else}
    System::Call 'uxtheme::SetWindowTheme(p r0, w "Explorer", p 0)i'
  ${EndIf}
  Pop $0
FunctionEnd

Function KiroStyleLabel
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent
  Pop $0
FunctionEnd

Function KiroCreateBackground
  ${NSD_CreateBitmap} 0 0 100% 100% ""
  Pop $KiroBackground
  ${If} $KiroTheme == "dark"
    ${NSD_SetStretchedImage} $KiroBackground "$PLUGINSDIR\windows-installer-full-dark.bmp" $KiroBackgroundHandle
  ${Else}
    ${NSD_SetStretchedImage} $KiroBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroBackground, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
FunctionEnd

Function KiroActionClicked
  Pop $0
  SendMessage $KiroNativeNext ${BM_CLICK} 0 0
FunctionEnd

Function KiroExitClicked
  Pop $0
  SendMessage $KiroNativeCancel ${BM_CLICK} 0 0
FunctionEnd

Function KiroCreateActionButtons
  ${NSD_CreateButton} 66.6% 90.6% 12.5% 4.6% "$KiroActionLabel"
  Pop $KiroActionButton
  SendMessage $KiroActionButton ${WM_SETFONT} $KiroButtonFont 0
  ${If} $KiroTheme == "dark"
    SetCtlColors $KiroActionButton 0x2B144B 0xFFFFFF
  ${Else}
    SetCtlColors $KiroActionButton 0xFFFFFF 0x6332B4
  ${EndIf}
  ${NSD_OnClick} $KiroActionButton KiroActionClicked
  ${NSD_SetFocus} $KiroActionButton

  ${NSD_CreateButton} 89.2% 3.1% 8.3% 4.2% "$(kiroExitSetup)  ×"
  Pop $KiroExitButton
  SendMessage $KiroExitButton ${WM_SETFONT} $KiroPrimaryFont 0
  ${NSD_OnClick} $KiroExitButton KiroExitClicked
  Push $KiroExitButton
  Call KiroStyleControl
FunctionEnd

Function KiroUseCurrentUser
  StrCpy $KiroScope "current"
  StrCpy $KiroInstallDir $KiroPerUserDefault
  ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${NSD_SetText} $KiroScopeNote "$(freshInstallForCurrent)"
  SetCtlColors $KiroScopeNote $KiroMutedColor transparent
FunctionEnd

Function KiroUseAllUsers
  StrCpy $KiroScope "all"
  StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${NSD_SetText} $KiroScopeNote "$(freshInstallForAll)"
  SetCtlColors $KiroScopeNote $KiroMutedColor transparent
FunctionEnd

Function KiroScopeChanged
  Pop $0
  SendMessage $KiroScopeSelect ${CB_GETCURSEL} 0 0 $1
  ${If} $1 == 1
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
FunctionEnd

Function KiroBrowseClicked
  Pop $0
  nsDialogs::SelectFolderDialog "$(^DirBrowseText)" "$KiroInstallDir"
  Pop $1
  ${If} $1 != "error"
  ${AndIf} $1 != ""
    StrCpy $KiroInstallDir $1
    Call KiroEnsureAppInstallDir
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${EndIf}
FunctionEnd

; Reuse the opening screen's staggered character motion on every visible setup
; phase, including the options page. Compact crops replace the matching
; characters already painted into the full-window surface.
Function KiroCreateOpeningAnimation
  ${NSD_CreateBitmap} 14.84% 0% 14.84% 15.12% ""
  Pop $KiroProgressGhostTopLeft
  ${NSD_CreateBitmap} 0% 24.4% 14.84% 31.4% ""
  Pop $KiroProgressGhostLeft
  ${NSD_CreateBitmap} 59.38% 0% 21.09% 27.91% ""
  Pop $KiroProgressGhostLarge
  ${NSD_CreateBitmap} 85.16% 40.7% 14.84% 32.56% ""
  Pop $KiroProgressGhostRight
  ${NSD_CreateBitmap} 14.06% 69.77% 31.25% 30.23% ""
  Pop $KiroProgressGhostBottom
  ${NSD_CreateBitmap} 78.13% 9.3% 14.06% 20.93% ""
  Pop $KiroProgressGhostSmall
  ${NSD_CreateBitmap} 6.64% 62.79% 13.28% 22.09% ""
  Pop $KiroProgressGhostSmallLeft
  ${NSD_CreateBitmap} 60.16% 67.44% 13.28% 23.26% ""
  Pop $KiroProgressGhostBottomRight
  StrCpy $KiroProgressFrame 0
  StrCpy $KiroOpeningSettled 0
  StrCpy $KiroOpeningBobFrame 0
  Call KiroSetProgressFrame
  ${NSD_CreateTimer} KiroAdvanceProgressFrame 150
  StrCpy $KiroTimerRunning 1
FunctionEnd

Function KiroOptionsCreate
  ${If} $KiroSkipOptions == 1
    Abort
  ${EndIf}
  Call KiroDetectTheme
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome

  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 10 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 12 600
  CreateFont $KiroButtonFont "Segoe UI Variable Text" 11 650
  Call KiroCreateBackground
  Call KiroCreateOpeningAnimation

  ${NSD_CreateLabel} 19.4% 67.5% 24% 3.4% "$(kiroInstallOptions)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent
  ${NSD_CreateLabel} 19.4% 72.5% 15% 3.5% "$(kiroInstallFor)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateDropList} 34.7% 71.1% 44.4% 100u ""
  Pop $KiroScopeSelect
  ReadEnvStr $0 "USERNAME"
  StrCpy $1 "$(onlyForMe) ($0)"
  ${NSD_CB_AddString} $KiroScopeSelect "$1"
  ${NSD_CB_AddString} $KiroScopeSelect "$(forAll)"
  ${NSD_CB_SelectString} $KiroScopeSelect "$1"
  ${NSD_OnChange} $KiroScopeSelect KiroScopeChanged
  SetCtlColors $KiroScopeSelect $KiroPrimaryColor $KiroControlBackground
  Push $KiroScopeSelect
  Call KiroStyleControl

  ${NSD_CreateLabel} 34.7% 76.1% 44.4% 3.2% ""
  Pop $KiroScopeNote
  SetCtlColors $KiroScopeNote $KiroMutedColor transparent
  SendMessage $KiroScopeNote ${WM_SETFONT} $KiroPrimaryFont 0
  ${NSD_CreateLabel} 19.4% 80.6% 15% 3.5% "$(kiroInstallLocation)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateText} 34.7% 79.3% 35.5% 4.5% "$KiroInstallDir"
  Pop $KiroLocationInput
  SetCtlColors $KiroLocationInput $KiroPrimaryColor $KiroControlBackground
  Push $KiroLocationInput
  Call KiroStyleControl
  ${NSD_OnChange} $KiroLocationInput KiroLocationChanged
  ${NSD_CreateBrowseButton} 70.8% 79.3% 8.3% 4.5% "$(^BrowseBtn)"
  Pop $0
  ${NSD_OnClick} $0 KiroBrowseClicked
  Push $0
  Call KiroStyleControl

  ${NSD_CreateCheckbox} 29.7% 85.2% 22% 3.8% "$(kiroDesktopShortcut)"
  Pop $KiroDesktopCheckbox
  Push $KiroDesktopCheckbox
  Call KiroStyleLabel
  ${If} $KiroCreateDesktopShortcut == 1
    ${NSD_Check} $KiroDesktopCheckbox
  ${EndIf}
  ${NSD_CreateCheckbox} 52% 85.2% 27.1% 3.8% "$(kiroStartWithWindows)"
  Pop $KiroStartupCheckbox
  Push $KiroStartupCheckbox
  Call KiroStyleLabel
  ${If} $KiroStartWithWindows == 1
    ${NSD_Check} $KiroStartupCheckbox
  ${EndIf}

  ${NSD_CreateLabel} 19.4% 91.4% 30% 3.5% "$(kiroReadyToInstall)"
  Pop $0
  Push $0
  Call KiroStyleLabel
  StrCpy $KiroActionLabel "$(kiroInstallAction)"
  Call KiroCreateActionButtons
  ${If} $KiroScope == "all"
    ${NSD_CB_SelectString} $KiroScopeSelect "$(forAll)"
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
  nsDialogs::Show

  ${If} $KiroBackgroundHandle != ""
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
FunctionEnd

Function KiroLocationChanged
  Pop $0
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
FunctionEnd

; electron-builder's generated uninstaller removes $INSTDIR recursively. A
; fresh install therefore owns only a directory that did not exist before the
; install. Normalize to a product-name leaf, then keep nesting past collisions;
; checking only the leaf name would mistake an unrelated existing folder named
; Kiro Crew for an install root. Updates skip this function, so legacy custom
; paths stay in place.
Function KiroEnsureAppInstallDir
  ${GetFileName} "$KiroInstallDir" $0
  ${If} $0 != "${APP_FILENAME}"
    StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  ${EndIf}

  KiroCheckFreshInstallDir:
  IfFileExists "$KiroInstallDir\*.*" KiroFreshInstallDirExists 0
  IfFileExists "$KiroInstallDir" KiroFreshInstallDirExists KiroFreshInstallDirReady

  KiroFreshInstallDirExists:
  StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  Goto KiroCheckFreshInstallDir

  KiroFreshInstallDirReady:
FunctionEnd

Function KiroOptionsLeave
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
  ${If} $KiroInstallDir == ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
    Abort
  ${EndIf}
  Call KiroEnsureAppInstallDir
  ${NSD_GetState} $KiroDesktopCheckbox $KiroCreateDesktopShortcut
  ${NSD_GetState} $KiroStartupCheckbox $KiroStartWithWindows

  ${If} $KiroScope == "all"
    System::Call "shell32::IsUserAnAdmin()i.r0"
    ${If} $0 == 0
      StrCpy $0 "/allusers /kiro-options /kiro-desktop=$KiroCreateDesktopShortcut /kiro-startup=$KiroStartWithWindows /D=$KiroInstallDir"
      ClearErrors
      ExecShell "runas" "$EXEPATH" "$0"
      ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "$(loginWithAdminAccount)"
        Abort
      ${EndIf}
      Call KiroStopProgressAnimation
      Quit
    ${EndIf}
  ${EndIf}
  Call KiroStopProgressAnimation
FunctionEnd

; Invisible handoff after electron-builder selects the current/all-users shell
; context. It reapplies the path chosen on the integrated page.
Function KiroApplyOptions
  ${If} $KiroSkipOptions == 0
    Call KiroEnsureAppInstallDir
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
  Abort
FunctionEnd

Function KiroResolveOpeningFrame
  Pop $1
  Pop $0
  ${If} $KiroOpeningSettled == 0
    IntOp $2 $KiroProgressFrame - $0
    ${If} $2 < 0
      StrCpy $2 0
    ${ElseIf} $2 > 4
      StrCpy $2 4
    ${EndIf}
  ${Else}
    IntOp $2 $KiroOpeningBobFrame + $1
    IntOp $2 $2 % 24
    ${If} $2 == 0
      StrCpy $2 7
    ${ElseIf} $2 < 6
      StrCpy $2 4
    ${ElseIf} $2 < 12
      StrCpy $2 5
    ${ElseIf} $2 < 18
      StrCpy $2 6
    ${Else}
      StrCpy $2 5
    ${EndIf}
  ${EndIf}
  Push $2
FunctionEnd

!macro KiroRefreshOpeningGhost NAME CONTROL HANDLE DELAY OFFSET
  ${If} ${HANDLE} != ""
    ${NSD_ClearBitmap} ${CONTROL}
    ${NSD_FreeBitmap} ${HANDLE}
  ${EndIf}
  Push ${DELAY}
  Push ${OFFSET}
  Call KiroResolveOpeningFrame
  Pop $0
  ${NSD_SetStretchedImage} ${CONTROL} "$PLUGINSDIR\windows-installer-progress-$KiroTheme-${NAME}-$0.bmp" ${HANDLE}
!macroend

!macro KiroReleaseOpeningGhost CONTROL HANDLE
  ${If} ${HANDLE} != ""
    ${NSD_ClearBitmap} ${CONTROL}
    ${NSD_FreeBitmap} ${HANDLE}
    StrCpy ${HANDLE} ""
  ${EndIf}
!macroend

Function KiroSetProgressFrame
  Push $0
  Push $1
  Push $2
  !insertmacro KiroRefreshOpeningGhost "top-left" $KiroProgressGhostTopLeft $KiroProgressGhostTopLeftHandle 0 0
  !insertmacro KiroRefreshOpeningGhost "large" $KiroProgressGhostLarge $KiroProgressGhostLargeHandle 1 3
  !insertmacro KiroRefreshOpeningGhost "left" $KiroProgressGhostLeft $KiroProgressGhostLeftHandle 2 6
  !insertmacro KiroRefreshOpeningGhost "right" $KiroProgressGhostRight $KiroProgressGhostRightHandle 3 9
  !insertmacro KiroRefreshOpeningGhost "bottom" $KiroProgressGhostBottom $KiroProgressGhostBottomHandle 4 12
  !insertmacro KiroRefreshOpeningGhost "small" $KiroProgressGhostSmall $KiroProgressGhostSmallHandle 5 15
  !insertmacro KiroRefreshOpeningGhost "small-left" $KiroProgressGhostSmallLeft $KiroProgressGhostSmallLeftHandle 6 18
  !insertmacro KiroRefreshOpeningGhost "bottom-right" $KiroProgressGhostBottomRight $KiroProgressGhostBottomRightHandle 7 21
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function KiroAdvanceProgressFrame
  ${If} $KiroOpeningSettled == 0
    IntOp $KiroProgressFrame $KiroProgressFrame + 1
    ${If} $KiroProgressFrame >= 11
      StrCpy $KiroOpeningSettled 1
      StrCpy $KiroOpeningBobFrame 0
    ${EndIf}
  ${Else}
    IntOp $KiroOpeningBobFrame $KiroOpeningBobFrame + 1
    IntOp $KiroOpeningBobFrame $KiroOpeningBobFrame % 24
  ${EndIf}
  Call KiroSetProgressFrame
FunctionEnd

Function KiroStopProgressAnimation
  ${If} $KiroTimerRunning == 1
    ${NSD_KillTimer} KiroAdvanceProgressFrame
    StrCpy $KiroTimerRunning 0
  ${EndIf}
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostTopLeft $KiroProgressGhostTopLeftHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostLarge $KiroProgressGhostLargeHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostLeft $KiroProgressGhostLeftHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostRight $KiroProgressGhostRightHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostBottom $KiroProgressGhostBottomHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostSmall $KiroProgressGhostSmallHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostSmallLeft $KiroProgressGhostSmallLeftHandle
  !insertmacro KiroReleaseOpeningGhost $KiroProgressGhostBottomRight $KiroProgressGhostBottomRightHandle
FunctionEnd

Function KiroInstallShow
  Call KiroDetectTheme
  Call KiroConfigureWindow
  Call KiroHideNativeChrome
  FindWindow $KiroProgressPage "#32770" "" $HWNDPARENT
  ${If} $KiroProgressPage == 0
    Return
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroProgressPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w "", i 0x5000000E, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, p $KiroProgressPage, p 0, p 0, p 0)p.r0'
  StrCpy $KiroProgressBackground $0
  System::Call "user32::SetWindowPos(p $KiroProgressBackground, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  ${If} $KiroTheme == "dark"
    ${NSD_SetStretchedImage} $KiroProgressBackground "$PLUGINSDIR\windows-installer-full-dark.bmp" $KiroBackgroundHandle
  ${Else}
    ${NSD_SetStretchedImage} $KiroProgressBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  ${EndIf}
  Call KiroCreateOpeningAnimation

  ; Keep only the localized live status and native progress bar on the glass.
  GetDlgItem $0 $KiroProgressPage 1016
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $KiroProgressPage 1027
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $KiroProgressPage 1006
  IntOp $1 $KiroWindowWidth - 496
  IntOp $2 $KiroWindowHeight - 220
  System::Call "user32::SetWindowPos(p r0, p ${KIRO_HWND_TOP}, i 248, i r2, i r1, i 34, i ${KIRO_SWP_NOACTIVATE})i"
  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 11 600
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent
  GetDlgItem $0 $KiroProgressPage 1004
  IntOp $2 $KiroWindowHeight - 150
  System::Call "user32::SetWindowPos(p r0, p ${KIRO_HWND_TOP}, i 248, i r2, i r1, i 12, i ${KIRO_SWP_NOACTIVATE})i"
  SendMessage $0 ${KIRO_PBM_SETBARCOLOR} 0 0x8E48FF
  SendMessage $0 ${KIRO_PBM_SETBKCOLOR} 0 $KiroControlBackground
  ${NSD_CreateButton} 89.2% 3.1% 8.3% 4.2% "$(kiroExitSetup)  ×"
  Pop $KiroExitButton
  SendMessage $KiroExitButton ${WM_SETFONT} $KiroPrimaryFont 0
  ${NSD_OnClick} $KiroExitButton KiroExitClicked
  Push $KiroExitButton
  Call KiroStyleControl
FunctionEnd

Function KiroFinishCreate
  Call KiroStopProgressAnimation
  ${If} $KiroBackgroundHandle != ""
    ${NSD_ClearBitmap} $KiroProgressBackground
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
  Call KiroDetectTheme
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome
  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 10 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 19 650
  CreateFont $KiroButtonFont "Segoe UI Variable Text" 11 650
  Call KiroCreateBackground
  Call KiroCreateOpeningAnimation

  ${NSD_CreateLabel} 19.4% 70.5% 59.7% 6% "$(kiroInstalled)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent
  ${NSD_CreateCheckbox} 19.4% 78.3% 59.7% 4.5% "$(kiroLaunchAfterFinish)"
  Pop $KiroFinishLaunchCheckbox
  Push $KiroFinishLaunchCheckbox
  Call KiroStyleLabel
  ${NSD_Check} $KiroFinishLaunchCheckbox
  StrCpy $KiroActionLabel "$(^FinishBtn)"
  Call KiroCreateActionButtons
  nsDialogs::Show
  Call KiroStopProgressAnimation

  ${If} $KiroBackgroundHandle != ""
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
FunctionEnd
!endif

!macro customWelcomePage
  Page custom KiroOptionsCreate KiroOptionsLeave
!macroend

!macro customPageAfterChangeDir
  Page custom KiroApplyOptions
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroInstallShow
!macroend

!macro customFinishPage
  Function KiroFinishLeave
    ${NSD_GetState} $KiroFinishLaunchCheckbox $0
    ${If} $0 == ${BST_CHECKED}
      ${If} ${isUpdated}
        StrCpy $1 "--updated"
      ${Else}
        StrCpy $1 ""
      ${EndIf}
      ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
    ${EndIf}
  FunctionEnd
  Page custom KiroFinishCreate KiroFinishLeave
!macroend

!macro customInit
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "${PROJECT_DIR}/../../packaging/installer-assets/windows-installer-full-light.bmp"
  File "${PROJECT_DIR}/../../packaging/installer-assets/windows-installer-full-dark.bmp"
  File "${PROJECT_DIR}/../../packaging/installer-assets/windows-installer-progress-*.bmp"
  SetOutPath "$INSTDIR"

  StrCpy $KiroSkipOptions 0
  StrCpy $KiroTimerRunning 0
  StrCpy $KiroBackgroundHandle ""
  StrCpy $KiroProgressGhostLeftHandle ""
  StrCpy $KiroProgressGhostLargeHandle ""
  StrCpy $KiroProgressGhostSmallHandle ""
  StrCpy $KiroCreateDesktopShortcut 1
  StrCpy $KiroStartWithWindows 1
  StrCpy $KiroScope "current"
  ${If} $installMode == "all"
    StrCpy $KiroScope "all"
  ${EndIf}
  StrCpy $KiroInstallDir $INSTDIR
  StrCpy $KiroPerUserDefault "$LOCALAPPDATA\Programs\${APP_FILENAME}"
  StrCpy $KiroPerMachineDefault "$PROGRAMFILES\${APP_FILENAME}"
  !ifdef APP_64
    ${If} ${RunningX64}
      StrCpy $KiroPerMachineDefault "$PROGRAMFILES64\${APP_FILENAME}"
    ${EndIf}
  !endif
  ${If} $perUserInstallationFolder != ""
    StrCpy $KiroPerUserDefault $perUserInstallationFolder
  ${EndIf}
  ${If} $perMachineInstallationFolder != ""
    StrCpy $KiroPerMachineDefault $perMachineInstallationFolder
  ${EndIf}

  ; Existing installs predate startup opt-in, so a missing preference must not
  ; silently opt them in during an update.
  ${If} $hasPerUserInstallation == 1
  ${OrIf} $hasPerMachineInstallation == 1
    StrCpy $KiroStartWithWindows 0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}"
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}"
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $0
  ${EndIf}

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/kiro-options" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroSkipOptions 1
    StrCpy $KiroScope "all"
    StrCpy $KiroInstallDir $INSTDIR
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-desktop=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $R1
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-startup=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $R1
  ${EndIf}
  ${If} ${isUpdated}
    StrCpy $KiroSkipOptions 1
  ${EndIf}
!macroend

!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    ${If} $KiroScope == "all"
      StrCpy $isForceMachineInstall 1
    ${Else}
      StrCpy $isForceCurrentInstall 1
    ${EndIf}
  !endif
!macroend

!macro customInstall
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}" $KiroCreateDesktopShortcut
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}" $KiroStartWithWindows
  ${If} $KiroCreateDesktopShortcut != 1
    Delete "$newDesktopLink"
  ${EndIf}
  ${If} $KiroStartWithWindows == 1
    WriteRegStr SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}" '"$appExe"'
  ${Else}
    DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ${EndIf}
!macroend

!macro customUnInstall
  DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ; The generated uninstaller clears only Roaming AppData. Remove this channel's
  ; LocalAppData updater cache on a real uninstall, never during an auto-update.
  ${ifNot} ${isUpdated}
    DetailPrint "Removing update cache: $LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
  ${endIf}
!macroend
