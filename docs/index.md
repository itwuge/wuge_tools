# 个人工具

> 基于 PySide6 的个人工具，支持操作、文件传输下载、邮件通知、GitHub 自更新

## 功能特性

- **工具**：单账号/批量操作，账号间防风控随机间隔，支持结果详情展示
- **邮件通知**：操作完成后自动发送结果邮件，支持 QQ 邮箱 SMTP
- **文件传输**：GitHub Release 资产浏览/下载 + TUS 协议上传，下载器 GitHub 配置独立于自更新
- **自更新**：检测 GitHub Release，独立更新助手自动下载、安装并重启
  - Windows/Linux 解压替换，macOS 挂载 DMG 安装（更新助手内嵌于主程序 .app 包内）
- **路径配置**：可自定义数据存储目录、下载目录、日志目录等
- **代理配置**：支持系统代理 / 手动代理切换
- **系统配置**：GitHub Token、SMTP 邮件、文件 IO 等配置

## 快速开始

### 源码运行

```bash
pip install -r requirements.txt
python main.py
```

### 下载预编译版本

前往 [Releases](https://github.com/itwuge/wuge_tools/releases) 页面下载对应平台的安装包：

| 平台 | 更新包 |
|------|--------|
| Windows | `wuge_tools-Windows.zip` |
| Linux | `wuge_tools-Linux.tar.gz` |
| macOS (Apple Silicon) | `wuge_tools-macOS-arm64.dmg` |
| macOS (Intel) | `wuge_tools-macOS-x86_64.dmg` |

macOS 的 `update.app` 已内嵌于主程序 `.app` 包内，拖入"应用程序"即可同时获得主程序与更新助手。

## 项目架构

```
个人工具/
├── main.py                     # 程序入口
├── runtime/                    # GUI运行时(MVC):view/controller/model/workers
├── service/                    # 业务层:/更新检测/邮件/下载
├── infrastructure/             # 底层基础设施:HTTP/GitHub API/文件传输
├── updater_app/                # 独立更新助手(自带MVC)
├── docs/                       # 文档站源码
└── data_store/                 # 用户数据(配置/账号/Cookie,禁止上传)
```

详细文档请查看左侧导航。
