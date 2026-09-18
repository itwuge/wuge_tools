# 个人工具

> #### 基于 PySide6 的个人工具，支持操作、文件传输下载、邮件通知、GitHub 自更新
>
> ### 📖 在线文档站
>
> #### 👉 📚 [点击访问项目静态文档站](https://itwuge.github.io/wuge_tools/)

## 功能特性

- **工具**：单账号/批量操作，账号间防风控随机间隔，支持结果详情展示
- **邮件通知**：操作完成后自动发送结果邮件，支持 QQ 邮箱 SMTP
- **文件传输**：GitHub Release 资产浏览/下载 + TUS 协议上传，下载器 GitHub 配置独立于自更新
- **自更新**：检测 GitHub Release，独立更新助手自动下载、安装并重启，全程无控制台黑窗
  - Windows/Linux 解压替换，macOS 挂载 DMG 安装（更新助手内嵌于主程序 .app 包内）
- **路径配置**：可自定义数据存储目录、下载目录、日志目录等
- **代理配置**：支持系统代理 / 手动代理切换
- **系统配置**：GitHub Token、SMTP 邮件、文件 IO 等配置

## 项目目录结构

```
个人工具/
    ├── main.py                     	 # 主程序唯一入口
    ├── update.spec                 	 # 更新助手 PyInstaller 打包配置
├── wuge_tools.spec             		 # 主程序 PyInstaller 打包配置
├── build_zip.py                		 # Windows zip 打包脚本(CI 调用)
├── build_tar.py                		 # Linux tar.gz 打包脚本(CI 调用)
├── build_dmg.py                		 # macOS dmg 打包脚本(CI 调用,内嵌 update.app)
├── requirements.txt            		 # Python 依赖
├── README.md                   		 # 项目说明文档
├── mkdocs.yml                  		 # 文档站(mkdocs)配置
├── app_icon.ico / .png         		 # 应用图标
│
├── runtime/                    		 # 【GUI运行时 - MVC架构】
│   ├── app_bootstrap.py        		 # 应用启动引导:日志/配置/信号初始化
│   ├── controller/             		 # 控制器层
│   │   ├── app_starter.py      		 # 启动流程:版本检测/自动更新/主窗口
│   │   ├── main_controller.py  		 # 主窗口控制器:/批量/关闭
│   │   └── file_transfer_controller.py  # 文件传输Tab控制器:资产拉取/下载/上传
│   ├── view/                   		 # 视图层
│   │   ├── main_window.py      		 # 主窗口(工具/文件传输/配置管理三个Tab)
│   │   └── common_dialog.py    		 # 通用对话框
│   ├── model/                  		 # 模型层
│   │   ├── main_model.py       		 # 数据模型:账号/配置/记录
│   │   ├── app_config.py       		 # 配置管理:路径解析/默认值/JSON读写
│   │   ├── account_store.py    		 # 数据存储
│   │   ├── sign_tools.py       		 # 记录/结果工具
│   │   └── update_launcher.py  		 # 启动外部更新助手:任务JSON/无窗启动/残留清理
│   └── workers/                		 # 后台线程(QThread)
│       ├── base_worker.py      		 # Worker基类:日志双写
│       ├── sign_mail_worker.py 		 # 单账号线程
│       ├── batch_sign_worker.py		 # 批量线程
│       ├── file_transfer_worker.py  	 # 通用下载/上传线程
│       ├── release_list_worker.py   	 # Release资产列表拉取线程
│       └── version_check_worker.py  	 # 版本检测线程
│
├── service/                    		 # 【业务层】核心业务逻辑
│   ├── run_ba.py               		 # 核心:登录/会话/操作
│   ├── github_update.py        		 # GitHub更新检测
│   ├── send_email.py           		 # 邮件通知业务
│   └── file_downloade.py       		 # 文件下载业务封装
│
├── infrastructure/             		 # 【底层基础设施,禁止写业务】
│   ├── http_client.py          		 # 通用HTTP客户端:会话池/重试/Cookie
│   ├── http_github.py          		 # GitHub REST API:Release/Asset元数据
│   ├── file_transfer.py        		 # 文件传输:多线程分片下载/sha256校验
│   ├── http_email.py           		 # SMTP邮件发送底层
│   ├── system_proxy.py         		 # 系统代理检测
│   └── archive.py              		 # 压缩包处理:zip/tar.gz/dmg 挂载辅助
│
├── updater_app/                		 # 【独立更新助手 update/update.exe,自带完整MVC】
│   ├── updater_main.py         		 # 薄入口:自举/可选任务JSON→日志→装配MVC
│   ├── bootstrap.py            		 # 纯标准库自举:定位软件目录/主程序(支持嵌套.app)
│   ├── installer.py            		 # 安装核心:等旧进程退出/解压/备份/回滚/DMG挂载安装
│   ├── model/                  		 # Model:config_manager/updater_model
│   ├── view/                   		 # View:更新服务设置表单+下载/安装进度窗口
│   ├── controller/             		 # Controller:检测→下载→安装→重启 全流程编排
│   └── workers/                		 # Workers:Release检测/下载线程、文件安装线程
│
├── docs/                       		 # 文档站源码(mkdocs,部署 GitHub Pages)
│   ├── index.md                		 # 首页
│   ├── quickstart.md           		 # 快速开始
│   ├── config.md               		 # 配置说明
│   └── update.md               		 # 自更新说明
│
├── data_store/                 		 # 【用户数据,已 gitignore】
│   ├── config/                 		 # 配置文件(config_manifest.json 及各类模块JSON)
│   ├── accounts/ cookies/ records/ downloads/ logs/
│   └── ...
│
└── .github/workflows/
    ├── build.yml               		 # 多平台构建+Release发布
    └── pages.yml               		 # 文档站部署
```

