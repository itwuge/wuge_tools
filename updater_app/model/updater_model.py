# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: updater_model.py
# 归属: updater_app/model —— 更新助手数据模型(UpdaterModel)
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手 Model 层核心模块——任务信息 + 全局配置 + GitHub 表单读写.
#   作为 MVC 架构中的数据层,持有更新任务的所有状态信息,
#   并提供配置读写、版本管理、设置表单数据聚合等功能.
# ------------------------------------------------------------------------------
# 架构定位:
#   MVC 架构中的 Model 层(数据模型层),与主程序 MainModel 同构.
#   只做数据读写,不创建控件、不 import 任何 View/Worker.
#   配置以主程序透传的任务 JSON 为初始快照;用户在助手界面保存 GitHub 配置后,
#   立即落盘(github.json/version.json)并重新从磁盘加载全量配置,
#   后续检测/下载使用新配置.
#   数据目录由入口在 import app_config 前通过 SERVICE_DATA_DIR 环境变量注入,
#   因此本层所有配置路径与主程序完全一致.
#
#   关联组件:
#     - 上游: UpdateAssistantController(Controller,调用 Model 读写数据)
#     - 下游:
#       - config_manager(配置管理工具)
#       - bootstrap(路径检测工具)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 任务信息持有(app_exe、app_dir、old_pid、版本号等)
#   2. 全局配置管理(加载、刷新、版本更新)
#   3. 更新服务设置表单数据聚合(get_update_settings_form)
#   4. 更新服务设置保存与校验(save_update_settings_form)
#   5. 任务文件清理(remove_task_file)
# ------------------------------------------------------------------------------
# 职责边界:
#   1. 只做数据读写,不创建控件、不 import 任何 View/Worker
#   2. 不直接操作 UI,不启动线程
#   3. 不做网络请求,不执行业务流程
# ------------------------------------------------------------------------------
# 线程模型:
#   Model 实例在 Qt 主线程中创建和主要访问.
#   配置文件的写入操作由 config_manager 的线程锁保护,
#   因此 Model 的配置读写是线程安全的.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging, os, sys, typing
#   - 第三方: 无
#   - 项目内:
#       updater_app.model.config_manager(配置管理工具)
#       updater_app.bootstrap(路径检测工具)
# ==============================================================================

# 导入日志记录库,用于记录模型层的日志
import logging
# 导入操作系统接口库,用于路径操作和文件操作
import os
# 导入系统相关库,用于获取可执行文件名等
import sys
# 导入类型提示库,用于函数签名的类型注解
from typing import Any, Dict, Tuple

# 从配置管理模块导入需要的函数
from updater_app.model.config_manager import (
    _read_module_json,           # 读取单个模块配置
    is_installed_edition,        # 安装版识别(存在 installed.flag)
    _write_module_json,          # 写入单个模块配置
    get_update_download_dir,     # 获取更新下载目录
    load_and_merge_all_config,   # 加载并合并全部配置
    render_update_save_filename, # 渲染更新包文件名
    save_github_config,          # 保存 GitHub 配置
    save_proxy_config,           # 保存代理配置
    save_user_info,              # 保存用户信息配置
)
# 从 bootstrap 模块导入需要的常量和函数
from updater_app.bootstrap import MAIN_EXE_NAME, detect_app_dir, detect_main_exe

# 创建模块级日志器实例,命名空间为 Updater.Model
_logger = logging.getLogger("Updater.Model")


