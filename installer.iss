; =============================================================================
; 文件: installer.iss
; 归属: 项目根目录/构建辅助脚本层 —— Windows 安装程序(Inno Setup)
; -----------------------------------------------------------------------------
; 用途:
;   把 Windows 构建产物(dist/个人小工具.exe + dist/update/ 目录)打包为
;   wuge_tools-Windows-setup.exe 安装程序,由 GitHub Actions 的 Windows job
;   编译后随 Release 发布。安装版程序目录(Program Files)只读,
;   数据目录由程序自动落到 %LOCALAPPDATA%\wuge_tools(installed.flag 标记识别)。
;
; 编译(CI):
;   ISCC.exe /DMyAppVersion=2.0.0 installer.iss
;   版本号由 CI 从 git tag 读取后经 /D 传入,脚本内为兜底默认值。
; =============================================================================
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppName
  #define MyAppName "个人小工具"
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "wuge"
#endif
#define MyAppExeName "个人小工具.exe"
#define MyAppAssocName MyAppName + " File"

[Setup]
; 应用唯一标识(卸载/升级识别,固定不变,勿改)
AppId={{8E5A2B71-9C4E-4F3D-8B2A-6C1D7E0F5A34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 默认安装到 Program Files(需要管理员权限,静默升级时自动弹 UAC 由用户确认)
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
; 安装程序图标:与主程序同款 app_icon.ico
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 产物名:匹配自更新模板 wuge_tools-Windows-setup.exe
OutputBaseFilename=wuge_tools-Windows-setup
OutputDir=.
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 更新时自动关闭运行中的主程序/更新助手,保证覆盖安装成功
CloseApplications=yes
RestartApplications=no
; 仅支持 64 位系统(Inno Setup 6.3+ 语法)
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; 安装后需要管理员权限(Program Files + 注册表卸载项)
PrivilegesRequired=admin
; 卸载时保留 %LOCALAPPDATA% 用户数据
Uninstallable=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "installer_lang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 主程序单文件 exe
Source: "dist\个人小工具.exe"; DestDir: "{app}"; Flags: ignoreversion
; 更新助手 onedir 目录(update.exe + lib/ 依赖),必须整目录递归
Source: "dist\update\*"; DestDir: "{app}\update"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装版标记:更新助手/主程序据此识别"安装版",数据目录外置、更新走 setup.exe
Source: "dist\installed.flag"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后自动启动主程序(静默升级后无需用户手动打开)
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall

