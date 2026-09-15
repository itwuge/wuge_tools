# build_zip.py
import os
import zipfile


def main():
    root = "dist"
    items = os.listdir(root)
    bin_name: str | None = None
    has_update = False
    for n in items:
        full_path = os.path.join(root, n)
        if not os.path.isdir(full_path):
            bin_name = n
        if n == "update" and os.path.isdir(full_path):
            has_update = True

    if bin_name is None or not has_update:
        raise ValueError(f"缺少打包成员:bin={bin_name}, has_update={has_update}")

    os_name = os.environ["OS_NAME"]
    out_name = f"wuge_tools-{os_name}.zip"

    # 打包
    with zipfile.ZipFile(out_name, "w", zipfile.ZIP_DEFLATED) as z:
        src_file = os.path.join(root, bin_name)
        z.write(src_file, arcname=bin_name)
        update_root = os.path.join(root, "update")
        for dp, _, fs in os.walk(update_root):
            for f in fs:
                fp = os.path.join(dp, f)
                arc = os.path.relpath(fp, root)
                z.write(fp, arcname=arc)
    print(f"打包完成: {out_name}")
    print("archive members:", [bin_name, "update"])

    # ========== 内置校验逻辑 ==========
    with zipfile.ZipFile(out_name, "r") as z:
        raw_names = z.namelist()
        names = []
        for raw in raw_names:
            try:
                # Windows zip中文文件名cp437转gbk
                decoded = raw.encode("cp437").decode("gbk")
            except Exception:
                decoded = raw
            names.append(decoded)
        print("ZIP内文件列表:", names)
        assert "个人小工具.exe" in names
        assert "update/update.exe" in names
        assert any(n.startswith("update/lib/") for n in names)
        # 黑名单目录检查
        black = {"data_store","accounts","checkin_record","cookies","downloads","logs","mail_result"}
        for n in names:
            top_dir = n.replace("\\","/").split("/")[0]
            assert top_dir not in black, f"禁止目录被打包: {top_dir}"
    print("✅ archive layout ok")

if __name__ == "__main__":
    main()