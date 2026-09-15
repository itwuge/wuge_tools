# 个人工具

> 基于 PySide6 的个人工具，支持单账号/批量操作、邮件通知、GitHub 自更新

## 功能特性

- **单账号操作**：输入账号密码即可执行，支持结果详情展示
- **批量操作**：多账号顺序执行，账号间防风控随机间隔，汇总邮件通知
- **邮件通知**：操作完成后自动发送结果邮件，支持 QQ 邮箱 SMTP
- **自更新**：检测 GitHub Release，自动下载更新包，解压覆盖并重启软件
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

前往 [Releases](https://github.com/itwuge/wuge_tools/releases) 页面下载对应平台的压缩包，解压后运行即可。

## 项目架构

```
个人工具/
├── main.py                     # 程序入口
├── comm_tools/                 # 通用工具层
├── infrastructure/             # 底层基础设施
├── service/                    # 业务层
└── runtime/                    # GUI运行时(MVC)
```

详细文档请查看左侧导航。