## 架构分层 & 依赖规则

依赖流向单向，**底层包绝对不能导入上层业务代码**

```
main.py (入口)
    ↓
runtime/app_bootstrap.py → runtime/controller/* (Controller)
    ↓                           ↓
runtime/model/* (Model)     runtime/view/* (View)
    ↓                           ↓
service/* (业务层)                runtime/workers/* (后台线程)
    ↓
infrastructure/* (底层库)
```

1. **infrastructure/**：只依赖标准库 + requests，**零业务逻辑**
2. **service/**：核心业务逻辑，调用 infrastructure
3. **runtime/**：GUI 层，MVC 架构，Controller 调用 service 业务
4. **updater_app/**：独立的更新助手应用（自带 MVC），与主程序同构、相互隔离

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt`：

```txt
requests>=2.31.0
urllib3>=2.0.0
PySocks>=1.7.1
PySide6>=6.5.0
beautifulsoup4>=4.12.0
```

## 快速使用

### 源码运行

```bash
python main.py
```

### 打包发布

多平台构建由 GitHub Actions 自动完成（`.github/workflows/build.yml`），产物格式：

| 平台 | 更新包 | 说明 |
|------|--------|------|
| Windows | `wuge_tools-Windows-setup.exe` | **安装版**(推荐)：Inno Setup 安装程序,带图标/快捷方式/卸载项,自更新自动覆盖安装 |
| Windows | `wuge_tools-Windows.zip` | 绿色版：解压即用,含 `个人小工具.exe` + `update.exe`(自取,自动更新走 zip 替换) |
| Linux | `wuge_tools-Linux.tar.gz` | 含 `个人小工具` + `update` |
| macOS (Apple Silicon) | `wuge_tools-macOS-arm64.dmg` | 主程序 .app 内嵌 `update.app` |
| macOS (Intel) | `wuge_tools-macOS-x86_64.dmg` | 主程序 .app 内嵌 `update.app` |

本地打包：

```bash
pyinstaller --noconfirm --clean wuge_tools.spec   # 主程序
pyinstaller --noconfirm --clean update.spec       # 更新助手
python build_zip.py / build_tar.py / build_dmg.py # 按平台打更新包(CI 调用)

Windows 安装版编译（需安装 Inno Setup 6，CI 自动执行）：

```bash
New-Item -ItemType File -Path dist\installed.flag -Force   # 生成安装版标记(勿放项目根)
ISCC.exe /DMyAppVersion=2.0.0 installer.iss                # 产物 wuge_tools-Windows-setup.exe
```




## 配置说明

首次运行自动创建 `data_store/` 目录（macOS 位于 `~/Library/Application Support/wuge_tools/`，Windows 安装版位于 `%LOCALAPPDATA%\wuge_tools\`，程序目录只读，数据不随更新/卸载丢失；绿色版位于程序目录 `data_store/`），配置文件统一由 `config/config_manifest.json` 定位：

| 文件                          | 说明                                               |
| ----------------------------- | -------------------------------------------------- |
| `config/config_manifest.json` | 配置模块定位清单                                   |
| `config/user_info.json`       | 路径配置、账号密码、邮件开关                       |
| `config/github.json`          | **自更新** GitHub 仓库、Token、更新配置            |
| `config/transfer_github.json` | **文件传输下载器专用** GitHub 配置（与自更新独立） |
| `config/smtp.json`            | SMTP 邮件服务器配置                                |
| `config/proxy.json`           | 代理配置                                           |
| `config/file_io.json`         | 文件 IO 配置（分片大小/并发数）                    |
| `config/version.json`         | 本地版本号、更新包模板、自动检查开关               |

> ⚠️ `data_store/` 包含明文密钥和 Cookie，已加入 `.gitignore`，禁止上传。

## 自更新流程

1. 主窗口就绪后静默检测 GitHub Release（读 `github.json`），发现新版本弹窗确认
2. 确认后主程序写入任务文件（含本地/远程版本与 `old_pid`），以 GUI 方式启动更新助手后退出
3. 更新助手自举定位软件目录，按平台匹配更新包（Windows 安装版=setup.exe / Windows 绿色版=zip / Linux=tar.gz / macOS=dmg）
4. 分片下载 + SHA256 校验，等待主程序进程退出（按 PID 精准等待）
5. 安装：
   - Windows 安装版：静默运行新版本 `setup.exe`（/VERYSILENT），Inno Setup 自动关闭进程并覆盖安装，完成后自动启动新版本
   - Windows/Linux 绿色版：解压到 `_update_stage/`，旧文件改名 `.old_del` 备份，复制新文件
   - macOS：挂载 DMG → 复制 `.app`（含内嵌 `update.app`）→ 备份/回滚
6. 成功则清理临时文件并启动新主程序，助手退出；失败自动回滚旧版本
7. 更新助手自身无法在运行时覆盖，新版先存为 `update.exe.new_pending`，主程序下次启动时替换

> 全程不生成/不启动任何 cmd、bat、VBS 脚本，因此不会出现控制台黑窗。
> 细节见 [docs/update.md](docs/update.md)。

## 文件传输（下载器）说明

- 文件传输 Tab 为**独立下载器**：浏览 GitHub Release 资产、勾选下载、TUS 上传
- 其 GitHub 配置（`transfer_github.json`）**与自更新 `github.json` 完全分离**：
  在文件传输 Tab 修改并保存后，只影响资产拉取/下载，不影响自更新检测
- 下载/上传分片与并发数（`file_io.json`）仅下载器使用

## 重要注意事项

1. QQ 邮箱 SMTP：必须网页端开启 POP3/SMTP，使用**授权码**，不要填账号登录密码
2. 自更新 GitHub 配置：在"配置管理 → GitHub 配置"或更新助手中设置
3. 批量：账号间有 10-50 秒随机间隔，避免风控
4. 更新包：macOS 的 `update.app` 内嵌于主程序 `.app/Contents/Resources/`，两者不分离；
   Windows/Linux 更新包需同时包含主程序与更新助手
5. 安装版识别：程序目录存在 `installed.flag` 即安装版（数据目录外置、自更新走 setup.exe 覆盖安装）；
   绿色版无此文件。项目根**不要**放 `installed.flag`，否则本地源码运行会被误判为安装版

## .gitignore 规则

- `data_store/` - 用户数据（账号/Cookie/配置），禁止上传
- `dist/`、`build/`、`*.exe`、`*.dmg` - 打包产物
- `__pycache__/`、`*.pyc` - Python 缓存
- `*.log` - 日志文件
- `p.py`、`test_*.py`、`text_*.py` - 测试脚本

