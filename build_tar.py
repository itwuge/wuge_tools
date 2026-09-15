# build_tar.py
import os
import tarfile


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
    out_name = f"wuge_tools-{os_name}.tar.gz"

    # 打包tar
    with tarfile.open(out_name, "w:gz") as t:
        t.add(os.path.join(root, bin_name), arcname=bin_name)
        t.add(os.path.join(root, "update"), arcname="update")
    print(f"打包完成: {out_name}")
    print("archive members:", [bin_name, "update"])

    # 内置校验
    with tarfile.open(out_name, "r:gz") as t:
        names = t.getnames()
        print("tar内文件列表:", names)
        assert "个人小工具" in names
        assert "update/update" in names
        assert any(n.startswith("update/lib/") for n in names)
        black = {"data_store","accounts","checkin_record","cookies","downloads","logs","mail_result"}
        for n in names:
            top_dir = n.split("/")[0]
            assert top_dir not in black, f"禁止目录被打包: {top_dir}"
    print("✅ archive layout ok")

if __name__ == "__main__":
    main()