# ==============================================================================
# 类: UpdaterModel
# ==============================================================================
# =========** [UpdaterModel][Model] =========
# 关联组件:
#   - [UpdateAssistantController][Controller]: 被 Controller 持有和调用
#   - [config_manager][Model/Infra]: 调用 config_manager 读写配置文件
#   - [bootstrap][Infra]: 调用 bootstrap 的路径检测工具
# ==============================================================================
class UpdaterModel:
    """
    更新助手数据模型类,持有更新任务的所有状态和配置.

    详细说明:
        自给自足设计: 任务 JSON 未提供的字段一律自行从运行环境/磁盘配置补齐,
        因此主程序移交(launch_mode="task")与独立双击(launch_mode="standalone")
        可复用同一个 Model 与后续 MVC 链路.

    Attributes:
        task (Dict[str, Any]): 任务 JSON 字典(独立模式为 bootstrap 自举的默认任务)
        task_file_path (str): 任务 JSON 文件路径(独立模式为空;安装结束后仅任务模式删除)
        launch_mode (str): 启动模式: "task"=主程序移交(结束后负责重启主程序);
                           "standalone"=独立运行(不代启动)
        app_dir (str): 软件安装目录的绝对路径(替换目标)
        main_exe_name (str): 主程序文件名(按名等待/定位)
        app_exe (str): 待更新的主程序绝对路径(同目录未找到时为空,安装前置校验拦截)
        old_pid (int): 旧主程序 PID(任务模式精准等待;独立模式为 0,安装时按进程名等待)
        self_exe_name (str): 更新助手自身的可执行文件名
        local_version (str): 本地版本号(缺省从 version.json 读取)
        remote_version (str): 远程版本号(检测后由 Controller 回填)
        _config (Dict[str, Any]): 内存中的全量配置缓存(嵌套字典,模块名为键)
    """

    def __init__(self, task: Dict[str, Any], task_file_path: str = "",
                 launch_mode: str = "standalone"):
        """
        初始化更新助手数据模型.

        详细说明:
            根据传入的任务字典初始化所有属性,任务中缺失的字段自动检测或从配置读取.
            支持两种启动模式: 任务模式和独立模式,都会走同一条初始化链路.

        参数:
            task (Dict[str, Any]): 任务字典,包含 app_dir、app_exe、old_pid、config 等字段
            task_file_path (str): 任务 JSON 文件的路径;默认为空串
            launch_mode (str): 启动模式,"task" 或 "standalone";默认为 "standalone"

        返回值:
            None

        异常:
            无
        """
        # 保存任务字典,为空时使用空字典
        self.task = task or {}
        # 保存任务文件路径
        self.task_file_path = task_file_path
        # 保存启动模式,确保值合法(只接受 task 或 standalone)
        self.launch_mode = launch_mode if launch_mode in ("task", "standalone") else "standalone"
        # 软件目录: 任务中指定优先,否则自动检测,转为绝对路径
        self.app_dir = os.path.abspath(
            str(self.task.get("app_dir", "")).strip() or detect_app_dir()
        )
        # 主程序名: 任务指定 > 默认值;detect_main_exe 按此名优先匹配,找不到自动扫描目录
        self.main_exe_name = str(self.task.get("main_exe_name", "")).strip() or MAIN_EXE_NAME
        # 主程序路径: 任务指定 > 同目录按名定位(自动扫描兜底);都没有则留空
        # 先获取任务中的 app_exe
        task_exe = str(self.task.get("app_exe", "")).strip()
        # 任务中有则用任务值,否则自动检测
        self.app_exe = (
            os.path.abspath(task_exe)
            if task_exe
            else detect_main_exe(self.app_dir, self.main_exe_name)
        )
        # 旧主程序 PID: 从任务中获取,转整数,失败则为 0
        try:
            self.old_pid = int(self.task.get("old_pid", 0) or 0)
        except (TypeError, ValueError):
            # 类型转换失败,默认 0
            self.old_pid = 0
        # 更新助手自身的可执行文件名(从 sys.executable 提取)
        self.self_exe_name = os.path.basename(os.path.abspath(sys.executable))
        # 新流程始终按config_root/config_files从磁盘读取真实配置,避免任务复制配置快照后发生漂移
        self._config: Dict[str, Any] = load_and_merge_all_config()
        # 旧任务兼容:仅当磁盘加载不到有效模块时才使用旧config快照补缺,不覆盖真实磁盘配置
        task_cfg = self.task.get("config")
        if isinstance(task_cfg, dict):
            for module_name, module_cfg in task_cfg.items():
                if module_name not in self._config and isinstance(module_cfg, dict):
                    self._config[module_name] = dict(module_cfg)
        # 本地版本: 任务透传 > version.json;远程版本任务可预置,检测后由 Controller 回填
        # 优先从任务获取,为空则从配置中读取 APP_VERSION
        self.local_version = str(self.task.get("local_version", "")).strip() or \
            str(self._config.get("version", {}).get("APP_VERSION", ""))
        # 远程版本: 从任务获取,默认为空
        self.remote_version = str(self.task.get("remote_version", ""))

    @property
    def should_restart_main(self) -> bool:
        """
        判断是否应该重启主程序(任务模式下重启,独立模式不重启).

        详细说明:
            任务模式下主程序已自行退出,助手结束时负责重启;
            独立模式下不代用户拉起主程序(用户自己启动的自己管).

        返回值:
            bool: True 表示需要重启主程序;False 表示不需要

        异常:
            无
        """
        # 任务模式下返回 True,独立模式下返回 False
        return self.launch_mode == "task"

    # ------------------------------------------------------------------
    # 全局配置(供 Worker 使用)
    # ------------------------------------------------------------------
    def global_config(self) -> Dict[str, Any]:
        """
        返回当前生效的全量配置(版本/GitHub/代理/文件 IO 等).

        参数:
            无

        返回值:
            Dict[str, Any]: 全量配置字典,结构为 {模块名: {配置键: 值}}

        异常:
            无
        """
        # 直接返回内存中的配置缓存
        return self._config

    def reload_config_from_disk(self) -> Dict[str, Any]:
        """
        从数据目录重新加载全量配置(保存 GitHub 配置后调用).

        详细说明:
            当配置被修改后(如用户保存了新的设置),
            需要从磁盘重新加载以确保内存配置与磁盘一致.

        参数:
            无

        返回值:
            Dict[str, Any]: 重新加载后的全量配置字典

        异常:
            无
        """
        # 从磁盘重新加载全量配置并更新内存缓存
        self._config = load_and_merge_all_config()
        # 返回新的配置
        return self._config

    def update_local_version(self, new_version: str) -> None:
        """
        安装成功后写入最新版本号到 version.json 并刷新内存配置.

        详细说明:
            远程 tag 可能带 v 前缀(如 v1.2.3),本地版本号格式不带前缀(如 1.2.3),
            写入前统一去除 v/V 前缀,保证重启后版本比较口径一致.

        参数:
            new_version (str): 新版本号字符串(可能带 v 前缀)

        返回值:
            None

        异常:
            无
        """
        # 去除首尾空白
        ver = (new_version or "").strip()
        # 去除 v/V 前缀(统一格式)
        if ver.lower().startswith(("v", "V")):
            ver = ver[1:]
        # 版本号为空则跳过并记录警告
        if not ver:
            _logger.warning("⚠ 写入版本号为空,跳过version.json更新")
            return
        # 记录版本更新日志
        _logger.info(f"🔖 写入新本地版本号:{self.local_version} -> {ver}")
        # 读取当前版本配置
        version_cfg = _read_module_json("version")
        # 更新版本号
        version_cfg["APP_VERSION"] = ver
        # 写回版本配置文件
        _write_module_json("version", version_cfg)
        # 更新内存中的本地版本号
        self.local_version = ver
        # 重新加载全量配置,确保内存与磁盘一致
        self.reload_config_from_disk()

    def download_dir(self) -> str:
        """
        获取更新包保存目录(与主程序"路径配置-下载目录"一致).

        参数:
            无

        返回值:
            str: 下载目录的绝对路径

        异常:
            无
        """
        # 调用配置管理模块的函数获取下载目录
        return get_update_download_dir(self._config.get("version", {}))

    def target_package_name(self, tag: str) -> str:
        """
        按当前平台与文件名模板渲染要下载的更新包资产名.

        参数:
            tag (str): 版本标签(用于 {tag} 占位符)

        返回值:
            str: 渲染后的更新包文件名

        异常:
            无
        """
        # 安装版(存在 installed.flag)匹配 setup.exe 安装包,绿色版匹配 zip 压缩包
        installed = is_installed_edition(self.app_dir)
        # 调用配置管理模块的函数渲染文件名
        return render_update_save_filename(self._config.get("version", {}), tag, installed=installed)

    # ------------------------------------------------------------------
    # 更新服务设置表单(聚合主程序的 github/version/user_info/proxy 四份 JSON)
    # ------------------------------------------------------------------
    def get_update_settings_form(self) -> Dict[str, Any]:
        """
        读取更新相关全部配置,聚合成表单数据.

        详细说明:
            聚合四份配置文件的数据:
            - GitHub 配置(仓库所有者、仓库名、API 地址、超时、令牌)
            - 版本配置(更新包文件名模板、启动自动检查)
            - 用户信息配置(下载目录)
            - 代理配置(自动检测系统代理开关)

        参数:
            无

        返回值:
            Dict[str, Any]: 表单数据字典,包含以下字段:
                - github_owner: 仓库所有者
                - github_name: 仓库项目名
                - github_api_url: GitHub API 地址
                - github_timeout: 请求超时(秒)
                - update_pkg: 更新软件包文件名模板
                - github_token: 访问令牌
                - download_dir: 下载目录(绝对路径)
                - auto_check_update: 启动时自动检查更新
                - auto_detect_proxy: 自动检测系统代理

        异常:
            无
        """
        # 获取 GitHub 配置
        github = self._config.get("github", {})
        # 获取版本配置
        version = self._config.get("version", {})
        # 获取代理配置
        proxy = self._config.get("proxy", {})
        # 返回聚合后的表单数据
        return {
            # 仓库所有者
            "github_owner": str(github.get("GITHUB_REPO_OWNER", "")),
            # 仓库项目名
            "github_name": str(github.get("GITHUB_REPO_NAME", "")),
            # GitHub API 地址
            "github_api_url": str(github.get("GITHUB_API_BASE_URL", "https://api.github.com")),
            # 请求超时时间(秒,转字符串用于显示)
            "github_timeout": str(github.get("GITHUB_TIMEOUT", 15)),
            # 更新软件包文件名模板
            "update_pkg": str(version.get("UPDATE_SAVE_FILENAME_TPL", "")),
            # GitHub 访问令牌
            "github_token": str(github.get("GITHUB_TOKEN", "") or ""),
            # 下载目录(给绝对路径,与主程序"路径配置-更新目录"运行时口径一致)
            "download_dir": self.download_dir(),
            # 启动时自动检查更新开关
            "auto_check_update": bool(version.get("AUTO_CHECK_UPDATE_ON_START", True)),
            # 自动检测系统代理开关
            "auto_detect_proxy": bool(proxy.get("AUTO_DETECT_SYSTEM_PROXY", True)),
        }

    def save_update_settings_form(self, form: Dict[str, Any]) -> Tuple[bool, str]:
        """
        校验并保存更新设置,落盘到 github/version/user_info/proxy 后重载配置.

        详细说明:
            先对表单数据进行校验,校验通过后分别写入四份配置文件:
            1. github.json: 仓库所有者、仓库名、令牌、API 地址、超时
            2. version.json: 更新包模板、启动自检开关
            3. proxy.json: 自动检测系统代理开关
            4. user_info.json: 下载目录

        参数:
            form (Dict[str, Any]): 表单数据字典

        返回值:
            Tuple[bool, str]: 二元组,包含:
                - ok: 是否保存成功
                - msg: 结果描述信息

        异常:
            不向外抛出异常;配置保存失败时返回 (False, 错误信息)
        """
        # 提取并清理各字段
        owner = str(form.get("github_owner", "")).strip()          # 仓库所有者
        name = str(form.get("github_name", "")).strip()            # 仓库项目名
        api_url = str(form.get("github_api_url", "")).strip().rstrip("/")  # API 地址(去掉末尾斜杠)
        tpl = str(form.get("update_pkg", "")).strip()              # 更新包文件名模板
        token = str(form.get("github_token", "")).strip()          # 访问令牌
        dl_dir = str(form.get("download_dir", "")).strip()         # 下载目录

        # ---------- 校验开始 ----------
        # 校验: 仓库所有者不能为空
        if not owner:
            return False, "仓库所有者不能为空"
        # 校验: 仓库项目名不能为空
        if not name:
            return False, "仓库项目名不能为空"
        # 校验: 更新包文件名模板不能为空
        if not tpl:
            return False, "更新软件包文件名模板不能为空"
        # 校验: 文件名模板必须包含 {platform} 占位符
        if "{platform}" not in tpl:
            return False, "文件名模板必须包含 {platform} 占位符"
        # 校验: API 地址不能为空
        if not api_url:
            return False, "API地址不能为空"
        # 校验: API 地址必须以 http:// 或 https:// 开头
        if not (api_url.lower().startswith("http://")
                or api_url.lower().startswith("https://")):
            return False, "API地址必须以 http:// 或 https:// 开头"
        # 校验: 请求超时必须是 1~600 之间的整数
        try:
            timeout_val = int(str(form.get("github_timeout", "")).strip())
            if timeout_val < 1 or timeout_val > 600:
                raise ValueError
        except (TypeError, ValueError):
            return False, "请求超时必须是1~600之间的数字(秒)"
        # 校验: 下载目录不能为空
        if not dl_dir:
            return False, "更新下载位置不能为空"

        # 下载目录: 相对路径锚定软件目录转绝对;绝对路径原样规范化
        dl_dir_abs = dl_dir if os.path.isabs(dl_dir) else os.path.abspath(
            os.path.join(self.app_dir, dl_dir)
        )
        # 校验: 下载目录必须能创建
        try:
            os.makedirs(dl_dir_abs, exist_ok=True)
        except OSError as e:
            return False, f"下载目录无法创建:{e}"

        # ---------- 校验通过,保存配置 ----------
        try:
            # 1. 保存 github.json: 仓库所有者、仓库名、令牌、API 地址、超时
            save_github_config({
                "GITHUB_REPO_OWNER": owner,
                "GITHUB_REPO_NAME": name,
                "GITHUB_TOKEN": token,
                "GITHUB_API_BASE_URL": api_url,
                "GITHUB_TIMEOUT": timeout_val,
            })
            # 2. 保存 version.json: 更新包模板 + 启动自检开关
            version_cfg = _read_module_json("version")
            version_cfg["UPDATE_SAVE_FILENAME_TPL"] = tpl
            version_cfg["AUTO_CHECK_UPDATE_ON_START"] = bool(form.get("auto_check_update", True))
            _write_module_json("version", version_cfg)
            # 3. 保存 proxy.json: 仅切换"自动检测系统代理"开关,手动代理项保留原值
            save_proxy_config({
                "AUTO_DETECT_SYSTEM_PROXY": bool(form.get("auto_detect_proxy", True)),
            })
            # 4. 保存 user_info.json: 下载目录(存绝对路径,主程序 _to_abs_path 对绝对路径原样返回)
            #    save_user_info 内部会立即刷新运行时路径并建目录
            save_user_info({"download_dir": dl_dir_abs})
        except OSError as e:
            # 配置保存失败,记录错误日志
            _logger.error(f"❌ 更新设置保存失败:{e}", exc_info=True)
            # 返回失败结果
            return False, f"配置保存失败:{e}"

        # 保存成功后重新从磁盘加载配置,确保内存与磁盘一致
        self.reload_config_from_disk()
        # 记录保存成功日志
        _logger.info(
            f"💾 更新助手内更新设置已保存:{owner}/{name} | API={api_url} | 超时={timeout_val}s | "
            f"模板={tpl} | 下载目录={dl_dir_abs} | 启动自检={'开' if form.get('auto_check_update') else '关'} | "
            f"系统代理={'自动' if form.get('auto_detect_proxy') else '不自动'}"
        )
        # 返回成功结果
        return True, "更新设置已保存"

    # ------------------------------------------------------------------
    # 任务收尾
    # ------------------------------------------------------------------
    def remove_task_file(self) -> None:
        """
        安装流程结束(或取消/失败退出)时删除任务 JSON 文件.

        详细说明:
            删除任务文件避免下次启动被误判为任务模式.
            独立模式下 task_file_path 为空,不执行任何操作.

        参数:
            无

        返回值:
            None

        异常:
            不向外抛出异常;删除失败时记录警告日志
        """
        # 任务文件路径为空则直接返回(独立模式)
        if not self.task_file_path:
            return
        try:
            # 检查文件是否存在,存在则删除
            if os.path.isfile(self.task_file_path):
                os.remove(self.task_file_path)
        except OSError as e:
            # 删除失败,记录警告日志
            _logger.warning(f"⚠ 任务文件删除失败:{self.task_file_path} | {e}")
