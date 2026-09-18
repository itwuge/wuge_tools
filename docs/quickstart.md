# 快速开始

## 环境要求

- Python 3.9+
- pip

## 安装依赖

```bash
pip install -r requirements.txt
```

依赖列表：

```txt
requests>=2.31.0
urllib3>=2.0.0
PySocks>=1.7.1
PySide6>=6.5.0
beautifulsoup4>=4.12.0
```

## 运行软件

```bash
python main.py
```

首次运行会自动创建 `data_store/` 目录及默认配置文件（macOS 位于 `~/Library/Application Support/wuge_tools/`，程序目录只读）。

## 打包为可执行文件

### Windows

```bash
pyinstaller --noconfirm --clean wuge_tools.spec   # 主程序
pyinstaller --noconfirm --clean update.spec       # 更新助手
```

### Linux / macOS

多平台构建由 GitHub Actions 完成（见下节），本地按平台调用对应打包脚本：

```bash
python build_zip.py    # Windows: wuge_tools-Windows.zip
python build_tar.py    # Linux:   wuge_tools-Linux.tar.gz
python build_dmg.py    # macOS:   wuge_tools-macOS-{arm64|x86_64}.dmg
```

macOS 打包脚本会把 `update.app` 内嵌到主程序 `.app/Contents/Resources/` 下，
两者不分离；打包产物格式与 CI 产物一致。

## GitHub Actions 自动构建

项目已配置多平台自动构建工作流（`.github/workflows/build.yml`）：

- 推送 `v*` 标签时自动构建 Windows / Linux / macOS（arm64 + x86_64）四平台版本
- 自动创建 GitHub Release 并上传产物
- 手动触发：在 Actions 页面点击 "Run workflow"

触发方式：

```bash
git tag v1.0.0
git push origin v1.0.0
```

更新包资产名与客户端文件名模板一致（`wuge_tools-{平台}.{扩展名}`），
客户端据此精确/模糊匹配下载，详见 [自更新](update.md)。
