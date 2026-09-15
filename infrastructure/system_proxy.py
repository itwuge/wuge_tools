# -*- coding: utf-8 -*-
"""跨平台系统代理检测。

统一返回 requests 可直接使用的 ``{"http": url, "https": url}``。支持
Windows Internet Settings、macOS 系统代理、Linux 环境变量/GNOME 设置,
并兼容 HTTP、HTTPS、SOCKS4、SOCKS5;PAC 只报告检测来源,不执行脚本。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

_logger = logging.getLogger("SystemProxy")


def _with_scheme(proxy_url: str, default_scheme: str = "http") -> str:
    """为裸host:port补协议;SOCKS5统一用socks5h让DNS也经过代理。"""
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    scheme, rest = value.split("://", 1)
    scheme = scheme.lower()
    if scheme in ("socks", "socks5"):
        scheme = "socks5h"
    return f"{scheme}://{rest}"


def _proxy_pair(value: str, scheme: str = "http") -> Optional[Dict[str, str]]:
    """把一个代理地址映射为HTTP和HTTPS共用的requests代理字典。"""
    url = _with_scheme(value, scheme)
    return {"http": url, "https": url} if url else None


def _environment_proxies() -> Optional[Dict[str, str]]:
    """读取HTTP_PROXY/HTTPS_PROXY/ALL_PROXY,HTTP(S)优先于ALL_PROXY。"""
    raw = urllib.request.getproxies_environment()
    http_value = raw.get("http")
    https_value = raw.get("https")
    all_value = raw.get("all") or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    if http_value or https_value:
        http_url = _with_scheme(http_value or https_value or "")
        https_url = _with_scheme(https_value or http_value or "")
        return {"http": http_url, "https": https_url}
    if all_value:
        value = str(all_value)
        scheme = "socks5h" if value.lower().startswith(("socks://", "socks5://")) else "http"
        return _proxy_pair(value, scheme)
    return None


def split_proxy_host_port(proxy_url: str) -> Tuple[str, str]:
    """从代理URL拆出主机和端口,供现有界面回填。"""
    parts = urlsplit(_with_scheme(proxy_url))
    return parts.hostname or "", str(parts.port) if parts.port else ""


def _parse_windows_proxy_server(server: str) -> Optional[Dict[str, str]]:
    """解析Windows ProxyServer注册表值,兼容分协议和SOCKS代理。"""
    value = str(server or "").strip()
    if not value:
        return None
    if ";" not in value and "=" not in value:
        return _proxy_pair(value)
    parsed: Dict[str, str] = {}
    fallback = ""
    socks_value = ""
    socks_scheme = "socks5h"
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            fallback = item
            continue
        scheme, address = item.split("=", 1)
        scheme, address = scheme.strip().lower(), address.strip()
        if not address:
            continue
        if scheme in ("http", "https"):
            parsed[scheme] = _with_scheme(address, "http")
        elif scheme in ("socks", "socks5"):
            socks_value, socks_scheme = address, "socks5h"
        elif scheme == "socks4":
            socks_value, socks_scheme = address, "socks4"
        elif scheme in ("", "*"):
            fallback = address
    if parsed:
        parsed.setdefault("http", parsed.get("https", ""))
        parsed.setdefault("https", parsed.get("http", ""))
        return parsed
    if socks_value:
        return _proxy_pair(socks_value, socks_scheme)
    return _proxy_pair(fallback) if fallback else None


def _detect_windows() -> Optional[Dict[str, str]]:
    """读取Windows Internet Settings;无静态代理时回退环境变量。"""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except FileNotFoundError:
            enabled = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except FileNotFoundError:
            server = ""
        try:
            pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
        except FileNotFoundError:
            pac_url = ""
    if enabled and str(server).strip():
        parsed = _parse_windows_proxy_server(str(server))
        if parsed:
            return parsed
    env = _environment_proxies()
    if env:
        return env
    if pac_url:
        _logger.info("ℹ️ 检测到Windows PAC自动代理脚本,但当前程序不执行PAC脚本:%s", pac_url)
    return None


def _detect_macos() -> Optional[Dict[str, str]]:
    """读取macOS系统/环境代理,支持urllib返回的SOCKS代理。"""
    raw = urllib.request.getproxies()
    http_value = raw.get("http")
    https_value = raw.get("https")
    if http_value or https_value:
        return {
            "http": _with_scheme(http_value or https_value or ""),
            "https": _with_scheme(https_value or http_value or ""),
        }
    socks_value = raw.get("socks") or raw.get("all")
    if socks_value:
        return _proxy_pair(socks_value, "socks5h")
    return _environment_proxies()


def _gsettings_get(schema: str, key: str) -> Optional[str]:
    """读取一条GNOME gsettings值,失败返回None。"""
    try:
        completed = subprocess.run(
            ["gsettings", "get", schema, key], capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if not value or value in ("''", '""', "false", "true"):
        return None
    return value.strip("'\"")


def _detect_linux() -> Optional[Dict[str, str]]:
    """检测Linux环境变量及GNOME HTTP/HTTPS/SOCKS代理。"""
    env = _environment_proxies()
    if env:
        return env
    mode = _gsettings_get("org.gnome.system.proxy", "mode")
    if mode != "manual":
        if mode == "auto":
            pac = _gsettings_get("org.gnome.system.proxy", "autoconfig-url")
            _logger.info("ℹ️ 检测到GNOME PAC自动代理脚本,但当前程序不执行PAC脚本:%s", pac or "(未提供URL)")
        return None
    out: Dict[str, str] = {}
    http_host = _gsettings_get("org.gnome.system.proxy.http", "host")
    http_port = _gsettings_get("org.gnome.system.proxy.http", "port")
    https_host = _gsettings_get("org.gnome.system.proxy.https", "host")
    https_port = _gsettings_get("org.gnome.system.proxy.https", "port")
    if http_host and http_port:
        out["http"] = f"http://{http_host}:{http_port}"
    if https_host and https_port:
        out["https"] = f"http://{https_host}:{https_port}"
    if out:
        out.setdefault("http", out.get("https", ""))
        out.setdefault("https", out.get("http", ""))
        return out
    socks_host = _gsettings_get("org.gnome.system.proxy.socks", "host")
    socks_port = _gsettings_get("org.gnome.system.proxy.socks", "port")
    return _proxy_pair(f"{socks_host}:{socks_port}", "socks5h") if socks_host and socks_port else None


_PLATFORM_SOURCES = {
    "win32": ("Windows系统代理(Internet设置/环境变量)", _detect_windows),
    "darwin": ("macOS系统代理(网络设置/环境变量)", _detect_macos),
    "linux": ("Linux代理(环境变量/GNOME设置)", _detect_linux),
}


def detect_system_proxy() -> Tuple[Optional[Dict[str, str]], str]:
    """检测当前系统HTTP/HTTPS/SOCKS代理;异常时安全回退直连。"""
    if sys.platform.startswith("win"):
        source, detector = _PLATFORM_SOURCES["win32"]
    elif sys.platform == "darwin":
        source, detector = _PLATFORM_SOURCES["darwin"]
    else:
        source, detector = _PLATFORM_SOURCES["linux"]
    try:
        proxies = detector()
    except Exception as exc:
        _logger.warning("❌ 系统代理检测异常(%s):%s", source, exc, exc_info=True)
        return None, f"{source}-检测异常"
    if proxies:
        _logger.debug("🌐 检测到系统代理(%s):http=%s, https=%s", source, proxies.get("http"), proxies.get("https"))
    return proxies, source
