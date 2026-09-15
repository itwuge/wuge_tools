# 配置说明

首次运行自动创建 `data_store/` 目录，包含以下配置文件：

## 配置文件列表

| 文件 | 说明 |
|------|------|
| `config/user_info.json` | 路径配置、账号密码、邮件开关 |
| `config/github.json` | GitHub 仓库、Token、更新配置 |
| `config/smtp.json` | SMTP 邮件服务器配置 |
| `config/proxy.json` | 代理配置 |
| `config/file_io.json` | 文件 IO 配置 |
| `config/version.json` | 本地版本号 |

## 路径配置

在软件"路径配置"标签页中可设置：

- **数据目录**：存储账号、Cookie、操作记录等
- **下载目录**：更新包下载位置
- **日志目录**：`app.log` 日志文件位置
- **邮件目录**：邮件正文文件位置

## 邮件配置

QQ 邮箱 SMTP 配置：

1. 网页端登录 QQ 邮箱
2. 设置 → 账户 → 开启 POP3/SMTP 服务
3. 获取**授权码**（不是登录密码）
4. 在软件"系统配置"中填入授权码

## 代理配置

- **自动**：跟随系统代理
- **手动**：填写代理地址和端口
- **关闭**：不使用代理

## 注意事项

> ⚠️ `data_store/` 包含明文密钥和 Cookie，已加入 `.gitignore`，禁止上传到 Git 仓库。
