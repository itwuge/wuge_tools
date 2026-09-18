# 自更新功能

软件通过 **GitHub Release + 独立更新助手** 完成自动更新，全程不生成任何
cmd/bat/VBS 脚本，从根上消除更新时弹出控制台黑窗的问题。

## 架构

```
主程序(个人小工具 / 个人小工具.app)      更新助手(update / update.app / update.exe)
─────────────────────────────          ──────────────────────────────────────
1. 主窗口就绪后静默检测 Release(读 github.json)
2. 发现新版 -> 用户确认
3. 写入任务JSON(本地/远程版本+old_pid)并启动同目录更新助手
4. 主程序退出,释放文件锁        ──►    1. 自举定位软件目录/数据目录/主程序(支持嵌套.app)
                                        2. 显示更新助手窗口(进度/日志)
                                        3. 按平台匹配更新包并分片下载 + SHA256 校验
                                        4. 按 old_pid 精准等待主程序退出
                                        5. 安装:Win安装版=setup.exe静默覆盖;Win/Linux绿色版=解压替换;macOS=挂载DMG复制.app
                                        6. 旧版本备份,失败自动回滚
                                        7. 启动新主程序,助手退出
```

- **助手自给自足,链路完全内聚**：更新助手是独立可执行文件（macOS 内嵌于主程序
  `.app/Contents/Resources/update.app`），双击可独立运行，与主程序移交启动走同一条
  MVC 链路；助手自行读取同目录数据目录指针与配置。
- **更新助手自身按 MVC 分层**（与主程序 `runtime/` 同构）：

  | 层 | 位置 | 职责 |
  |----|------|------|
  | 入口 | `updater_app/updater_main.py` | 自举推断(bootstrap)、可选任务JSON、装配MVC |
  | 自举 | `updater_app/bootstrap.py` | 纯标准库定位软件目录/主程序(处理嵌套.app) |
  | Model | `updater_app/model/` | 任务缺省字段、配置读写、包名渲染 |
  | View | `updater_app/view/updater_window.py` | 更新服务设置表单 + 下载/安装进度 |
  | Controller | `updater_app/controller/update_controller.py` | 检测→匹配→下载→安装→重启编排 |
  | Workers | `updater_app/workers/` | Release检测/下载线程、安装线程 |
  | 安装核心 | `updater_app/installer.py` | 解压/DMG挂载/备份/回滚(纯标准库) |

- 更新助手窗口内置**更新服务设置表单**，聚合读写主程序配置
  （`github.json` / `version.json` / `user_info.json` / `proxy.json`），
  与主程序"配置管理"共享同一份配置。主程序被错误仓库/API 地址/代理卡住时，
  可直接在更新助手中修正后重新检测。
- **包名匹配**：助手按 `wuge_tools-{平台}.{扩展名}` 渲染期望包名，先精确匹配、
  再按平台前缀模糊匹配（兼容旧格式资产）；Windows **安装版**（程序目录有 `installed.flag`）
  匹配 `wuge_tools-Windows-setup.exe`，绿色版匹配 zip，Linux=tar.gz，macOS=dmg。
- 检测/下载阶段失败为**可恢复**：不退出助手，留在窗口供修改配置后"重新检测"；
  仅安装阶段失败/取消才按启动模式重启旧版并退出。
- 更新助手自身正在运行无法覆盖：包内新助手先落为 `update.exe.new_pending` /
  `update.new_pending`，主程序下次启动最早期完成替换（助手的"自升级"）。
- 更新助手缺失时（用户只拷贝了主程序）无法自动更新，提示重新下载完整包。
- 更新助手运行日志：软件目录 `_updater.log`，主程序下次启动时归档进 app.log。

## 触发时机

| 入口 | 行为 |
|------|------|
| 软件启动后 | 主窗口显示约0.5秒后**静默检测**，无新版/检测失败均不弹窗打扰（可关闭） |
| 主窗口"检查更新"按钮 | 显式检测，结果必有提示 |

## 更新包格式

按平台生成独立格式（CI 由 `.github/workflows/build.yml` 自动构建）：

```
Windows 安装版: wuge_tools-Windows-setup.exe   # Inno Setup 安装程序(带图标/快捷方式/卸载项)
Windows 绿色版: wuge_tools-Windows.zip
├── 个人小工具.exe      # 主程序
└── update.exe          # 更新助手

Linux:   wuge_tools-Linux.tar.gz
├── 个人小工具          # 主程序
└── update              # 更新助手

macOS:   wuge_tools-macOS-{arm64|x86_64}.dmg
└── 个人小工具.app/Contents/Resources/update.app   # 更新助手内嵌于主程序包内
```

| 平台 | 主程序 | 更新助手 | 更新包文件名 |
|------|--------|----------|--------------|
| Windows (安装版) | 个人小工具.exe | update.exe | wuge_tools-Windows-setup.exe |
| Windows (绿色版) | 个人小工具.exe | update.exe | wuge_tools-Windows.zip |
| Linux | 个人小工具 | update | wuge_tools-Linux.tar.gz |
| macOS (Apple Silicon) | 个人小工具.app | 内嵌 update.app | wuge_tools-macOS-arm64.dmg |
| macOS (Intel) | 个人小工具.app | 内嵌 update.app | wuge_tools-macOS-x86_64.dmg |

Windows 安装版安装流程：静默运行新版 `setup.exe`（`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`），
Inno Setup 自动关闭运行中的主程序/更新助手（CloseApplications）并覆盖安装，完成后自动启动新版本；
数据目录在 `%LOCALAPPDATA%\wuge_tools\`，不受覆盖安装影响。
macOS 安装流程：挂载 DMG → 将 `.app`（含内嵌 `update.app`）复制到主程序位置 →
设置可执行权限 → 卸载镜像；安装前自动备份旧版本，失败回滚。

## 本地打包

```bash
pyinstaller --noconfirm --clean wuge_tools.spec   # 主程序
pyinstaller --noconfirm --clean update.spec       # 更新助手
python build_zip.py / build_tar.py / build_dmg.py # 打更新包(与CI产物一致)

Windows 安装版（需 Inno Setup 6）：

```bash
New-Item -ItemType File -Path dist\installed.flag -Force   # 安装版标记
ISCC.exe /DMyAppVersion=2.0.0 installer.iss                # wuge_tools-Windows-setup.exe
```
```

推送 `v*` tag 后，GitHub Actions 自动构建四个平台产物并上传 Release。

## 失败处理

- 下载失败/用户取消：更新助手弹提示并重新启动当前版本，主程序文件完全不动。
- 解压/复制失败：自动把备份改回原路径回滚，启动旧版本，临时目录现场保留。
- 等待主程序退出超时：放弃安装并启动旧版本。
- 主程序每次启动会清理 `.old_del`、`_update_stage` 等残留文件。

## 发布更新

1. 更新版本号：**代码内置默认**（`runtime/model/app_config.py` 与
   `updater_app/model/config_manager.py` 的 `APP_VERSION`）与本地
   `data_store/config/version.json` 的 `APP_VERSION` 保持一致
2. 推送 `v版本号` tag（如 `v1.0.1`），CI 自动构建并创建 Release（Windows 同时产出
   安装版 `wuge_tools-Windows-setup.exe` 与绿色版 zip）
3. 更新包资产名必须与文件名模板一致（`wuge_tools-{平台}.{扩展名}` / 安装版
   `wuge_tools-Windows-setup.exe`），客户端据此精确/模糊匹配下载
