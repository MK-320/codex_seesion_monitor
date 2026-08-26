!include "FileFunc.nsh"
!include "LogicLib.nsh"

!macro NSIS_HOOK_PREINSTALL
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
