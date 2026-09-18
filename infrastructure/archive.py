# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: archive.py
# 归属: infrastructure 基础设施层 —— 跨平台多格式压缩包打包/解包工具
# ------------------------------------------------------------------------------
# 文件用途:
#   跨平台多格式压缩包打包/解包工具(基础设施层,跨业务通用);依据文件扩展名
#   自动识别压缩格式,并分发到 zip/tar/7z/rar 对应处理器,为更新包解压、
#   数据备份打包等上层业务提供统一的 extract/pack 入口.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 infrastructure 基础设施层的最底层通用工具模块;仅依赖 Python 标准库
#   及可选第三方库,不依赖 runtime/updater_app 等任何业务层(依赖方向只能被
#   业务层调用);由更新下载、数据备份等上层模块通过公开函数调用.
#
#   关联组件:
#     - 上游: 更新下载、数据备份等上层业务模块
#     - 下游: 标准库 zipfile/tarfile;可选 py7zr/rarfile
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 按扩展名检测压缩格式(handler 标识)
#   2. 统一解压入口 extract(zip/tar 系列/7z/rar)
#   3. 统一打包入口 pack(zip/tar 系列/7z;rar 格式不开放仅支持解压)
#   4. 提供格式能力判断工具函数(可解压/可打包/扩展名清单)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责压缩包的解压与打包,不涉及网络下载或业务逻辑
#   - 成功返回 True,失败返回 False,具体错误信息通过模块 logger 输出
#   - .7z 和 .rar 为可选依赖,未安装时会抛 ImportError,由 extract/pack 统一捕获转为 False
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块全部为纯函数(无全局可变状态、无类实例),每次调用均在调用方线程中
#   同步执行;解压/打包过程为 CPU+IO 密集型,不建议在 UI 主线程直接调用大文件
#   操作,应放入工作线程;模块内部无锁、无队列、无线程池,多线程并发调用不同
#   文件完全安全(无共享资源竞争).
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、os、tarfile、zipfile、typing
#   - 第三方: py7zr(.7z 可选)、rarfile(.rar 可选,另需系统 unrar/WinRAR)
#   - 项目内: 无
# ==============================================================================
import logging  # 日志记录:解压/打包的过程与异常统一经模块 logger 输出
import os  # 路径与目录操作:扩展名切分、目录创建、目录树递归遍历
import tarfile  # tar 系列标准库:处理 .tar/.tar.gz/.tar.bz2/.tar.xz
import zipfile  # zip 标准库:处理 .zip 的解压与 DEFLATED 压缩打包
from typing import Any, Final, Optional  # 类型工具:Any 放宽 tar 模式串类型、Final 锁定常量表、Optional 可空返回

# 模块级日志器:命名空间 Infra.Archive,App 根 logger 绑定 handler 后自动写入软件运行日志
_logger = logging.getLogger("Infra.Archive")

# 支持的压缩格式扩展名映射
# value = (handler标识, 是否支持打包)
_FORMAT_MAP: Final[dict[str, tuple[str, bool]]] = {  # 扩展名 -> (处理器标识, 是否允许打包);Final 防止运行时被篡改
    ".zip": ("zip", True),  # ZIP:标准库即可解压与打包
    ".tar": ("tar", True),  # 纯 tar:无压缩归档包
    ".tar.gz": ("tar_gz", True),  # gzip 压缩的 tar
    ".tgz": ("tar_gz", True),  # .tar.gz 的简写扩展名,复用同一处理器
    ".tar.bz2": ("tar_bz2", True),  # bzip2 压缩的 tar
    ".tbz2": ("tar_bz2", True),  # .tar.bz2 的简写扩展名
    ".tar.xz": ("tar_xz", True),  # xz 压缩的 tar
    ".txz": ("tar_xz", True),  # .tar.xz 的简写扩展名
    ".7z": ("seven_z", True),  # 7z:依赖可选第三方库 py7zr
    ".rar": ("rar", False),  # RAR 格式不开放,仅能解压、不能打包
}

# tar 模式映射(解压 / 打包)
_TAR_MODES: Final[dict[str, str]] = {  # 解压侧:处理器标识 -> tarfile.open 的读模式串
    "tar": "r:",  # 只读、不做压缩转换
    "tar_gz": "r:gz",  # gzip 方式解压读取
    "tar_bz2": "r:bz2",  # bzip2 方式解压读取
    "tar_xz": "r:xz",  # xz 方式解压读取
}
_TAR_WRITE_MODES: Final[dict[str, str]] = {  # 打包侧:处理器标识 -> tarfile.open 的写模式串
    "tar": "w:",  # 纯打包、不压缩
    "tar_gz": "w:gz",  # gzip 压缩写入
    "tar_bz2": "w:bz2",  # bzip2 压缩写入
    "tar_xz": "w:xz",  # xz 压缩写入
}


