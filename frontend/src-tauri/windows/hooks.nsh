!include "FileFunc.nsh"
!include "LogicLib.nsh"

; A normal window close hides the app in the tray by design. During install or
; uninstall we must terminate both the shell and its sidecar process tree so
; onedir DLLs are not left locked by a hidden instance.
!macro NSIS_HOOK_STOP_APP_PROCESSES
  ExecWait '"$SYSDIR\taskkill.exe" /F /T /IM "Codex Session Monitor.exe"' $R9
  ExecWait '"$SYSDIR\taskkill.exe" /F /T /IM "codex-monitor-sidecar-x86_64-pc-windows-msvc.exe"' $R9
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro NSIS_HOOK_STOP_APP_PROCESSES
  ${IfNot} ${Silent}
    ${GetParameters} $R0
    ClearErrors
    ${GetOptions} $R0 "/UPDATE" $R1
    ${If} ${Errors}
      ${If} ${FileExists} "$LOCALAPPDATA\CodexSessionMonitor\config.json"
        MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "检测到已有的 Codex Session Monitor 用户配置。$\r$\n$\r$\n是否覆盖配置并恢复默认设置？已导入项目记录和布局偏好将被重置。" IDNO keep_existing_config
        Delete "$LOCALAPPDATA\CodexSessionMonitor\config.json"
        keep_existing_config:
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro NSIS_HOOK_STOP_APP_PROCESSES
  ${IfNot} ${Silent}
    ${GetParameters} $R0
    ClearErrors
    ${GetOptions} $R0 "/UPDATE" $R1
    ${If} ${Errors}
      MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "是否同时删除 Codex Session Monitor 用户配置？$\r$\n$\r$\n这只会删除已导入项目记录和布局偏好，不会删除 Codex 会话或已导入的项目文件夹。" IDNO keep_user_config
      Delete "$LOCALAPPDATA\CodexSessionMonitor\config.json"
      RMDir "$LOCALAPPDATA\CodexSessionMonitor"
      keep_user_config:
    ${EndIf}
  ${EndIf}
!macroend
