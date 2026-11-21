; ============================================================================
; 声迹 VoiceTrace — Windows 安装程序（NSIS 3.x，Unicode）
;
; 设计目标：
;   * 覆盖安装：从注册表推导既有安装目录（旧壳只写了 UninstallString），
;     原地覆盖程序文件；models\、data\ 与 runtime 中的 torch 不在 payload 内，
;     天然保留，升级不重新下载模型。
;   * per-user 安装（RequestExecutionLevel user），无需管理员。
;   * 卸载时默认保留 models / data / runtime（含已下载的 torch），可选全删。
;
; 构建：makensis /DVERSION=1.0.1 /DPAYLOAD=<payload目录> /DOUTFILE=<输出exe>
;       配套组装脚本见 scripts/build-installer.ps1
; ============================================================================

Unicode true

!ifndef VERSION
  !define VERSION "1.0.1"
!endif
!ifndef PAYLOAD
  !error "必须通过 /DPAYLOAD=<目录> 指定已组装的安装内容"
!endif
!ifndef OUTFILE
  !define OUTFILE "VoiceTrace_${VERSION}_x64-setup.exe"
!endif

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; ---------------------------------------------------------------- 基本属性
Name "声迹 VoiceTrace"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\VoiceTrace"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ShowInstDetails show
ShowUnInstDetails show
BrandingText "声迹 VoiceTrace ${VERSION} — 本地离线视频转字幕"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "声迹 VoiceTrace"
VIAddVersionKey /LANG=2052 "FileDescription" "声迹 VoiceTrace 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "MIT License"

; ---------------------------------------------------------------- MUI 配置
!define MUI_ICON "..\app\src-tauri\icons\icon.ico"
!define MUI_UNICON "..\app\src-tauri\icons\icon.ico"
!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "确定要取消安装 声迹 VoiceTrace 吗？"

!define MUI_DIRECTORYPAGE_TEXT_TOP "安装程序将把文件复制到下列目录。$\r$\n$\r$\n若该目录已装有旧版本（例如 D:\VoiceTrace），将执行覆盖升级：已下载的离线模型、用户数据与推理组件（torch）全部保留，无需重新下载。"

!define MUI_FINISHPAGE_RUN "$INSTDIR\声迹.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 声迹 VoiceTrace"
!define MUI_FINISHPAGE_RUN_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ---------------------------------------------------------------- 工具函数
; 主程序窗口存在即视为运行中（引擎进程由壳的 Job Object 随壳退出而回收）。
Function EnsureAppClosed
  retry:
  FindWindow $0 "" "声迹 VoiceTrace"
  ${If} $0 == 0
    Return
  ${EndIf}
  MessageBox MB_RETRYCANCEL|MB_ICONSTOP \
    "检测到 声迹 VoiceTrace 正在运行。$\r$\n请先完全退出程序（窗口关闭后稍等几秒），再点「重试」继续安装。" \
    /SD IDCANCEL IDRETRY retry
  Abort
FunctionEnd

Function .onInit
  ; 安装程序单实例
  System::Call 'kernel32::CreateMutex(p 0, i 0, t "VoiceTraceSetupMutex") p .r1 ?e'
  Pop $0
  ${If} $0 <> 0
    MessageBox MB_OK|MB_ICONSTOP "安装程序已在运行。"
    Abort
  ${EndIf}

  ; 推导既有安装目录：优先 InstallLocation（新键），
  ; 回退 UninstallString（1.0.0 旧壳只写了这一项）。
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "InstallLocation"
  ${If} $0 == ""
    ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "UninstallString"
    ${If} $0 != ""
      ; 去掉可能存在的引号，再取父目录
      StrCpy $1 $0 1
      ${If} $1 == '"'
        StrCpy $0 $0 "" 1
        StrCpy $0 $0 -1
      ${EndIf}
      ${GetParent} $0 $0
    ${EndIf}
  ${EndIf}
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}

  Call EnsureAppClosed
FunctionEnd

; ---------------------------------------------------------------- 安装
Section "声迹 VoiceTrace（必需）" SEC_MAIN
  SectionIn RO
  Call EnsureAppClosed

  SetOutPath $INSTDIR
  ; 覆盖程序文件。payload 不含 models\ / data\ / runtime\.downloads\，
  ; 也不含 torch 相关目录 —— 这些既有内容原地保留。
  File /r "${PAYLOAD}\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 注册表（per-user「应用和功能」条目；1.0.1 起补齐 InstallLocation 等字段）
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "DisplayName" "声迹 VoiceTrace"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "Publisher" "jue5hen"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "DisplayIcon" "$INSTDIR\声迹.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace" "NoRepair" 1

  ; 开始菜单快捷方式（与 1.0.0 布局一致）
  CreateDirectory "$SMPROGRAMS\声迹 VoiceTrace"
  CreateShortCut "$SMPROGRAMS\声迹 VoiceTrace\声迹 VoiceTrace.lnk" "$INSTDIR\声迹.exe" "" "$INSTDIR\声迹.exe" 0
SectionEnd

; ---------------------------------------------------------------- 卸载
Function un.onInit
  FindWindow $0 "" "声迹 VoiceTrace"
  ${If} $0 <> 0
    MessageBox MB_OK|MB_ICONSTOP "请先完全退出 声迹 VoiceTrace，再执行卸载。"
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  ; 默认保留用户下载内容与数据（models / data / runtime 含 torch），
  ; 选「是」才彻底清空。避免卸载即损失 8 GB 已下载内容。
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "是否同时删除已下载的离线模型、推理组件（torch）与用户数据？$\r$\n$\r$\n· 否（推荐）：保留 models、data、runtime，将来重装可直接使用，无需重新下载。$\r$\n· 是：彻底删除全部内容（约 8 GB，不可恢复）。" \
    /SD IDNO IDYES un_purge

  ; 保留分支：只移除程序本体
  RMDir /r "$INSTDIR\engine"
  Goto un_files

  un_purge:
  RMDir /r "$INSTDIR\models"
  RMDir /r "$INSTDIR\data"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\engine"

  un_files:
  Delete "$INSTDIR\声迹.exe"
  Delete "$INSTDIR\WebView2Loader.dll"
  Delete "$INSTDIR\使用说明.txt"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\version.txt"
  Delete "$INSTDIR\uninstall.exe"

  Delete "$SMPROGRAMS\声迹 VoiceTrace\声迹 VoiceTrace.lnk"
  RMDir "$SMPROGRAMS\声迹 VoiceTrace"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceTrace"

  ; 目录非空（保留了 models/data/runtime）时静默失败，属预期
  RMDir "$INSTDIR"
SectionEnd