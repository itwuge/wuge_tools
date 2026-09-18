#!/bin/bash
# ============================================================================
# macOS DMG 豆包式"双击即安装"打包流程
# ----------------------------------------------------------------------------
# 方案: 【模板法单阶段】——不依赖 Finder(CI 可跑)
#
#   传统做法需要在打包机上用 Finder 打开卷生成"原生 .DS_Store",
#   但 GitHub Actions 的 macOS runner 无 Finder GUI, 该步骤会失败。
#   本脚本改用"预置模板": 把 Finder 原生生成的 .DS_Store 作为
#   dsstore_template.bin 提交到仓库(build/macos/), 打包时:
#
#   1. 组装源(主程序.app + 背景图 .background/background.tiff)
#   2. hdiutil create 生成全新 UDRW 卷(新 UUID, Finder 从未打开过)
#      ⚠ 必须加 -fs HFS+: CI 的 -srcfolder 默认卷是 APFS, mac_alias 在
#        APFS 上生成的背景图 Alias 其 CNID 为 64 位, Finder 解析失败
#        (背景图不显示, 已实测); HFS+ 卷 CNID 32 位, Alias 有效。
#   3. 把模板 .DS_Store 复制进卷内
#   4. make_dsstore.py 改写布局(背景图 Alias 指向本卷 + 图标位置 + 窗口)
#      (模板结构被 Finder 认可; Alias 在卷挂载后生成, CNID 有效)
#   5. 卸载 → convert 成 UDZO(最终交付格式)
#
#   ⚠ 为什么必须用模板而不是从零创建 .DS_Store:
#     Finder 只认可它自己生成的 .DS_Store 结构(页大小 4096 + 各 B-tree
#     记录), 用库从零创建的(即使页大小对齐)会被 Finder 整体忽略。
#     详见 make_dsstore.py 头部的踩坑记录。
#
# 依赖(见 requirements.txt / .github/workflows/build.yml):
#   - python3 + pip install ds_store mac_alias
#     (ds_store 需用仓库内 vendor 修复版, make_dsstore.py 自动加载)
#   - Xcode/swift(生成背景图, CI 自带) 或直接复用 dmg_background.png
#
# 用法:
#   ./build_dmg_macos.sh <app目录/个人小工具.app> <输出dmg路径>
# 示例:
#   ./build_dmg_macos.sh dist/个人小工具.app dist/wuge_tools-macOS-x86_64.dmg
#   环境变量 VOLNAME 可指定最终卷名(更新器按此匹配, 默认 wuge_tools-macOS-x86_64)
# ============================================================================
set -euo pipefail

# ---- 参数 ----------------------------------------------------------------
APP_PATH="${1:?用法: $0 <个人小工具.app路径> <输出.dmg路径>}"
OUT_DMG="${2:?用法: $0 <个人小工具.app路径> <输出.dmg路径>}"
VOLNAME="${VOLNAME:-wuge_tools-macOS-x86_64}"   # 最终卷名(更新器按此匹配)

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BG_PNG="$SCRIPT_DIR/dmg_background.png"          # 背景图源(2x 1200x640 PNG)
BG_NAME="background.tiff"                         # 卷内背景图文件名(豆包同款 tiff)
APP_NAME="个人小工具.app"                              # 卷内应用图标名(用于 AppleScript 定位)
TEMPLATE="$SCRIPT_DIR/dsstore_template.bin"      # Finder 原生 .DS_Store 模板

# 将 PNG 转成 Finder 按 DPI 缩放的 tiff(豆包官方 144 DPI = 2x Retina)
# 关键: PNG 无 DPI 元数据(72dpi)时 Finder 按原尺寸显示, 1200 宽会被窗口裁切,
#       箭头/文案跑到可视区外; tiff@144dpi 则 Finder 按 600x320 显示完整内容
make_bg_tiff() {
    sips -s format tiff -s dpiWidth 144 -s dpiHeight 144 "$BG_PNG" --out "$1" >/dev/null
}

# 前置检查
[ -d "$APP_PATH" ] || { echo "错误: 应用目录不存在: $APP_PATH" >&2; exit 1; }
[ -f "$TEMPLATE" ] || { echo "错误: .DS_Store 模板不存在: $TEMPLATE" >&2; exit 1; }
[ -f "$BG_PNG" ] || { echo "错误: 背景图不存在: $BG_PNG" >&2; exit 1; }

# 工作目录
WORK="$(mktemp -d /tmp/dmg_build.XXXXXX)"
STAGE="$WORK/stage"        # 打包源: app + .background
DMG="$WORK/wuge_tools.dmg"

echo "== 组装打包源 =="
mkdir -p "$STAGE/.background"
cp -R "$APP_PATH" "$STAGE/"
make_bg_tiff "$STAGE/.background/$BG_NAME"

echo "== 创建 UDRW 卷并挂载(生成有效 Alias) =="
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -fs HFS+ -ov -format UDRW "$DMG" >/dev/null
MP="$(hdiutil attach "$DMG" -nobrowse -plist | python3 -c \
  "import plistlib,sys; d=plistlib.loads(sys.stdin.buffer.read()); print([e.get('mount-point','') for e in d['system-entities'] if e.get('mount-point')][-1])")"
echo "   挂载点: $MP"

echo "== 生成 .DS_Store(背景图 Alias + 图标位置 + 窗口) =="
cp "$TEMPLATE" "$MP/.DS_Store"
python3 "$SCRIPT_DIR/make_dsstore.py" "$MP" "$MP/.background/$BG_NAME"

echo "== 卸载并压缩(不打开 Finder, 无占用) =="
for i in 1 2 3 4 5; do
    if hdiutil detach "$MP" -force >/dev/null 2>&1; then
        echo "   卷已卸载(第 $i 次尝试成功)"
        break
    fi
    echo "   卸载重试 ${i}/5..." >&2
    sleep 3
done
if [ -d "$MP" ]; then
    diskutil unmount force "$MP" >/dev/null 2>&1 || true
    sleep 2
fi
if [ -d "$MP" ]; then
    echo "错误: 卷 $MP 无法卸载" >&2
    exit 16
fi
hdiutil convert "$DMG" -format UDZO -o "$OUT_DMG" >/dev/null
echo "== 打包完成: $OUT_DMG =="

# 清理临时文件
rm -rf "$WORK"
echo "== 全部完成 =="
