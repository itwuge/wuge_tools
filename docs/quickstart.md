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
PySide6>=6.5.0
beautifulsoup4>=4.12.0
```

## 运行软件

```bash
python main.py
```

首次运行会自动创建 `data_store/` 目录及默认配置文件。

## 打包为可执行文件

### Windows

```bash
pyinstaller --noconfirm --clean wuge_tools.spec
```

产物：`dist/wuge_tools.exe`

### Linux / macOS

```bash
pyinstaller --noconfirm --clean \
  --onefile --windowed \
  --name wuge_tools \
  --add-data "app_icon.png:." \
  main.py
```

## GitHub Actions 自动构建

项目已配置多平台自动构建工作流（`.github/workflows/build.yml`）：

- 推送 `v*` 标签时自动构建 Windows / Linux / macOS 三平台版本
- 自动创建 GitHub Release 并上传产物
- 手动触发：在 Actions 页面点击 "Run workflow"

触发方式：

```bash
git tag v1.0.0
git push origin v1.0.0
```