def _detect_format(file_path: str) -> Optional[str]:
    """根据文件扩展名检测压缩格式,返回 handler 标识;未知格式返回 None

    :param file_path: 待检测的压缩包路径(扩展名大小写不敏感)
    :type file_path: str
    :return: 处理器标识(如 "zip"、"tar_gz");不支持的扩展名返回 None
    :rtype: Optional[str]
    """
    name = file_path.lower()  # 统一转小写,使扩展名匹配不受文件名大小写影响
    # 先匹配多段扩展名(.tar.gz 等)
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"):  # 必须优先匹配复合扩展名,否则 .tar.gz 会被 splitext 错切成 .gz
        if name.endswith(ext):  # 命中某个复合扩展名
            return _FORMAT_MAP[ext][0]  # 返回映射元组首项:处理器标识
    # 再匹配单段扩展名
    _, ext = os.path.splitext(name)  # splitext 只能取最后一段扩展名(如 .zip/.rar)
    if ext in _FORMAT_MAP:  # 单段扩展名在映射表内
        return _FORMAT_MAP[ext][0]  # 返回对应处理器标识
    return None  # 复合、单段扩展名均未命中:判定为未知格式


def _is_supported(file_path: str, require_pack: bool = False) -> bool:
    """检查文件格式是否支持;require_pack=True 时额外检查是否支持打包

    :param file_path: 待判断的文件路径
    :type file_path: str
    :param require_pack: 是否要求该格式具备打包能力;False 时仅判断能否解压
    :type require_pack: bool
    :return: 支持(且在要求打包时可打包)返回 True,否则 False
    :rtype: bool
    """
    fmt = _detect_format(file_path)  # 先识别处理器标识
    if fmt is None:  # 未知格式直接判定不支持
        return False
    if require_pack:  # 调用方要求"可打包"时,需反查映射表确认该处理器的 can_pack 标志
        for ext, (handler, can_pack) in _FORMAT_MAP.items():  # 同一处理器可能对应多个扩展名(如 .tgz),遍历查找匹配项
            if handler == fmt:  # 找到该处理器对应的表项
                return can_pack  # 返回其打包能力布尔值
        return False  # 理论不可达:格式已识别却找不到映射,按不支持做防御兜底
    return True  # 仅要求可解压:格式已识别即视为支持


# ==============================================================================
# 解压
# ==============================================================================
def extract(package_path: str, extract_dir: str) -> bool:
    """统一解压入口:根据扩展名自动选择格式,解压到指定目录

    :param package_path: 压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录(不存在时自动创建)
    :type extract_dir: str
    :return: True=成功,False=失败(格式不支持或解压过程抛异常)
    :rtype: bool
    """
    fmt = _detect_format(package_path)  # 识别压缩包格式
    if fmt is None:  # 未知格式无法分发处理器
        _logger.error(f"❌ 不支持的压缩格式:{package_path}")  # 记录错误,便于定位文件类型问题
        return False  # 直接返回失败,不做解压尝试

    os.makedirs(extract_dir, exist_ok=True)  # 确保目标目录存在;exist_ok=True 避免目录已存在时报错
    try:  # 各处理器均可能抛异常(文件损坏、缺少可选依赖、权限不足等),在此统一兜底
        if fmt == "zip":  # ZIP 走标准库处理器
            return _extract_zip(package_path, extract_dir)
        if fmt in _TAR_MODES:  # tar 系列(含 gz/bz2/xz)走同一处理器,仅打开模式不同
            return _extract_tar(package_path, extract_dir, fmt)
        if fmt == "seven_z":  # 7z 走 py7zr 处理器
            return _extract_7z(package_path, extract_dir)
        if fmt == "rar":  # rar 走 rarfile 处理器
            return _extract_rar(package_path, extract_dir)
    except Exception as e:  # 捕获解压过程中的任意异常,防止异常上抛中断上层业务流程
        _logger.error(f"❌ 解压失败:{package_path} -> {e}", exc_info=True)  # exc_info=True 输出完整堆栈,便于排查根因
        return False
    return False  # 所有格式分支均未命中时的防御性返回(理论不可达)


