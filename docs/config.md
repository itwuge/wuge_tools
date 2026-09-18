# 配置说明

首次运行自动创建 `data_store/` 目录（macOS 位于 `~/Library/Application Support/wuge_tools/`，程序目录只读，数据不随更新丢失），配置文件统一由 `config/config_manifest.json` 定位模块文件名。

## 配置文件列表

| 文件 | 说明 |
|------|------|
| `config/config_manifest.json` | 配置模块定位清单 |
| `config/user_info.json` | 路径配置、账号密码、邮件开关 |
| `config/github.json` | **自更新** GitHub 仓库、Token、更新配置 |
| `config/transfer_github.json` | **文件传输下载器专用** GitHub 配置（与自更新独立） |
| `config/smtp.json` | SMTP 邮件服务器配置 |
| `config/proxy.json` | 代理配置 |
| `config/file_io.json` | 文件 IO 配置（分片大小/并发数） |
| `config/version.json` | 本地版本号、更新包模板、自动检查开关 |

## 主界面三个 Tab

- **工具**：账号管理（添加/删除/列表）、单账号/批量、日志
- **文件传输**：GitHub Release 资产浏览下载 + TUS 上传（下载器）
- **配置管理**：路径、邮件、GitHub、文件 IO、网络代理配置

## GitHub 配置（自更新 vs 下载器）

**自更新**（`github.json`）：仓库所有者/仓库名/访问令牌/API 地址/请求超时，
由"配置管理 → GitHub 配置"或更新助手内表单修改，影响版本检测与更新包匹配。

**下载器**（`transfer_github.json`）：文件传输 Tab 内可直接编辑并"保存配置"，
仅用于 Release 资产拉取与下载，**不影响自更新检测**。
首次使用会从 `github.json` 拷贝一次作为初始默认值，之后完全独立。

## 路径配置

在软件"配置管理 → 路径配置"中可设置：

- **数据目录**：存储账号、Cookie、记录等
- **下载目录**：更新包下载位置
- **日志目录**：`app.log` 日志文件位置
- **邮件目录**：邮件正文文件位置

## 邮件配置

QQ 邮箱 SMTP 配置：

1. 网页端登录 QQ 邮箱
2. 设置 → 账户 → 开启 POP3/SMTP 服务
3. 获取**授权码**（不是登录密码）
4. 在软件"配置管理 → 邮件配置"中填入授权码

## 文件 IO 配置

- **下载分片大小**（默认 1MB）：多线程下载单片大小
- **下载并发数**（默认 4）：下载并行线程数
- **上传分片大小**（默认 5MB）：TUS 上传单片大小
- **上传并发数**（默认 3）：上传并行线程数

该配置仅下载器使用，可在文件传输 Tab 或配置管理 Tab 修改。

## 代理配置

- **自动**：跟随系统代理
- **手动**：填写代理地址和端口
- **关闭**：不使用代理

## 注意事项

> ⚠️ `data_store/` 包含明文密钥和 Cookie，已加入 `.gitignore`，禁止上传到 Git 仓库。
