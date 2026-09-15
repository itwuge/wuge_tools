# 个人工具

> #### 基于 PySide6 的个人工具，支持单账号/批量操作、邮件通知、GitHub 自更新
>
> ### 📖 在线文档站
>
> #### 👉 📚 [点击访问项目静态文档站](https://itwuge.github.io/wuge_tools/)
>
> 

## 功能特性

- **单账号操作**：输入账号密码即可执行，支持结果详情展示
- **批量操作**：多账号顺序执行，账号间防风控随机间隔，汇总邮件通知
- **邮件通知**：操作完成后自动发送结果邮件，支持 QQ 邮箱 SMTP
- **自更新**：检测 GitHub Release，独立更新助手(update.exe)自动下载、解压覆盖并重启，全程无控制台黑窗
- **路径配置**：可自定义数据存储目录、下载目录、日志目录等
- **代理配置**：支持系统代理 / 手动代理切换
- **系统配置**：GitHub Token、SMTP 邮件、文件 IO 等配置

## 项目目录结构

```
个人工具/
├── main.py                     # 主程序唯一入口
├── update.spec                 # 更新助手 PyInstaller 打包配置
├── requirements.txt            # Python 依赖
├── README.md                   # 项目说明文档
├── wuge_tools.spec             # 主程序 PyInstaller 打包配置
├── app_icon.ico / .png         # 应用图标
│
├── updater_app/                # 【独立更新助手 update.exe,自带完整MVC,可无参独立运行】
│   ├── updater_main.py         # 薄入口:自举/可选任务JSON→日志→装配MVC
│   ├── bootstrap.py            # 纯标准库自举:定位软件目录/数据目录指针/主程序(import配置前运行)
│   ├── model/                  # Model:缺省字段自给/全局配置/GitHub配置表单读写
│   ├── view/                   # View:GitHub配置表单+下载/安装进度窗口
│   ├── controller/             # Controller:检测→下载→安装→重启 全流程编排
│   ├── workers/                # Workers:Release检测/下载线程、文件安装线程
│   └── installer.py            # 文件替换核心:等旧进程退出(PID/按名)/解压/备份/回滚(纯标准库)
│
├── comm_tools/                 # 【通用工具层】
│   ├── app_config.py           # 配置管理:路径解析/默认值/JSON读写
│   ├── update_launcher.py      # 启动外部更新助手:任务JSON/无窗启动/残留清理
│   └── tools.py                # 工具函数:HTML解析/字符串清洗/tar.gz解压
│
├── infrastructure/             # 【底层基础设施,禁止写业务】
│   ├── http_client.py          # 通用HTTP客户端:会话池/重试/Cookie
│   ├── http_github.py          # GitHub REST API:Release/Asset元数据
│   ├── file_transfer.py        # 文件传输:多线程分片下载/sha256校验
│   ├── http_email.py           # SMTP邮件发送底层
│   └── system_proxy.py         # 系统代理检测
│
├── service/                    # 【业务层】核心业务逻辑
│   ├── run_ba.py               # 核心:登录/会话/操作
│   ├── github_update.py        # GitHub更新检测
│   ├── send_email.py           # 邮件通知业务
│   └── file_downloade.py       # 文件下载业务封装
│
└── runtime/                    # 【GUI运行时 - MVC架构】
    ├── app_bootstrap.py        # 应用启动引导:日志/配置/信号初始化
    ├── controller/             # 控制器层
    │   ├── app_starter.py      # 启动流程:版本检测/自动更新/主窗口
    │   ├── main_controller.py  # 主窗口控制器:操作/批量操作/关闭
    │   └── update_controller.py# 多线程下载Tab控制器:检查更新/下载(兜底,仅下载)
    ├── view/                   # 视图层
    │   ├── main_window.py      # 主窗口(含个人工具/多线程下载/路径配置/系统配置四个Tab)
    │   └── common_dialog.py    # 通用对话框
    ├── model/                  # 模型层
    │   └── main_model.py       # 数据模型:账号/配置/记录
    └── workers/                # 后台线程(QThread)
        ├── base_worker.py      # Worker基类:日志双写
        ├── sign_mail_worker.py # 单账号操作线程
        ├── batch_sign_worker.py# 批量操作线程
        ├── update_worker.py    # 检查更新/下载线程
        └── version_check_worker.py # 版本检测线程
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
infrastructure/* (底层库) + comm_tools/* (通用工具)
```

1. **infrastructure/**：只依赖标准库 + requests，**零业务逻辑**
2. **service/**：核心业务逻辑，调用 infrastructure
3. **comm_tools/**：通用工具，不导入网络底层和业务层
4. **runtime/**：GUI 层，MVC 架构，Controller 调用 service 业务

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt`：

```txt
requests>=2.31.0
urllib3>=2.0.0
PySide6>=6.5.0
beautifulsoup4>=4.12.0
```

## 快速使用

### 源码运行

```bash
python main.py
```

### 打包为 exe

需要分别打包**主程序**和**更新助手**两个独立 exe,并放在同一目录分发:

```bash
pyinstaller --noconfirm --clean wuge_tools.spec
pyinstaller --noconfirm --clean update.spec
```

产物:`dist/wuge_tools.exe`(主程序) + `dist/update.exe`(更新助手),
两者一起放入 tar.gz 发布到 Release(详见 [docs/update.md](docs/update.md))。

## 配置说明

首次运行自动创建 `data_store/` 目录，包含以下配置文件：

| 文件 | 说明 |
|------|------|
| `config/user_info.json` | 路径配置、账号密码、邮件开关 |
| `config/github.json` | GitHub 仓库、Token、更新配置 |
| `config/smtp.json` | SMTP 邮件服务器配置 |
| `config/proxy.json` | 代理配置 |
| `config/file_io.json` | 文件 IO 配置 |
| `config/version.json` | 本地版本号 |

> ⚠️ `data_store/` 包含明文密钥和 Cookie，已加入 `.gitignore`，禁止上传。

## 自更新流程

1. 主窗口就绪后静默检测 GitHub Release，发现新版本弹窗确认
2. 确认后主程序写入任务文件，以 GUI 方式启动同目录更新助手 `update.exe`，随后退出
3. 更新助手下载 tar.gz 包（内含主程序与更新助手），等待主程序进程退出
4. 解压到 `_update_stage/`，旧主程序改名 `.old_del` 备份，复制新文件
5. 成功则清理临时文件并启动新主程序，助手退出；失败自动回滚旧版本
6. 更新助手自身无法在运行时覆盖，新版先存为 `update.exe.new_pending`，主程序下次启动时替换

> 全程不生成/不启动任何 cmd、bat、VBS 脚本，因此 Windows 下不会出现控制台黑窗。
> 细节见 [docs/update.md](docs/update.md)。

## 重要注意事项

1. QQ 邮箱 SMTP：必须网页端开启 POP3/SMTP，使用**授权码**，不要填账号登录密码
2. GitHub 更新：需配置仓库地址，可选 Token 提高 API 限速
3. 批量操作：账号间有 10-50 秒随机间隔，避免风控
4. 自更新：发布包(tar.gz)需同时包含主程序与更新助手(`wuge_tools` + `update`)，缺助手时仅能下载需手动替换

## .gitignore 规则

- `data_store/` - 用户数据（账号/Cookie/配置），禁止上传
- `dist/`、`build/`、`*.exe` - 打包产物
- `__pycache__/`、`*.pyc` - Python 缓存
- `*.log` - 日志文件
- `p.py`、`test_*.py`、`text_*.py` - 测试脚本