def _extract_zip(package_path: str, extract_dir: str) -> bool:
    """使用标准库 zipfile 解压 ZIP 压缩包

    :param package_path: ZIP 压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录
    :type extract_dir: str
    :return: 解压完成恒返回 True
    :rtype: bool
    :raises zipfile.BadZipFile: 压缩包损坏时抛出,由上层 extract 统一捕获
    """
    target_root = os.path.abspath(extract_dir)  # ZIP路径穿越校验基准目录
    with zipfile.ZipFile(package_path, "r") as zf:  # 以只读模式打开 ZIP;上下文管理器保证文件句柄关闭
        for info in zf.infolist():
            target = os.path.abspath(os.path.join(target_root, info.filename))
            if os.path.commonpath([target_root, target]) != target_root:
                raise ValueError(f"ZIP包含越界路径:{info.filename}")
        zf.extractall(path=target_root)  # 全部条目校验通过后统一解压
    _logger.info(f"📦 ZIP解压完成:{package_path} -> {extract_dir}")  # 记录解压成功结果
    return True


def _extract_tar(package_path: str, extract_dir: str, fmt: str) -> bool:
    """按 fmt 选择 tar 解压模式(r: / r:gz / r:bz2 / r:xz)

    :param package_path: tar 系列压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录
    :type extract_dir: str
    :param fmt: 处理器标识(tar/tar_gz/tar_bz2/tar_xz)
    :type fmt: str
    :return: 解压完成恒返回 True
    :rtype: bool
    :raises tarfile.TarError: 归档损坏或压缩格式不符时抛出,由上层 extract 捕获
    """
    mode: Any = _TAR_MODES.get(fmt, "r:*")  # 按格式取读模式;未命中时用 r:* 让 tarfile 自动探测压缩方式
    target_root = os.path.abspath(extract_dir)
    with tarfile.open(package_path, mode) as tar:  # 以对应模式打开 tar 包;with 保证关闭
        for member in tar.getmembers():
            target = os.path.abspath(os.path.join(target_root, member.name))
            if os.path.commonpath([target_root, target]) != target_root:
                raise ValueError(f"TAR包含越界路径:{member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"TAR包含不允许的链接成员:{member.name}")
        tar.extractall(path=target_root)  # 全部条目校验通过后统一解压
    _logger.info(f"📦 TAR({fmt})解压完成:{package_path} -> {extract_dir}")  # 记录实际使用的 tar 格式
    return True


def _extract_7z(package_path: str, extract_dir: str) -> bool:
    """使用第三方库 py7zr 解压 7z 压缩包

    :param package_path: .7z 压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录
    :type extract_dir: str
    :return: 解压完成恒返回 True
    :rtype: bool
    :raises ImportError: 未安装 py7zr 时抛出,提示用户 pip install py7zr
    """
    try:
        import py7zr  # type: ignore[import-not-found]  # 函数内延迟导入:仅在真遇到 .7z 时才依赖第三方库
    except ImportError:  # 环境未安装可选依赖
        _logger.error("❌ 解压 .7z 需要安装 py7zr: pip install py7zr")  # 给出明确的安装指引
        raise  # 继续上抛,由 extract 捕获并转为 False 返回
    with py7zr.SevenZipFile(package_path, mode="r") as z:  # 只读方式打开 7z;with 保证资源释放
        z.extractall(path=extract_dir)  # 全量解压
    _logger.info(f"📦 7Z解压完成:{package_path} -> {extract_dir}")
    return True


def _extract_rar(package_path: str, extract_dir: str) -> bool:
    """使用第三方库 rarfile 解压 RAR 压缩包(需系统安装 unrar/WinRAR)

    :param package_path: .rar 压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录
    :type extract_dir: str
    :return: 解压完成恒返回 True
    :rtype: bool
    :raises ImportError: 未安装 rarfile 时抛出
    :raises rarfile.RarCannotExec: 系统缺少 unrar/WinRAR 可执行程序时抛出
    """
    try:
        import rarfile  # type: ignore[import-not-found]  # 延迟导入:rarfile 为可选依赖
    except ImportError:  # Python 层依赖缺失
        _logger.error("❌ 解压 .rar 需要安装 rarfile + 系统 unrar/WinRAR")  # 提示 Python 库与系统程序两个前置条件
        raise  # 上抛由 extract 统一兜底
    with rarfile.RarFile(package_path) as rf:  # 打开 rar 文件;底层解压仍依赖系统 unrar
        rf.extractall(path=extract_dir)  # 全量解压
    _logger.info(f"📦 RAR解压完成:{package_path} -> {extract_dir}")
    return True


# 向后兼容:tar.gz 专用解压接口
def extract_targz(package_path: str, extract_dir: str) -> bool:
    """解压 tar.gz 包到指定目录(向后兼容,内部委托 extract)

    :param package_path: .tar.gz/.tgz 压缩包路径
    :type package_path: str
    :param extract_dir: 解压目标目录
    :type extract_dir: str
    :return: True=成功,False=失败
    :rtype: bool
    """
    return extract(package_path, extract_dir)  # 旧调用方保留此函数名,实际复用统一入口完成格式分发


# ==============================================================================
# 打包
# ==============================================================================
def pack(source_path: str, output_path: str) -> bool:
    """统一打包入口:根据输出扩展名自动选择格式

    :param source_path: 待打包的文件或目录
    :type source_path: str
    :param output_path: 输出压缩包路径(扩展名决定格式)
    :type output_path: str
    :return: True=成功,False=失败
    :rtype: bool
    """
    if not os.path.exists(source_path):  # 打包前先确认源存在,避免各处理器产生晦涩的底层异常
        _logger.error(f"❌ 打包源不存在:{source_path}")
        return False

    fmt = _detect_format(output_path)  # 注意:打包时依据"输出路径"扩展名决定格式
    if fmt is None:  # 输出扩展名不在支持列表
        _logger.error(f"❌ 不支持的输出压缩格式:{output_path}")
        return False

    # 检查该格式是否支持打包
    can_pack = False  # 先假定不可打包
    for ext, (handler, cp) in _FORMAT_MAP.items():  # 反查映射表确认该处理器的打包标志
        if handler == fmt:  # 匹配到当前格式对应表项
            can_pack = cp  # 取出是否允许打包
            break  # 已找到,无需继续遍历
    if not can_pack:  # 如 rar 仅支持解压
        _logger.error(f"❌ 格式 {fmt} 不支持打包(仅解压)")
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)  # 确保输出目录存在;abspath 后 dirname 不会为空,空串时兜底当前目录
    try:  # 打包过程统一异常兜底
        if fmt == "zip":  # ZIP 打包
            return _pack_zip(source_path, output_path)
        if fmt in _TAR_WRITE_MODES:  # tar 系列打包
            return _pack_tar(source_path, output_path, fmt)
        if fmt == "seven_z":  # 7z 打包
            return _pack_7z(source_path, output_path)
    except Exception as e:  # 任意异常(磁盘满、权限、缺依赖)均转为失败返回
        _logger.error(f"❌ 打包失败:{source_path} -> {output_path} | {e}", exc_info=True)
        return False
    return False  # 分支未命中的防御性返回(理论不可达)


