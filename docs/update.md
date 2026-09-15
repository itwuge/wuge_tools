# 自更新功能

软件通过 **GitHub Release + 独立更新助手(update.exe)** 完成自动更新,全程不生成任何
cmd/bat/VBS 脚本,从根上消除 Windows 更新时弹出控制台黑窗的问题。

## 架构

```
wuge_tools.exe(主程序)                update/update.exe(独立更新助手)
─────────────────────────             ───────────────────────────────
1. 主窗口就绪后静默检测 Release
2. 发现新版 -> 用户确认
3. 无参启动同目录更新助手
4. 主程序退出,释放文件锁        ──►   1. 自举定位软件目录/数据目录/主程序/磁盘配置
                                      2. 显示更新助手窗口(进度/日志)
                                      3. 下载 tar.gz 更新包
                                      4. 按进程名等待主程序退出(Toolhelp32轮询)
                                      5. 前置校验主程序存在 -> 解压 _update_stage
                                      6. 旧主程序改名 .old_del 备份
                                      7. 复制新文件(失败自动回滚)
                                      8. ShellExecuteW 启动新主程序
                                      9. 助手硬退出(自毁)
```

- **助手自给自足,链路完全内聚**:双击 `update.exe`(无参)即可独立运行,与主程序移交启动
  走同一条MVC链路;助手自行读取同目录 `.data_dir_pointer.json`/`data_store` 配置。
  任务JSON(`data_store/_update_task.json`,首参传入)仅作为可选覆盖保留(精准 old_pid
  等待/透传配置),主程序默认不再写任务文件,无参启动即可。
- 独立模式(双击)下安装时若主程序仍在运行,助手会提示并等待其关闭(最长120秒);
  取消/失败仅退出助手,不替用户拉起主程序。移交模式下主程序已退出,失败/取消会重启旧版。

- 两个程序是**独立的 onefile 可执行文件**,更新助手运行时不占用主程序文件,
  因此替换文件不需要 taskkill、不需要批处理。
- 更新助手自身按 **MVC 分层**(与主程序 `runtime/` 同构):

  | 层 | 位置 | 职责 |
  |----|------|------|
  | 入口 | `updater_app/updater_main.py` | 自举推断(bootstrap)、可选任务JSON、注入数据目录、装配MVC |
  | 自举 | `updater_app/bootstrap.py` | 纯标准库定位软件目录/数据目录指针/主程序(import app_config前运行) |
  | Model | `updater_app/model/updater_model.py` | 任务缺省字段自给、全量配置、更新服务设置表单读写 |
  | View | `updater_app/view/updater_window.py` | 更新服务设置表单 + 下载/安装进度 |
  | Controller | `updater_app/controller/update_controller.py` | 检测→下载→安装→重启编排 |
  | Workers | `updater_app/workers/` | Release检测/下载线程、安装线程 |
  | 业务核心 | `updater_app/installer.py` | 纯标准库文件替换(可独立单测) |

- 更新助手窗口内置 **更新服务设置表单**,聚合读写主程序四份JSON(与主程序共用同一份配置):

  | 表单项 | 落盘文件/字段 |
  |---|---|
  | 仓库所有者 / 仓库项目名 / 访问令牌 / 更新连接(API地址) / 请求超时 | `github.json` |
  | 更新软件包模板 / 启动时自动检查新版本 | `version.json`(`UPDATE_SAVE_FILENAME_TPL` / `AUTO_CHECK_UPDATE_ON_START`) |
  | 更新下载位置(带目录选择,支持相对路径锚定软件目录) | `user_info.json` 的 `download_dir` |
  | 自动检测系统代理 | `proxy.json` 的 `AUTO_DETECT_SYSTEM_PROXY` |

  保存后立即落盘并重载配置、自动重新检测;主程序侧"系统配置→GitHub配置"同样可改
  API地址/超时/启动自检,两端字段一致。`AUTO_CHECK_UPDATE_ON_START=false` 时主程序
  启动跳过静默检测,仅在手动点"检查更新"时检测。
  适用场景:主程序被错误仓库/API地址/代理配置卡住时,可直接在更新助手中修正后重检。
- 检测/下载阶段失败为**可恢复**:不退出助手,留在窗口供修改配置后"重新检测"(此时主程序文件
  尚未改动);仅安装阶段失败/取消才按启动模式重启旧版并退出助手。
- 更新助手自身正在运行无法覆盖:包内新助手先落为 `update.exe.new_pending`,
  主程序下次启动最早期完成替换(助手的"自升级")。
- 更新助手缺失时(用户只拷贝了主程序)主程序自动回退到主窗口内集成的**多线程下载Tab**
  (内联在 `runtime/view/main_window.py` 的 `_build_tab4`,由"检查更新"入口自动切入或
  用户手动切换):仅下载资产到本地,不替换任何文件;用户也可随时手动切到该Tab浏览/下载资产。
  Tab左侧只读展示系统配置落盘的 github.json / version.json,修改需到"系统配置"Tab。
- 更新助手运行日志:软件目录 `_updater.log`,主程序下次启动时归档进 app.log 后清理。

## 触发时机

| 入口 | 行为 |
|------|------|
| 软件启动后 | 主窗口显示约0.5秒后**静默检测**,无新版/检测失败均不弹窗打扰 |
| 主窗口"检查更新"按钮 | 显式检测,结果必有提示 |

## 更新包格式

tar.gz,内含**主程序 + 更新助手两个文件**(可选单层顶层目录,安装时自动提升):

```
wuge_tools-Windows.tar.gz
├── wuge_tools.exe      # 主程序
└── update.exe          # 更新助手
```

| 平台 | 主程序 | 更新助手 | 更新包文件名 |
|------|--------|----------|--------------|
| Windows | wuge_tools.exe | update.exe | wuge_tools-Windows.tar.gz |
| Linux | wuge_tools | update | wuge_tools-Linux.tar.gz |
| macOS | wuge_tools | update | wuge_tools-macOS.tar.gz |

## 本地打包

```bash
# Windows
pyinstaller --noconfirm --clean wuge_tools.spec
pyinstaller --noconfirm --clean update.spec
# dist/wuge_tools.exe + dist/update.exe 一起放入 tar.gz

# Linux/macOS 见 .github/workflows/build.yml 中的命令(--name 分别为 wuge_tools / update)
```

推送 `v*` tag 后,GitHub Actions 自动构建两个程序并打包上传 Release。

## 失败处理

- 下载失败/用户取消:更新助手弹提示并重新启动当前版本,主程序文件完全不动。
- 解压/复制失败:自动把 `.old_del` 改回原路径回滚,启动旧版本,`_update_stage` 现场保留。
- 等待主程序退出超过30秒:放弃安装并启动旧版本。
- 主程序每次启动会清理 `.old_del`、`_update_stage`、历史 bat/VBS 遗留文件。

## 发布更新

1. 更新版本号(配置中的 `APP_VERSION` / `data_store/config/version.json`)
2. 推送 `v版本号` tag(如 `v1.0.1`),CI 自动构建并创建 Release
3. 更新包资产名必须与平台文件名模板一致,客户端据此自动勾选下载
