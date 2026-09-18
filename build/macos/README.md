# macOS DMG 豆包式"双击即安装"引导制作说明

本目录包含让 DMG 打开后呈现**豆包官方安装引导效果**（渐变背景 + 居中图标 + 箭头 + "双击 安装"文案）的全部制作资产与脚本。

## 效果原理

DMG 打开后 Finder 显示的引导界面 = **背景图(静态元素) + Finder 图标(实际可双击)**：

| 部分 | 文件 | 作用 |
| --- | --- | --- |
| 背景图 | `make_dmg_background.py` 生成 `dmg_background.png` | 渐变底色、图标底衬卡片、**箭头**、"双击 安装 个人小工具"文案——全部是预先设计好的静态元素 |
| .DS_Store | `make_dsstore.py` 生成 | 控制窗口尺寸(600x342)、**图标位置**(Iloc 像素坐标)、背景图引用 |
| 打包流程 | `build_dmg_macos.sh` | 两阶段构建，确保用户首次挂载即正确显示 |

图标位置由 `.DS_Store` 的 `Iloc` 记录设为 `(302, 100)`（中心坐标），与背景图中底衬卡片中心对齐，引导用户双击图标启动**自安装流程**（见项目根 `main.py::_auto_install_from_dmg`）。

## 文件清单

- `make_dmg_background.py` — 生成背景图（**Pillow 跨平台**，Windows/macOS/CI 均可跑；需 `pip install Pillow`）
- `dmg_background.png` — 背景图成品（2x 1200x640，已生成可复用）
- `make_dsstore.py` — 修改 .DS_Store（需 `pip install ds_store mac_alias`，仅 macOS）
- `build_dmg_macos.sh` — 完整两阶段打包脚本（仅 macOS）
- `README.md` — 本说明

## 使用

```bash
# 1. (可选) 修改文案/配色后重新生成背景图(任意平台)
python make_dmg_background.py dmg_background.png

# 2. macOS 上打包(替换 build_dmg.py 的 macOS 分支或独立调用)
VOLNAME=wuge_tools-macOS-x86_64 bash build_dmg_macos.sh <dist>/个人小工具.app <dist>/wuge_tools-macOS-x86_64.dmg
```

## 关键踩坑记录（重要）

1. **不能从零创建 .DS_Store**：ds_store 库直接新建的 .DS_Store Finder 会整体忽略。必须先让 Finder 打开目录/卷生成原生 .DS_Store，再修改（build_dmg_macos.sh 阶段A 的 `open` 就是干这个）。
2. **图标位置用 `Iloc`（像素坐标，整数元组）**，不是 `icvo`：icvo 的 gridOffset 网格坐标在 Finder 14 上不生效。
3. **`arrangeBy` 是字符串 `'none'`**，不是整数。
4. **背景图 Alias 含 CNID，必须在卷挂载后生成**；交付卷必须是**从未在 Finder 打开过的新 UUID 卷**（两阶段流程的原因），否则命中 Finder 布局缓存导致看起来"没生效"。**阶段A/B 的临时卷名也必须唯一**（脚本用进程号 `$$` 后缀）——复用同名卷第二次运行会命中缓存（图标跑左上角、背景不加载）。
5. **背景图必须 tiff@144dpi（豆包官方同款）**：1200x640 的 PNG(72dpi) 被 Finder 按原尺寸显示，1200 宽在 600 窗口里只显示左半，箭头/文案被裁掉；tiff@144dpi 会被 Finder 按 600x320 缩放显示，内容完整。构建脚本里 `sips -s format tiff -s dpiWidth 144 -s dpiHeight 144` 转换。
6. `.DS_Store` 的 `Iloc` 值必须是**整数**元组（编码器要求）。

## 与自更新/自安装的配合

- 双击图标 → 主程序检测到从 `/Volumes/` 运行 → 自动复制到 `/Applications` 并打开（`main.py::_auto_install_from_dmg`）
- 更新器 `updater_app/installer.py` 精确匹配主程序名 `个人小工具.app`（防止误装）
- DMG 内**不再放独立"安装.app"**（v1.0.8 起移除），只有一个主程序图标