def _pack_zip(source_path: str, output_path: str) -> bool:
    """将单个文件或整个目录打包为 ZIP(DEFLATED 压缩)

    :param source_path: 待打包的文件或目录
    :type source_path: str
    :param output_path: 输出 .zip 文件路径
    :type output_path: str
    :return: 打包完成恒返回 True
    :rtype: bool
    :raises OSError: 目录遍历或文件写入失败时抛出,由上层 pack 捕获
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:  # 写模式 + DEFLATED 压缩算法;with 负责收尾关闭
        if os.path.isfile(source_path):  # 源是单个文件
            zf.write(source_path, arcname=os.path.basename(source_path))  # arcname 只取文件名,避免把绝对路径目录结构写进压缩包
        else:  # 源是目录:递归写入目录下全部文件
            for root, dirs, files in os.walk(source_path):  # os.walk 自上而下遍历目录树
                for f in files:  # 只写文件;ZIP 格式本身不保留空目录
                    full = os.path.join(root, f)  # 拼出文件在磁盘上的完整路径
                    arc = os.path.relpath(full, os.path.dirname(source_path))  # 以源的父目录为基准算相对路径,使压缩包内保留"源目录名"这一层
                    zf.write(full, arcname=arc)  # 按相对归档名写入压缩包
    _logger.info(f"📦 ZIP打包完成:{source_path} -> {output_path}")
    return True


def _pack_tar(source_path: str, output_path: str, fmt: str) -> bool:
    """将文件或目录打包为 tar 系列格式(按 fmt 决定压缩方式)

    :param source_path: 待打包的文件或目录
    :type source_path: str
    :param output_path: 输出 tar 系列压缩包路径
    :type output_path: str
    :param fmt: 处理器标识(tar/tar_gz/tar_bz2/tar_xz)
    :type fmt: str
    :return: 打包完成恒返回 True
    :rtype: bool
    :raises tarfile.TarError: 打包失败时抛出,由上层 pack 捕获
    """
    mode: Any = _TAR_WRITE_MODES.get(fmt, "w:")  # 取对应写模式;未命中时退化为纯 tar 写入
    with tarfile.open(output_path, mode) as tar:  # 按模式创建压缩包;with 保证关闭
        tar.add(source_path, arcname=os.path.basename(source_path))  # tar.add 自行递归处理目录;arcname 取基名,避免写入绝对路径
    _logger.info(f"📦 TAR({fmt})打包完成:{source_path} -> {output_path}")
    return True


def _pack_7z(source_path: str, output_path: str) -> bool:
    """将文件或目录打包为 7z 压缩包(依赖 py7zr)

    :param source_path: 待打包的文件或目录
    :type source_path: str
    :param output_path: 输出 .7z 文件路径
    :type output_path: str
    :return: 打包完成恒返回 True
    :rtype: bool
    :raises ImportError: 未安装 py7zr 时抛出
    """
    try:
        import py7zr  # type: ignore[import-not-found]  # 延迟导入可选依赖
    except ImportError:  # 缺少 py7zr
        _logger.error("❌ 打包 .7z 需要安装 py7zr: pip install py7zr")
        raise  # 上抛由 pack 捕获
    with py7zr.SevenZipFile(output_path, "w") as z:  # 写模式创建 7z 文件
        if os.path.isfile(source_path):  # 单文件分支
            z.write(source_path, arcname=os.path.basename(source_path))  # 以纯文件名作为归档名
        else:  # 目录分支:手动递归写入(py7zr 写接口需逐文件添加)
            for root, dirs, files in os.walk(source_path):  # 递归遍历目录树
                for f in files:  # 逐个文件写入
                    full = os.path.join(root, f)  # 文件完整磁盘路径
                    arc = os.path.relpath(full, os.path.dirname(source_path))  # 相对源父目录的归档路径,保留源目录名层级
                    z.write(full, arcname=arc)  # 按相对路径写入 7z
    _logger.info(f"📦 7Z打包完成:{source_path} -> {output_path}")
    return True


# ==============================================================================
# 工具函数
# ==============================================================================
def supported_extensions() -> list:
    """返回当前支持的所有压缩格式扩展名列表

    :return: 扩展名字符串列表(如 [".zip", ".tar", ".tar.gz", ...])
    :rtype: list
    """
    return list(_FORMAT_MAP.keys())  # 直接由格式映射表的键生成,保证返回值与实际支持集合始终一致


def is_extractable(file_path: str) -> bool:
    """判断文件是否为可解压的压缩包

    :param file_path: 待判断的文件路径
    :type file_path: str
    :return: 扩展名受支持(可解压)返回 True,否则 False
    :rtype: bool
    """
    return _is_supported(file_path, require_pack=False)  # 仅要求解压能力


def is_packable(output_path: str) -> bool:
    """判断输出路径扩展名是否为可打包格式

    :param output_path: 计划输出的压缩包路径
    :type output_path: str
    :return: 该扩展名支持打包返回 True(如 rar 返回 False),否则 False
    :rtype: bool
    """
    return _is_supported(output_path, require_pack=True)  # 要求格式同时具备打包能力


# 模块公开 API 符号表:控制 from infrastructure.archive import * 的导出范围
# 所有以下划线 _ 开头的函数(如 _detect_format、_extract_zip 等)均为内部实现,禁止外部直接调用
__all__ = [  # 显式声明模块对外公开的函数名列表,形成清晰的 API 边界
    "extract",  # 统一解压入口:根据扩展名自动识别格式并解压到指定目录
    "extract_targz",  # tar.gz专用解压函数(向后兼容,内部委托extract)
    "pack",  # 统一打包入口:根据输出扩展名自动选择格式进行打包
    "supported_extensions",  # 工具函数:返回当前支持的所有压缩格式扩展名列表
    "is_extractable",  # 工具函数:判断文件是否为可解压的压缩格式
    "is_packable",  # 工具函数:判断输出路径扩展名是否为可打包格式
]
