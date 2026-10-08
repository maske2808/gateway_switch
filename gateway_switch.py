#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gateway_switch.py - IPv4 默认网关切换工具（Marvis 风格 UI，完全复刻风冷.exe 界面框架）

功能：
  - 实时解析当前 IPv4 默认路由（0.0.0.0/0）网关与网卡信息（route print + 网卡 IPv4/掩码）
  - 一键在网关 A（192.168.2.1）与网关 B（192.168.2.2）之间切换默认路由
  - 切换方式：netsh interface ip delete/add route 0.0.0.0/0（增删默认路由）
  - 切换瞬间网络会短暂闪断，界面明确提示
  - UI 仿 Marvis：左侧导航（程序 + 底部设置）+ 右侧内容框，浅色主题，
    自绘无边框标题栏 + 圆角窗口 + 右侧可收起日志栏 + 系统托盘

运行：以管理员身份运行 python gateway_switch.py（exe 内置 UAC 提权清单）
依赖：仅 Python 标准库 + ctypes；托盘需 pystray / pillow（可选）
"""

import configparser
import ctypes
import ctypes.wintypes as wt
import os
import queue
import re
import sys
import threading
import time
import shutil
import subprocess
import tkinter as tk
import base64
import winreg
import socket
import struct
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 系统托盘（可选依赖：pip install pystray pillow）
try:
    import pystray
    from PIL import Image
    try:
        from PIL import ImageTk
    except Exception:
        ImageTk = None
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False
    ImageTk = None
    Image = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SINGLE_INSTANCE_MUTEX = r'Global\GatewaySwitch_SingleInstance'
WINDOW_TITLE = '网关切换 - IPv4 默认路由管理'

# 默认网关配置
DEFAULT_GW_A = '192.168.2.1'
DEFAULT_GW_B = '192.168.2.2'

# 开机自启计划任务
AUTOSTART_TASK_NAME = 'GatewaySwitchAutostart'
# 静默启动参数：带该参数启动时主窗口不显示、仅托盘静默运行（兼容旧 --autostart）
SILENT_FLAGS = ('--silent', '--hidden', '--autostart')
IS_SILENT = any(a.lower() in SILENT_FLAGS for a in sys.argv[1:])

# 程序页站点网络卡片（横向 4 卡：百度搜索 / GitHub / YouTube / 谷歌）
SITES = [('百度搜索', 'www.baidu.com'),
         ('GitHub', 'github.com'),
         ('YouTube', 'www.youtube.com'),
         ('谷歌', 'www.google.com')]
PROBE_INTERVAL = 5  # 站点延迟/网速采样间隔（秒）
PROBE_DURATION = 60  # 每次触发后采样持续时间（秒），结束后停止等待下次触发

def probe_site(host, timeout=3.0, max_bytes=1048576):
    """探测站点网络质量：TCP 443 连接延迟(ms) + HTTPS 下载网速(Mbps)。

    返回 (delay_ms, speed_mbps, ok)：
      - delay_ms: 建连耗时毫秒，失败为 None
      - speed_mbps: 下载测速 Mbps，测速失败为 None
      - ok: TCP 是否连通
    """
    delay = None
    ok = False
    try:
        t0 = time.time()
        with socket.create_connection((host, 443), timeout=timeout):
            delay = (time.time() - t0) * 1000.0
        ok = True
    except Exception:
        return None, None, False
    speed = None
    try:
        req = urllib.request.Request('https://%s/' % host,
                                     headers={'User-Agent': 'Mozilla/5.0'})
        t1 = time.time()
        total = 0
        with urllib.request.urlopen(req, timeout=timeout + 1) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total >= max_bytes:
                    break
        dur = time.time() - t1
        if dur > 0 and total > 0:
            speed = total * 8.0 / dur / 1e6
    except Exception:
        pass
    return delay, speed, ok

def _round_rect_pts(x0, y0, x1, y1, r):
    """圆角矩形点列（配合 Canvas create_polygon smooth=True 绘制大圆角矩形）。
    r 为圆角半径，自动钳制到短边一半以内。"""
    r = max(1, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


# ---------------------------------------------------------------------------
# 单实例互斥（命名互斥体 Global\\ 前缀 + 激活已有窗口）
# ---------------------------------------------------------------------------
def _acquire_single_instance():
    """创建命名互斥体：成功返回 True（本进程为唯一实例，句柄保留至退出）；
    已存在返回 False（另一实例在运行）。"""
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.restype = wt.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    h = k.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if not h:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        try:
            k.CloseHandle(h)
        except Exception:
            pass
        return False
    return True


def _activate_existing_window():
    """查找已运行实例的主窗口并激活（还原 + 置前 + 前台）。"""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = wt.HWND
        u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        u.EnumWindows.restype = wt.BOOL
        u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.IsWindow.restype = wt.BOOL
        u.IsWindow.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u.FlashWindow.argtypes = [wt.HWND, wt.BOOL]
        u.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]

        def _title_ok(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value != WINDOW_TITLE

        _found = [0]

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _cb(hwnd, lparam):
            if not _title_ok(hwnd):
                _found[0] = hwnd
                return False
            return True

        hwnd = u.FindWindowW(None, WINDOW_TITLE)
        if not (hwnd and u.IsWindow(hwnd)):
            u.EnumWindows(_cb, 0)
            hwnd = _found[0]
        if hwnd and u.IsWindow(hwnd):
            u.ShowWindow(hwnd, 9)
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                           0x0001 | 0x0002 | 0x0040)   # NOMOVE|NOSIZE|SHOWWINDOW
            u.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                           0x0001 | 0x0002)            # NOMOVE|NOSIZE
            u.keybd_event(0x12, 0, 0, 0)     # ALT down
            u.keybd_event(0x12, 0, 2, 0)     # ALT up
            u.SetForegroundWindow(hwnd)
            u.FlashWindow(hwnd, True)
            return True
    except Exception:
        pass
    return False


def make_tray_icon(size=64, variant='gray'):
    """托盘图标：按 variant 加载随包对应颜色的地球图标（填充风格 SVG 渲染）：
    variant='gray'  -> icon_gray.ico  （程序/任务栏/网关 A（192.168.2.1）托盘）
    variant='red'   -> icon_red2.ico  （网关 B（192.168.2.2）谷歌不通托盘）
    variant='green' -> icon_green2.ico（网关 B（192.168.2.2）谷歌通托盘）
    优先加载 ICO 最大帧，其次同名 PNG，最后回退程序内置绘制灰色地球。"""
    if variant not in ('gray', 'red', 'green'):
        variant = 'gray'
    ico_name = {'gray': 'icon_gray.ico', 'red': 'icon_red2.ico',
                'green': 'icon_green2.ico'}[variant]
    png_name = {'gray': 'icon_gray.png', 'red': 'icon_red2.png',
                'green': 'icon_green2.png'}[variant]
    img = None
    for name in (ico_name, png_name):
        try:
            p = resource_path(name)
            if os.path.isfile(p):
                im = Image.open(p)
                # ICO 多帧：取最大帧（避免取到 16px 小帧导致托盘模糊/不一致）
                frames = []
                try:
                    while True:
                        frames.append((im.size[0], im.copy()))
                        im.seek(im.tell() + 1)
                except EOFError:
                    pass
                if frames:
                    frames.sort(key=lambda t: t[0], reverse=True)
                    im = frames[0][1]
                img = im.convert('RGBA')
                break
        except Exception:
            continue
    if img is None:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        s = size / 48.0
        fill = {'gray': (155, 155, 155, 255), 'red': (208, 2, 27, 255),
                'green': (126, 211, 33, 255)}[variant]
        d.ellipse([8 * s, 8 * s, 40 * s, 40 * s], fill=fill,
                  outline=(255, 255, 255, 255), width=int(3 * s))
    img = img.resize((size, size), Image.LANCZOS)
    return img


# ---------------------------------------------------------------------------
# 开机自启 + 配置持久化（config.ini 与程序同目录）
# ---------------------------------------------------------------------------
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE_NAME = 'GatewaySwitch'


def app_dir():
    """程序所在目录：exe 打包后为 exe 所在目录，py 运行时为脚本目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), 'config.ini')


def load_config():
    """读取 config.ini，返回 {'auto_start','auto_run','refresh_interval','gw_a','gw_b'}。
    auto_run / refresh_interval 为旧版监控配置，仅做兼容读取，新版已不再使用。"""
    cfg = {'auto_start': False, 'auto_run': False, 'refresh_interval': 1.0,
           'gw_a': DEFAULT_GW_A, 'gw_b': DEFAULT_GW_B}
    try:
        cp = configparser.ConfigParser()
        cp.read(config_path(), encoding='utf-8')
        if cp.has_section('settings'):
            cfg['auto_start'] = cp.getboolean('settings', 'auto_start', fallback=False)
            cfg['auto_run'] = cp.getboolean('settings', 'auto_run', fallback=False)
            try:
                cfg['refresh_interval'] = max(0.5, min(10.0, cp.getfloat('settings', 'refresh_interval', fallback=1.0)))
            except Exception:
                cfg['refresh_interval'] = 1.0
            cfg['gw_a'] = cp.get('settings', 'gw_a', fallback=DEFAULT_GW_A).strip() or DEFAULT_GW_A
            cfg['gw_b'] = cp.get('settings', 'gw_b', fallback=DEFAULT_GW_B).strip() or DEFAULT_GW_B
    except Exception:
        pass
    return cfg


def save_config(auto_start, gw_a=None, gw_b=None):
    """持久化设置到 config.ini（程序同目录）。None 参数保留现有值。返回是否成功。"""
    try:
        cp = configparser.ConfigParser()
        cp.read(config_path(), encoding='utf-8')
        if not cp.has_section('settings'):
            cp.add_section('settings')
        cp.set('settings', 'auto_start', '1' if auto_start else '0')
        cp.set('settings', 'auto_run', '0')
        if gw_a is not None:
            cp.set('settings', 'gw_a', gw_a)
        if gw_b is not None:
            cp.set('settings', 'gw_b', gw_b)
        with open(config_path(), 'w', encoding='utf-8') as f:
            cp.write(f)
        return True
    except Exception:
        return False


def get_startup_command():
    """注册表 Run 项值：exe 指向自身；py 运行时指向 python+脚本（均带引号）。"""
    if getattr(sys, 'frozen', False):
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))


def set_autostart(enabled):
    """写入/删除 HKCU Run 项（当前用户，无需管理员权限）。返回 (ok, msg)。"""
    key = None
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                              get_startup_command())
            winreg.CloseKey(key)
            return True, '已写入 Run 项 %s' % RUN_VALUE_NAME
        try:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True, '已删除 Run 项 %s' % RUN_VALUE_NAME
    except Exception as e:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
        return False, '注册表操作失败：%s' % e


def autostart_exe_path():
    """开机自启目标路径：exe 指向自身；py 运行时指向 python+脚本。"""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return '%s "%s"' % (sys.executable, os.path.abspath(__file__))


def is_autostart_task_set():
    """查询计划任务 GatewaySwitchAutostart 是否已存在。"""
    try:
        r = subprocess.run('schtasks /query /tn "%s"' % AUTOSTART_TASK_NAME,
                           shell=True, capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def set_autostart_task(enabled):
    """创建/删除开机自启计划任务（最高权限静默启动）。返回 (ok, msg)。"""
    if enabled:
        cmd = ('schtasks /create /tn "%s" /tr "\\"%s\\" --silent" '
               '/sc onlogon /rl highest /f' % (AUTOSTART_TASK_NAME, autostart_exe_path()))
        ok_msg = '已创建计划任务 %s（登录时最高权限静默启动）' % AUTOSTART_TASK_NAME
        fail_msg = '计划任务创建失败'
    else:
        cmd = 'schtasks /delete /tn "%s" /f' % AUTOSTART_TASK_NAME
        ok_msg = '已删除计划任务 %s' % AUTOSTART_TASK_NAME
        fail_msg = '计划任务删除失败'
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            if enabled:
                set_autostart(False)
            return True, ok_msg
        return False, '%s：%s' % (fail_msg, (r.stderr or r.stdout or '').strip())
    except Exception as e:
        return False, '%s：%s' % (fail_msg, e)


def resource_path(name):
    """打包后从 PyInstaller 解包目录 sys._MEIPASS 取资源；源码运行时取脚本同目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# ---------------------------------------------------------------------------
# 网络工具：默认路由检测 / netsh 增删默认路由
# ---------------------------------------------------------------------------
def _run_cmd(cmd, timeout=30):
    """执行系统命令，返回 CompletedProcess（自动识别 UTF-8 / GBK 编码兼容中文输出）。"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    try:
        out = r.stdout.decode('utf-8')
    except UnicodeDecodeError:
        out = r.stdout.decode('gbk', errors='replace')
    try:
        err = r.stderr.decode('utf-8')
    except UnicodeDecodeError:
        err = r.stderr.decode('gbk', errors='replace')
    r.stdout = out
    r.stderr = err
    return r


def interface_name_by_index(idx):
    """netsh interface ip show interfaces：索引 -> 接口名。"""
    r = _run_cmd('netsh interface ip show interfaces', timeout=15)
    if not r:
        return None
    pat = re.compile(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$')
    for line in r.stdout.splitlines():
        m = pat.match(line)
        if m and m.group(1) == str(idx):
            return m.group(5).strip()
    return None


def list_default_routes():
    """netsh interface ip show route：返回 [(接口名, 网关IP|None)] 列表（0.0.0.0/0）。"""
    r = _run_cmd('netsh interface ip show route', timeout=15)
    if not r:
        return []
    routes = []
    pat = re.compile(r'^\S+\s+\S+\s+\d+\s+0\.0\.0\.0/0\s+(\d+)\s+(\S+)\s*$')
    ip_pat = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    for line in r.stdout.splitlines():
        m = pat.match(line.strip())
        if m:
            idx = m.group(1)
            last = m.group(2)
            ifname = interface_name_by_index(idx)
            gw = last if ip_pat.match(last) else None
            routes.append((ifname, gw))
    return routes


def get_gateway_via_route_print():
    """route print -4 实时解析 0.0.0.0/0 默认路由，返回真实网关 IP；未检测到返回 None。"""
    r = _run_cmd('route print -4', timeout=15)
    if not r:
        return None
    ip_pat = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    for line in r.stdout.splitlines():
        s = line.strip()
        m = re.match(r'^0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+(\S+)\s+(\d+)\s*$', s)
        if not m:
            continue
        gw = m.group(1)
        if gw.lower() in ('在链接上', 'on-link') or not ip_pat.match(gw):
            continue
        return gw
    return None


def detect_default_route():
    """检测当前 IPv4 默认路由：返回 (接口名, 网关IP 或 None)；未检测到返回 (None, None)。
    网关 IP 优先通过 route print -4 实时解析 0.0.0.0/0 默认路由得到，接口名取自 netsh（切换用）。"""
    gw = get_gateway_via_route_print()
    routes = list_default_routes()
    ifname = routes[0][0] if routes else None
    if gw is None and routes:
        gw = routes[0][1]
    if ifname is None and gw is None:
        return None, None
    return ifname, gw


def prefixlen_to_mask(prefixlen):
    """IPv4 前缀长度（如 24）转点分掩码（如 255.255.255.0）；无法换算返回 '--'。"""
    try:
        n = int(prefixlen)
        if not 0 <= n <= 32:
            return '--'
        m = (0xffffffff << (32 - n)) & 0xffffffff if n else 0
        return '%d.%d.%d.%d' % ((m >> 24) & 255, (m >> 16) & 255, (m >> 8) & 255, m & 255)
    except Exception:
        return '--'


def get_active_ipv4():
    """获取默认路由所在活动网卡的 (接口名, IPv4 地址, 子网掩码)；
    优先 PowerShell Get-NetRoute + Get-NetIPAddress（EncodedCommand 避免引号转义），
    失败回退 ipconfig 解析。"""
    ps_script = (
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 | "
        "Select-Object -First 1; "
        "if ($r) { "
        "$ip = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $r.InterfaceIndex | "
        "Where-Object { $_.IPAddress -notlike '127.*' } | Select-Object -First 1; "
        "Write-Output ($r.InterfaceAlias + '|' + $ip.IPAddress + '|' + $ip.PrefixLength) }"
    )
    enc = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
    r = _run_cmd('powershell -NoProfile -EncodedCommand %s' % enc, timeout=20)
    ip_pat = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    if r and r.returncode == 0 and '|' in (r.stdout or ''):
        parts = (r.stdout.strip().split('|') + ['', '', ''])[:3]
        name, ip, plen = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if ip_pat.match(ip):
            return name, ip, prefixlen_to_mask(plen)
    # 回退：ipconfig 解析第一个非 127 的 IPv4 地址
    r2 = _run_cmd('ipconfig', timeout=20)
    if r2 and r2.stdout:
        sec = None
        mask = '--'
        for line in r2.stdout.splitlines():
            s = line.strip()
            if s and (s.endswith('适配器') or 'adapter' in s.lower()):
                sec = s
            m = re.search(r'子网掩码\s*[.:]\s*(\d{1,3}(\.\d{1,3}){3})', s)
            if m:
                mask = m.group(1)
            m = re.search(r'Subnet Mask\s*[.:]\s*(\d{1,3}(\.\d{1,3}){3})', s)
            if m:
                mask = m.group(1)
            m = re.search(r'IPv4[^\d]*?(\d{1,3}(\.\d{1,3}){3})', s)
            if m and sec:
                ip = m.group(1)
                if not ip.startswith('127.'):
                    return sec, ip, mask
    return None, None, '--'


def do_switch_gateway(ifname, target_gw):
    """用 netsh 增删默认路由切换网关：先删旧 0.0.0.0/0，再加新网关。
    返回 (ok, msg)。切换瞬间网络会短暂闪断（属正常现象）。"""
    if not ifname:
        return False, '未检测到默认路由接口，无法切换'
    r1 = _run_cmd('netsh interface ip delete route 0.0.0.0/0 "%s"' % ifname, timeout=30)
    r2 = _run_cmd('netsh interface ip add route 0.0.0.0/0 "%s" %s' % (ifname, target_gw), timeout=30)
    if r2 is not None and r2.returncode == 0:
        return True, '切换成功：默认路由 0.0.0.0/0 -> %s（接口 %s，网络已短暂闪断）' % (target_gw, ifname)
    err = ''
    if r2 is not None:
        err = (r2.stderr or r2.stdout or '').strip()
    elif r1 is not None:
        err = (r1.stderr or r1.stdout or '').strip()
    return False, '切换失败：%s' % (err or '未知错误')


# ---------------------------------------------------------------------------
# 网络配置文件类别：读取 / 切换（专用<->公用）
# ---------------------------------------------------------------------------
_NETWORK_LIST_PROFILES = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles'
_NETWORK_LIST_SIGS = (r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Signatures\Unmanaged',
                      r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Signatures\Managed')


def _read_profile_category(guid):
    """按配置文件 GUID 读取注册表 Profiles\{GUID} 的 (ProfileName, 类别)。
    Category 值：0=公用(Public)，1=专用(Private)，其他/未知返回 '未知'。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             _NETWORK_LIST_PROFILES + '\\' + guid)
        try:
            cat_val, _ = winreg.QueryValueEx(key, 'Category')
            try:
                name_val, _ = winreg.QueryValueEx(key, 'ProfileName')
            except OSError:
                name_val = None
            cat = {0: '公用', 1: '专用'}.get(cat_val, '未知')
            return name_val, cat
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None, None


def _active_profile_guids():
    """从 NetworkList\Signatures（Unmanaged/Managed）收集当前活动网络引用的
    ProfileGuid 列表（连接过的历史网络不在此列，代表当前生效接口的签名）。"""
    guids = []
    for base in _NETWORK_LIST_SIGS:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(key, sub)
                    try:
                        val, _ = winreg.QueryValueEx(sk, 'ProfileGuid')
                        if val and val not in guids:
                            guids.append(val)
                    except OSError:
                        pass
                    finally:
                        winreg.CloseKey(sk)
                except OSError:
                    pass
        finally:
            winreg.CloseKey(key)
    return guids


def _filetime_to_ts(data):
    """FILETIME(16字节小端) -> Unix 秒时间戳；解析失败返回 0。"""
    try:
        if data and len(data) >= 8:
            low, high = struct.unpack('<II', data[:8])
            ft = (high << 32) | low
            return (ft - 116444736000000000) / 10000000.0
    except Exception:
        pass
    return 0


def _profile_guid_by_name(name):
    """按注册表 Profiles 键的 ProfileName 匹配当前活动网络的 ProfileGuid。
    说明：实测 Signatures 子键不含 InterfaceGuid 值（无 接口GUID->配置文件GUID 直接映射），
    故以 PowerShell 返回的活动配置名 Name 关联 Profiles\{GUID}\ProfileName；
    重名（如 '网络'/'网络 2'）时取 DateLastConnected 最新者消歧。
    返回 ProfileGuid 或 None。"""
    if not name:
        return None
    candidates = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _NETWORK_LIST_PROFILES)
    except OSError:
        return None
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            try:
                sk = winreg.OpenKey(key, sub)
                try:
                    pn, _ = winreg.QueryValueEx(sk, 'ProfileName')
                    if pn == name:
                        try:
                            dlc, _ = winreg.QueryValueEx(sk, 'DateLastConnected')
                        except OSError:
                            dlc = None
                        candidates.append((sub, dlc))
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(sk)
            except OSError:
                pass
    finally:
        winreg.CloseKey(key)
    if not candidates:
        return None
    candidates.sort(key=lambda t: _filetime_to_ts(t[1]) if t[1] else 0, reverse=True)
    return candidates[0][0]


def get_network_profile():
    """读取当前活动网络配置文件，返回 (配置名, GUID, 类别)；不可知为 None / '未知'。
    类别：'专用' / '公用' / '未知'。
    1) 首选 PowerShell Get-NetConnectionProfile 取活动配置（默认路由/已连接优先，
       含接口 GUID 与配置名），再以 ProfileName 关联注册表 Profiles\{ProfileGuid}\Category；
    2) PowerShell 不可用时回退注册表：Signatures 定位活动 ProfileGuid -> Profiles 读 Category。"""
    name = guid = category = None
    try:
        ps = ("$def = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
              "| Sort-Object RouteMetric | Select-Object -First 1; "
              "$profiles = @(Get-NetConnectionProfile); "
              "$p = $null; "
              "if ($def -and $profiles) { $p = $profiles | Where-Object { $_.InterfaceIndex -eq $def.InterfaceIndex } | Select-Object -First 1 }; "
              "if (-not $p) { $p = $profiles | Where-Object { $_.IPv4Connectivity -in @('Internet','LocalNetwork') } | Select-Object -First 1 }; "
              "if (-not $p) { $p = $profiles | Select-Object -First 1 }; "
              "if ($p) { $g = \"$($p.InstanceID)\"; "
              "Write-Output ($p.Name + '|' + $p.NetworkCategory + '|' + $g + '|' + $p.InterfaceIndex) } "
              "else { Write-Output 'NOPROFILE' }")
        enc = base64.b64encode(ps.encode('utf-16-le')).decode('ascii')
        r = _run_cmd('powershell -NoProfile -EncodedCommand %s' % enc, timeout=20)
        if r and r.returncode == 0:
            out = (r.stdout or '').strip()
            if out and out != 'NOPROFILE' and '|' in out:
                parts = out.split('|')
                name = parts[0].strip() or None
                cat_raw = (parts[1].strip() if len(parts) > 1 else '').lower()
                ifguid = (parts[2].strip() if len(parts) > 2 else '') or None
                if cat_raw in ('private', '1'):
                    category = '专用'
                elif cat_raw in ('public', '0'):
                    category = '公用'
                else:
                    category = '未知'
                # 以 PowerShell 活动配置名关联注册表 ProfileName -> ProfileGuid
                if name:
                    guid = _profile_guid_by_name(name)
    except Exception:
        pass
    # 注册表 Profiles\{ProfileGuid}\Category 为权威（切换后立即更新）
    if guid:
        rname, rcat = _read_profile_category(guid)
        if rcat is not None:
            category = rcat
            if name is None:
                name = rname
    # PowerShell 不可用 / 未映射到 ProfileGuid 时回退注册表定位
    if category is None or guid is None:
        try:
            for g in _active_profile_guids():
                n, c = _read_profile_category(g)
                if c is not None:
                    return (name or n), g, c
        except Exception:
            pass
    return name, guid, category or '未知'


def set_network_category(target):
    """切换当前活动网络配置文件类别为 '专用' 或 '公用'。
    使用 PowerShell Set-NetConnectionProfile（默认路由接口优先，与读取逻辑一致）。
    返回 (ok, msg, profile_guid)：成功时 profile_guid 为切换后配置的 ProfileGuid，
    供界面立即按注册表最新 Category 刷新标签。"""
    if target not in ('专用', '公用'):
        return False, '目标类别无效：%s' % target, None
    target_ps = 'Private' if target == '专用' else 'Public'
    ps = ("$def = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
          "| Sort-Object RouteMetric | Select-Object -First 1; "
          "$profiles = @(Get-NetConnectionProfile); "
          "$p = $null; "
          "if ($def -and $profiles) { $p = $profiles | Where-Object { $_.InterfaceIndex -eq $def.InterfaceIndex } | Select-Object -First 1 }; "
          "if (-not $p) { $p = $profiles | Where-Object { $_.IPv4Connectivity -in @('Internet','LocalNetwork') } | Select-Object -First 1 }; "
          "if (-not $p) { $p = $profiles | Select-Object -First 1 }; "
          "if ($p) { Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory %s; "
          "$g = \"$($p.InstanceID)\"; "
          "Write-Output ('OK|' + $p.Name + '|' + $g) } "
          "else { Write-Output 'NOPROFILE' }" % target_ps)
    enc = base64.b64encode(ps.encode('utf-16-le')).decode('ascii')
    r = _run_cmd('powershell -NoProfile -EncodedCommand %s' % enc, timeout=30)
    if r is None:
        return False, '切换命令执行超时或无响应', None
    out = (r.stdout or '').strip()
    err = (r.stderr or '').strip()
    if out.startswith('OK|'):
        parts = out.split('|')
        pname = parts[1].strip() if len(parts) > 1 else ''
        ifguid = parts[2].strip() if len(parts) > 2 else ''
        # 以切换配置名关联注册表 ProfileName -> ProfileGuid，供界面立即按注册表 Category 刷新
        pguid = _profile_guid_by_name(pname) if pname else None
        if pguid:
            time.sleep(0.2)  # 等待注册表落盘，确保界面立即读到最新类别
            _, rcat = _read_profile_category(pguid)
            if rcat is not None:
                return True, '网络类别已切换为 %s（配置 %s）' % (target, pname), pguid
        return True, '网络类别已切换为 %s（配置 %s）' % (target, pname), pguid
    if 'NOPROFILE' in out:
        return False, '未检测到活动网络配置文件，无法切换', None
    if err:
        return False, '切换失败：%s（切换需管理员权限，若仍失败请检查系统策略/防火墙设置）' % err.strip(), None
    if out:
        return False, '切换失败：%s' % out.strip(), None
    return False, '切换失败：未知错误', None


class MarvisUI:
    BG_TOP = (0xfe, 0xfe, 0xfe)     # 极浅灰白渐变顶部
    BG_BOT = (0xf7, 0xf7, 0xf7)     # 极浅灰白渐变底部
    CARD_BG = '#fbfbfb'
    CARD_BORDER = '#efefef'
    NAV_ACTIVE = '#f0f0f0'
    NAV_INACTIVE = '#fbfbfb'
    NAV_TEXT_ACTIVE = '#333333'
    NAV_TEXT = '#333333'
    TEXT_MAIN = '#333333'
    TEXT_DIM = '#888888'
    GW_COLOR = '#ff6b57'
    ACCENT = '#35d07f'
    LAT_COLOR = '#666666'
    FONT = 'Microsoft YaHei UI'
    # 左侧导航图标（Font Awesome 6 Free 字体：fa-microchip U+F2DB / fa-cog U+F013）
    NAV_ICONS = {'程序': '\uf2db', '设置': '\uf013'}

    def _apply_window_icon(self):
        """统一窗口/任务栏图标（Alt+Tab、任务栏、标题栏）：与 exe 嵌入图标一致。
        用 GetAncestor(GA_ROOT) 定位 Tk 顶层窗口（winfo_id 可能返回内部子窗口），
        再经 WM_SETICON 设置 ICON_BIG/ICON_SMALL。"""
        try:
            import ctypes
            u32 = ctypes.windll.user32
            u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u32.LoadImageW.restype = ctypes.c_void_p
            u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u32.GetAncestor.restype = ctypes.c_void_p
            u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_void_p]
            u32.SendMessageW.restype = ctypes.c_void_p
            icon_path = resource_path('icon_gray.ico')
            hbig = u32.LoadImageW(None, icon_path, 1, 32, 32, 0x10)   # IMAGE_ICON + LR_LOADFROMFILE
            hsmall = u32.LoadImageW(None, icon_path, 1, 16, 16, 0x10)
            hwnd = self.root.winfo_id()
            if hwnd:
                hwnd = u32.GetAncestor(hwnd, 2)  # GA_ROOT=2 取顶层窗口
            if hwnd:
                if hbig:
                    u32.SendMessageW(hwnd, 0x0080, 1, hbig)     # WM_SETICON ICON_BIG
                if hsmall:
                    u32.SendMessageW(hwnd, 0x0080, 0, hsmall)   # WM_SETICON ICON_SMALL
        except Exception:
            pass

    def __init__(self, root):
        self.root = root
        self._apply_window_icon()
        self.q = queue.Queue()

        self._dragging = False
        self._bg_dirty = False
        self._drag_sync_pending = False
        self._nav_anim_running = False
        self._nav_anim_items = {}
        # 最新数值/文本缓存：_draw_cards 全量重建后立即恢复
        self._last_gw = '--'
        self._cur_gw = None
        self._last_ifname = '--'
        self._last_ip = '--'
        self._last_mask = '--'
        # 网络配置文件类别（专用/公用）显示与切换状态
        self._net_cat = '未知'
        self._net_cat_name = None
        self._net_cat_guid = None
        self._net_switch_running = False
        self._bg_cache = {}
        self._bg_photo = None

        self.current_page = '程序'
        self.tray = None
        self._tray_click_ts = 0
        self._tray_click_after = None
        self._tray_icon_red = None
        self._tray_icon_variant = None
        self._switch_running = False
        self._build()
        self._apply_window_icon()
        self._queue_notify()
        self._apply_startup_settings()
        # 站点延迟/网速后台探测线程（切换网关后由 _refresh_view 触发立即刷新）
        self._probe_stop = False
        self._probe_event = threading.Event()
        self._probe_pool = ThreadPoolExecutor(max_workers=len(SITES))
        threading.Thread(target=self._probe_worker, daemon=True).start()
        self._probe_now()  # 启动即触发一轮站点采样

    # ---------------- 基础绘制 ----------------
    def _rounded(self, c, x1, y1, x2, y2, r, fill='', outline='', width=1, tags=None):
        """圆角矩形（polygon + smooth）"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, fill=fill, outline=outline,
                                width=width, tags=tags)

    def _draw_gradient(self, canvas, w, h):
        if self._dragging or self._drag_sync_pending:
            self._bg_dirty = True
            return
        if w < 2 or h < 2:
            return
        if ImageTk is not None:
            key = (w, h)
            photo = self._bg_cache.get(key)
            if photo is None:
                try:
                    top = self.BG_TOP
                    bot = self.BG_BOT
                    n = max(h - 1, 1)
                    grad = Image.new('RGB', (1, h))
                    for y in range(h):
                        t = y / n
                        r = int(top[0] + (bot[0] - top[0]) * t)
                        g = int(top[1] + (bot[1] - top[1]) * t)
                        b = int(top[2] + (bot[2] - top[2]) * t)
                        grad.putpixel((0, y), (r, g, b))
                    img = grad.resize((w, h), Image.NEAREST)
                    photo = ImageTk.PhotoImage(img)
                    if len(self._bg_cache) > 3:
                        self._bg_cache.clear()
                    self._bg_cache[key] = photo
                    self._bg_photo = photo
                except Exception:
                    photo = None
            if photo is not None:
                canvas.delete('bg')
                canvas.create_image(0, 0, anchor='nw', image=photo, tags='bg')
                canvas.tag_lower('bg')
                self._bg_dirty = False
                return
        canvas.delete('bg')
        steps = max(h, 2)
        for i in range(steps):
            t = i / (steps - 1)
            r = int(self.BG_TOP[0] + (self.BG_BOT[0] - self.BG_TOP[0]) * t)
            g = int(self.BG_TOP[1] + (self.BG_BOT[1] - self.BG_TOP[1]) * t)
            b = int(self.BG_TOP[2] + (self.BG_BOT[2] - self.BG_TOP[2]) * t)
            col = '#%02x%02x%02x' % (r, g, b)
            canvas.create_line(0, i, w, i, fill=col, tags='bg')
        self._bg_dirty = False

    # ---------------- 构建 ----------------
    def _build(self):
        self.root.title(WINDOW_TITLE)
        self.root.overrideredirect(False)
        self.root.geometry('1000x500')
        self.root.minsize(940, 480)
        self.root.configure(bg='#f7f7f7')
        self._strip_chrome()
        self._last_rgn_size = (0, 0)
        self.root.bind('<Configure>', self._on_root_configure)

        self.bg = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg.bind('<Configure>', lambda e: self._draw_gradient(self.bg, e.width, e.height))

        self._build_titlebar()

        self.nav = tk.Canvas(self.root, width=196, highlightthickness=0, bd=0,
                             bg='#fbfbfb')
        self.nav.place(x=0, y=0, relheight=1, height=0)
        self._build_nav()

        self.content = tk.Frame(self.root, bg='#f7f7f7')
        self.content.place(x=196, y=0, relwidth=1, relheight=1,
                           width=-196, height=0)

        self.pages = {}
        self.pages['程序'] = self._build_page_program(self.content)
        self.pages['设置'] = self._build_page_settings(self.content)
        self.pages['关于'] = self._build_page_about(self.content)
        for p in self.pages.values():
            p.place(relwidth=1, relheight=1)

        self.titlebar_left.lift()
        self.titlebar_mid.lift()
        self.titlebar_right.lift()

        self._switch('程序')
        self.root.after(200, self._ensure_taskbar)

    # ---------------- 自绘标题栏 ----------------
    TITLEBAR_H = 36
    ROUND_RADIUS = 16

    def _hwnd(self):
        try:
            return ctypes.windll.user32.GetParent(self.root.winfo_id())
        except Exception:
            return None

    def _build_titlebar(self):
        tb_left = tk.Frame(self.root, bg=self.NAV_INACTIVE,
                           height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_left.place(x=0, y=0, width=196, height=self.TITLEBAR_H)
        self.titlebar_left = tb_left
        tb_right = tk.Frame(self.root, bg='#f7f7f7',
                            height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_right.place(relx=1.0, x=-10, y=0, width=118,
                       height=self.TITLEBAR_H, anchor='ne')
        self.titlebar_right = tb_right
        tb_mid = tk.Frame(self.root, bg='#f7f7f7',
                          height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_mid.place(x=196, y=0, relwidth=1, width=-324,
                     height=self.TITLEBAR_H)
        self.titlebar_mid = tb_mid
        self._is_maximized = False
        self._prev_rect = None
        self._drag_off = None
        self._last_press_at = None
        self._dbl_pending = False
        self._press_x = 0
        self._press_y = 0
        self._press_hwnd = None
        self._drag_hold = False
        self._poll_active = False
        self._poll_scheduled = False

        self._tb_btn_close = self._make_tb_btn(tb_right, '\u2715', self._tb_close,
                                               hover_close=True)
        self._tb_btn_close.pack(side='right', fill='y')
        self._tb_btn_max = self._make_tb_btn(tb_right, '\u25a1', self._tb_max)
        self._tb_btn_max.pack(side='right', fill='y')
        self._tb_btn_min = self._make_tb_btn(tb_right, '\u2014', self._tb_min)
        self._tb_btn_min.pack(side='right', fill='y')

        for tb in (tb_left, tb_mid, tb_right):
            tb.bind('<Button-1>', self._tb_drag_start)
            tb.bind('<B1-Motion>', self._tb_drag_move)
            tb.bind('<ButtonRelease-1>', self._tb_drag_end)
            tb.bind('<Double-Button-1>', lambda e: self._tb_toggle_max())
            for w in tb.winfo_children():
                if isinstance(w, tk.Label) and w not in (self._tb_btn_close,
                                                         self._tb_btn_max,
                                                         self._tb_btn_min):
                    w.bind('<Button-1>', self._tb_drag_start)
                    w.bind('<B1-Motion>', self._tb_drag_move)
                    w.bind('<ButtonRelease-1>', self._tb_drag_end)
                    w.bind('<Double-Button-1>', lambda e: self._tb_toggle_max())

        hwnd = self._hwnd()
        if hwnd:
            try:
                ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                ctypes.windll.user32.SetWindowLongW(hwnd, -20,
                                                    ex | 0x40000)
            except Exception:
                pass

    def _strip_chrome(self):
        try:
            self.root.update_idletasks()
            hwnd = self._hwnd()
            if not hwnd:
                return
            st = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
            st &= ~(0x00C00000 | 0x00080000 | 0x00010000 | 0x00040000)
            st |= 0x02000000 | 0x04000000
            ctypes.windll.user32.SetWindowLongW(hwnd, -16, st)
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ex |= 0x40000
            ex &= ~0x80
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex)
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0004 | 0x0020)
            self._apply_round_region(self.ROUND_RADIUS)
        except Exception:
            pass

    def _apply_round_region(self, radius):
        try:
            hwnd = self._hwnd()
            if not hwnd:
                return
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w <= 0 or h <= 0:
                return
            self._last_rgn_size = (w, h)
            pref = 1 if radius <= 0 else 2
            try:
                ret = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 33,
                    ctypes.byref(ctypes.c_int(pref)), 4)
                if ret == 0:
                    ctypes.windll.user32.SetWindowRgn(hwnd, None, True)
                    return
            except Exception:
                pass
            if radius <= 0:
                ctypes.windll.user32.SetWindowRgn(hwnd, None, True)
                return
            r = int(radius)
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, w + 1, h + 1, r * 2, r * 2)
            if not rgn:
                return
            if not ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True):
                ctypes.windll.gdi32.DeleteObject(rgn)
        except Exception:
            pass

    def _on_root_configure(self, e):
        try:
            if e.widget is not self.root:
                return
            if self._dragging:
                return
            if (e.width, e.height) == self._last_rgn_size:
                return
            self._apply_round_region(0 if self._is_maximized else self.ROUND_RADIUS)
        except Exception:
            pass

    def _ensure_taskbar(self):
        self._strip_chrome()

    def _make_tb_btn(self, tb, text, cmd, hover_close=False):
        btn = tk.Label(tb, text=text, bg=tb['bg'], fg='#888888',
                       font=('Segoe UI Symbol', 11), width=3, cursor='hand2',
                       relief='flat', bd=0, highlightthickness=0)
        btn.bind('<Button-1>', lambda e: cmd())
        if hover_close:
            btn.bind('<Enter>', lambda e: btn.configure(bg='#f9e6e6'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=tb['bg']))
        else:
            btn.bind('<Enter>', lambda e: btn.configure(bg='#f8f8f8'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=tb['bg']))
        return btn

    def _set_log_btn_state(self, active):
        try:
            self._log_icon_btn.configure(
                bg='#f7f7f7' if active else '#f7f7f7',
                text='\u2347' if active else '\u269d')
        except Exception:
            pass

    def _tb_min(self):
        hwnd = self._hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def _tb_max(self):
        self._tb_toggle_max()

    def _tb_toggle_max(self):
        hwnd = self._hwnd()
        if not hwnd:
            return
        if self._is_maximized:
            if self._prev_rect:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0,
                    self._prev_rect[0], self._prev_rect[1],
                    self._prev_rect[2] - self._prev_rect[0],
                    self._prev_rect[3] - self._prev_rect[1],
                    0x0004)
            self._is_maximized = False
            self._tb_btn_max.config(text='\u25a1')
            self._apply_round_region(self.ROUND_RADIUS)
        else:
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._prev_rect = (r.left, r.top, r.right, r.bottom)
            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', ctypes.c_ulong),
                    ('rcMonitor', ctypes.wintypes.RECT),
                    ('rcWork', ctypes.wintypes.RECT),
                    ('dwFlags', ctypes.c_ulong),
                ]
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            mon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
            if mon and ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                rc = mi.rcWork
            else:
                sp = wt.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(sp), 0)
                rc = sp
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, rc.left, rc.top,
                rc.right - rc.left, rc.bottom - rc.top,
                0x0004)
            self._is_maximized = True
            self._tb_btn_max.config(text='\u2750')
            self._apply_round_region(0)

    def _tb_close(self):
        self.on_close()

    def _tb_drag_start(self, e):
        now = time.time()
        dbl_ms = ctypes.windll.user32.GetDoubleClickTime()
        if self._last_press_at and (now - self._last_press_at) * 1000.0 < dbl_ms:
            self._last_press_at = None
            self._dbl_pending = False
            self._tb_toggle_max()
            return
        self._last_press_at = now
        self._dbl_pending = True
        self._press_x = e.x_root
        self._press_y = e.y_root
        self._press_hwnd = self._hwnd()
        self.root.after(60, self._check_start_drag)

    def _check_start_drag(self):
        if not getattr(self, '_dbl_pending', False):
            return
        try:
            if not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000):
                self._dbl_pending = False
                return
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            if (abs(pt.x - self._press_x) < 4 and
                    abs(pt.y - self._press_y) < 4):
                self.root.after(50, self._check_start_drag)
                return
        except Exception:
            self._dbl_pending = False
            return
        self._dbl_pending = False
        if self._is_maximized:
            self._tb_toggle_max()
        hwnd = self._press_hwnd or self._hwnd()
        if not hwnd:
            return
        try:
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._drag_off = (pt.x - r.left, pt.y - r.top)
            self._drag_hwnd = hwnd
            self._dragging = True
            self._drag_hold = True
            self._drag_poll()
        except Exception:
            self._drag_hwnd = None
            self._drag_off = None
            self._dragging = False
            self._drag_hold = False
            self._after_drag_sync()

    def _drag_poll(self):
        if not self._dragging:
            return
        try:
            if not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000):
                self._dragging = False
                self._drag_hold = False
                self._drag_hwnd = None
                self._drag_off = None
                self._after_drag_sync()
                return
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            hwnd = getattr(self, '_drag_hwnd', None)
            if hwnd and self._drag_off:
                x = pt.x - self._drag_off[0]
                y = pt.y - self._drag_off[1]
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, 0, 0,
                    0x0001 | 0x0004 | 0x0010)
            self.root.after(5, self._drag_poll)
        except Exception:
            self._dragging = False
            self._drag_hold = False
            self._drag_hwnd = None
            self._drag_off = None
            self._after_drag_sync()

    def _tb_drag_move(self, e):
        return

    def _after_drag_sync(self):
        self._drag_sync_pending = True
        self._drag_hold = False
        if not self._poll_active:
            self._queue_notify()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.after_idle(self._flush_bg_redraw)

    def _flush_bg_redraw(self):
        if self._dragging:
            self._bg_dirty = True
            return
        try:
            w = self.bg.winfo_width()
            h = self.bg.winfo_height()
            if w < 2 or h < 2:
                self.root.after_idle(self._flush_bg_redraw)
                return
            self._drag_sync_pending = False
            self._draw_gradient(self.bg, w, h)
            self._bg_dirty = False
            if not self._is_maximized:
                self._apply_round_region(self.ROUND_RADIUS)
        except Exception:
            self.root.after_idle(self._flush_bg_redraw)

    def _tb_drag_end(self, e):
        if getattr(self, '_dbl_pending', False):
            self._dbl_pending = False
        if self._dragging:
            self._dragging = False
            self._after_drag_sync()

    def _build_nav(self):
        n = self.nav
        _TITLE_AREA_H = 90
        self._nav_title = self.nav.create_text(
            44, _TITLE_AREA_H // 2, text='Masker', anchor='w',
            fill=self.NAV_TEXT, font=('Hartwell Alt Bold', 24),
            tags='nav_title')
        for ev, fn in (('<Button-1>', self._tb_drag_start),
                       ('<B1-Motion>', self._tb_drag_move),
                       ('<ButtonRelease-1>', self._tb_drag_end),
                       ('<Double-Button-1>', self._tb_toggle_max)):
            self.nav.tag_bind('nav_title', ev, fn)
        items = ['程序']
        self._nav_tags = {}
        self._nav_icon_w = 13
        self._nav_text_x = 44 + self._nav_icon_w + 6
        self._nav_text_cx = (14 + 182) / 2.0
        y0 = 96
        for i, name in enumerate(items):
            y = y0 + i * 40
            tag = 'nav_%d' % i
            ttag = tag + '_t'
            itag = tag + '_i'
            self._nav_tags[name] = (tag, y)
            self._rounded(n, 14, y, 182, y + 34, 10, fill=self.NAV_INACTIVE,
                          outline='', tags=tag)
            n.create_text(44, y + 17, anchor='w', text=self.NAV_ICONS.get(name, ''),
                          fill=self.NAV_TEXT, font=('Font Awesome 6 Free', 10),
                          tags=itag)
            n.create_text(self._nav_text_x, y + 17, anchor='w', text=name,
                          fill=self.NAV_TEXT, font=(self.FONT, 10, 'bold'),
                          tags=ttag)
            n.tag_bind(tag, '<Button-1>', lambda e, nm=name: self._switch(nm))
            n.tag_bind(tag, '<Enter>', lambda e, nm=name: self._nav_hover(nm, True))
            n.tag_bind(tag, '<Leave>', lambda e, nm=name: self._nav_hover(nm, False))
            n.tag_bind(ttag, '<Button-1>', lambda e, nm=name: self._switch(nm))
            n.tag_bind(itag, '<Button-1>', lambda e, nm=name: self._switch(nm))

        name = '设置'
        tag = 'nav_settings'
        ttag = tag + '_t'
        itag = tag + '_i'
        y = 540
        self._nav_tags[name] = (tag, y)
        self._nav_settings_poly = self._rounded(
            n, 14, y, 182, y + 34, 10, fill=self.NAV_INACTIVE, outline='',
            tags=tag)
        self._nav_settings_icon = n.create_text(
            44, y + 17, anchor='w', text=self.NAV_ICONS.get(name, ''),
            fill=self.NAV_TEXT, font=('Font Awesome 6 Free', 10), tags=itag)
        self._nav_settings_label = n.create_text(
            self._nav_text_x, y + 17, anchor='w', text=name, fill=self.NAV_TEXT,
            font=(self.FONT, 10, 'bold'), tags=ttag)
        n.tag_bind(tag, '<Button-1>', lambda e, nm=name: self._switch(nm))
        n.tag_bind(tag, '<Enter>', lambda e, nm=name: self._nav_hover(nm, True))
        n.tag_bind(tag, '<Leave>', lambda e, nm=name: self._nav_hover(nm, False))
        n.tag_bind(ttag, '<Button-1>', lambda e, nm=name: self._switch(nm))
        n.tag_bind(itag, '<Button-1>', lambda e, nm=name: self._switch(nm))

        self._nav_divider = n.create_line(16, 524, 180, 524, fill='#e8e8e8')
        n.bind('<Configure>', self._relayout_nav)

    def _relayout_nav(self, event=None):
        try:
            h = self.nav.winfo_height()
            if h < 200:
                return
            y = h - 52
            x1, x2, r = 14, 182, 10
            pts = [x1 + r, y, x2 - r, y, x2, y, x2, y + r,
                   x2, y + 34 - r, x2, y + 34, x2 - r, y + 34,
                   x1 + r, y + 34, x1, y + 34, x1, y + 34 - r,
                   x1, y + r, x1, y]
            self.nav.coords(self._nav_settings_poly, *pts)
            self.nav.coords(self._nav_settings_icon, 44, y + 17)
            self.nav.coords(self._nav_settings_label, self._nav_text_x, y + 17)
            self.nav.coords(self._nav_divider, 16, y - 16, 180, y - 16)
            self._nav_tags['设置'] = (self._nav_tags['设置'][0], y)
        except Exception:
            pass

    def _nav_hover(self, name, enter):
        tag, y = self._nav_tags[name]
        if name == self.current_page:
            return
        self.nav.itemconfig(tag, fill='#f0f0f0' if enter else self.NAV_INACTIVE)

    def _nav_redraw(self):
        for name, (tag, y) in self._nav_tags.items():
            active = (name == self.current_page)
            self.nav.itemconfig(tag, fill=self.NAV_ACTIVE if active else self.NAV_INACTIVE)
            for i in self.nav.find_withtag(tag + '_t'):
                self.nav.itemconfig(i, fill=self.NAV_TEXT_ACTIVE if active else self.NAV_TEXT)
            for i in self.nav.find_withtag(tag + '_i'):
                self.nav.itemconfig(i, fill=self.NAV_TEXT_ACTIVE if active else self.NAV_TEXT)

    # ---------------- 导航文字动画 ----------------
    def _nav_anim_start(self):
        base = self._nav_text_base_x()
        cx = getattr(self, '_nav_text_cx', 98)
        if getattr(self, '_nav_anim_after_id', None) is not None:
            try:
                self.root.after_cancel(self._nav_anim_after_id)
            except Exception:
                pass
            self._nav_anim_after_id = None
        items = {}
        for nm, (tag, y) in self._nav_tags.items():
            ttag = tag + '_t'
            coords = self.nav.coords(ttag)
            cur = coords[0] if coords else base
            target = cx if nm == self.current_page else base
            if abs(cur - target) > 1:
                items[nm] = [ttag, cur, target]
        if not items:
            self._nav_anim_running = False
            self._nav_anim_items = {}
            return
        self._nav_anim_running = True
        self._nav_anim_items = items
        self._nav_anim_step()

    def _nav_anim_step(self):
        items = getattr(self, '_nav_anim_items', None)
        if not items:
            self._nav_anim_running = False
            return
        step = 4
        done = True
        for nm, (ttag, cur, target) in items.items():
            _co = self.nav.coords(ttag)
            y = _co[1] if len(_co) > 1 else 0
            if abs(cur - target) <= step:
                self.nav.coords(ttag, target, y)
            else:
                cur += step if target > cur else -step
                self.nav.coords(ttag, cur, y)
                items[nm][1] = cur
                done = False
        if done:
            self._nav_anim_running = False
            self._nav_anim_items = {}
            self._nav_anim_after_id = None
        else:
            self._nav_anim_after_id = self.root.after(16, self._nav_anim_step)

    def _nav_text_base_x(self):
        return getattr(self, '_nav_text_x', 60)

    # ---------------- 程序页 ----------------
    def _build_page_program(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='程序', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        # 切换开关（fa-right-left）：一键在网关 A / B 之间切换默认路由
        self.btn_toggle = tk.Button(head, text='\uf362', bg='#f7f7f7', fg='#35d07f',
                                    activebackground='#f0f0f0', activeforeground='#35d07f',
                                    relief='flat', bd=0, highlightthickness=0,
                                    font=('Font Awesome 6 Free', 16),
                                    cursor='hand2', command=self._toggle_gateway)
        self.btn_toggle.pack(side='left', padx=(16, 0))
        tk.Label(head, text='切换默认网关（192.168.2.1 ↔ 192.168.2.2）',
                 bg='#f7f7f7', fg=self.TEXT_DIM,
                 font=(self.FONT, 11)).pack(side='left', padx=(10, 0))

        btn_kw = dict(bg='#f7f7f7', bd=0, relief='flat', highlightthickness=0,
                      cursor='hand2')
        self._log_icon_btn = tk.Label(head, text='\u269d', fg='#888888',
                                      font=('Segoe UI Symbol', 15), **btn_kw)
        self._log_icon_btn.pack(side='right')
        self._log_icon_btn.bind('<Button-1>', lambda e: self._toggle_log_panel())
        self._log_icon_btn.bind('<Enter>', lambda e: self._log_icon_btn.configure(
            bg='#f0f0f0'))
        self._log_icon_btn.bind('<Leave>', lambda e: self._set_log_btn_state(
            self._log_visible))

        # 大数字卡片
        cards = tk.Frame(page, bg='#f7f7f7')
        cards.pack(fill='x', padx=28, pady=(10, 6))
        cards.grid_columnconfigure(0, weight=1, uniform='c')
        cards.grid_columnconfigure(1, weight=1, uniform='c')

        self.gw_card = tk.Canvas(cards, height=172, bg='#f7f7f7',
                                 highlightthickness=0, bd=0)
        self.gw_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.info_card = tk.Canvas(cards, height=172, bg='#f7f7f7',
                                   highlightthickness=0, bd=0)
        self.info_card.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        cards.bind('<Configure>', lambda e: self._draw_cards(e.width))

        # 切换目标网关变量（设置页可修改，卡片底部候选网关展示用）
        self.gw_a_var = tk.StringVar(value=DEFAULT_GW_A)
        self.gw_b_var = tk.StringVar(value=DEFAULT_GW_B)

        # 站点网络卡片（百度搜索 / GitHub / YouTube / 谷歌，横向 4 卡）
        self._site_delay = {i: None for i in range(len(SITES))}
        self._site_speed = {i: None for i in range(len(SITES))}
        site_row = tk.Frame(page, bg='#f7f7f7')
        site_row.pack(fill='x', padx=28, pady=(4, 6))
        for i in range(len(SITES)):
            site_row.grid_columnconfigure(i, weight=1, uniform='s')
        self.site_cards = []
        for i in range(len(SITES)):
            sc = tk.Canvas(site_row, height=118, bg='#f7f7f7',
                           highlightthickness=0, bd=0)
            sc.grid(row=0, column=i, sticky='nsew',
                    padx=(0 if i == 0 else 4, 0 if i == len(SITES) - 1 else 4))
            self.site_cards.append(sc)
        self._site_row = site_row
        site_row.bind('<Configure>', lambda e: self._draw_site_cards(e.width))

        # 右侧可收起日志侧边栏（同原程序做法）
        self.log_panel = tk.Frame(self.root, bg='#f7f7f7', highlightthickness=0)
        self._log_visible = False
        self._log_anim = False
        self._log_cur = 0
        self._log_target = 0
        self.log_panel.place_forget()
        lp_sep = tk.Frame(self.log_panel, bg='#e8e8e8', width=1)
        lp_sep.pack(side='left', fill='y')
        lp_head = tk.Frame(self.log_panel, bg='#f7f7f7')
        lp_head.pack(fill='x', pady=(40, 0))
        tk.Label(lp_head, text='运行日志', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 14, 'bold')).pack(pady=(10, 8))
        self.log_text = tk.Text(self.log_panel, height=8, bg='#f7f7f7', fg='#333333',
                                insertbackground='#333333', relief='flat', bd=0,
                                font=('Consolas', 9), state='disabled', wrap='word',
                                padx=10, pady=4)
        sb = tk.Canvas(self.log_panel, width=10, bg='#f7f7f7',
                       highlightthickness=0, bd=0)
        self._log_sb = sb
        self._log_sb_hide_after = None
        self._log_sb_drag_off = 0
        self._log_sb_geo = sb.create_rectangle(0, 0, 10, 20, fill='', outline='')
        self._log_sb_item = sb.create_polygon(
            *_round_rect_pts(0, 0, 10, 20, 4),
            fill='#ffffff', outline='#d8d8d8', width=1, smooth=True)
        sb.tag_raise('all')
        self.log_text.bind('<MouseWheel>', self._log_sb_kick)
        sb.bind('<ButtonPress-1>', self._log_sb_press)
        sb.bind('<B1-Motion>', self._log_sb_drag)
        sb.bind('<ButtonRelease-1>', lambda e: self._log_sb_kick(e, release=True))
        self.log_text.configure(yscrollcommand=self._log_sb_update)
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log('程序已就绪。点击顶部绿色开关在 192.168.2.1 与 192.168.2.2 之间切换默认网关。')

        # 网络类别标签交互（右上角 badge：点击切换 专用/公用；未知不可点击）
        self.gw_card.tag_bind('net_cat', '<Button-1>', self._toggle_network_category)
        self.gw_card.tag_bind('net_cat', '<Enter>', self._net_badge_enter)
        self.gw_card.tag_bind('net_cat', '<Leave>', self._net_badge_leave)

        return page

    def _log_sb_kick(self, event=None, release=False):
        sb = getattr(self, '_log_sb', None)
        if sb is None:
            return
        try:
            if not sb.winfo_ismapped():
                head = getattr(self, 'log_panel', None).winfo_children()[1]
                y0 = head.winfo_y() + head.winfo_height() + 4
                h0 = self.log_panel.winfo_height() - y0 - 8
                if h0 < 40:
                    h0 = 40
                sb.place(relx=1.0, rely=0.0, anchor='ne',
                         y=y0, height=h0, width=10)
                self.root.after_idle(self._log_sb_refresh)
        except Exception:
            return
        delay = 1200 if release else 2000
        if self._log_sb_hide_after is not None:
            try:
                self.root.after_cancel(self._log_sb_hide_after)
            except Exception:
                pass
            self._log_sb_hide_after = None
        self._log_sb_hide_after = self.root.after(delay, self._log_sb_hide)

    def _log_sb_update(self, first, last):
        self._log_sb_refresh()

    def _log_sb_refresh(self):
        sb = getattr(self, '_log_sb', None)
        if sb is None or not sb.winfo_ismapped():
            return
        try:
            first, last = self.log_text.yview()
            h = sb.winfo_height()
            frac = max(0.0, min(1.0, last - first))
            thumb_h = max(20, int(h * frac))
            y0 = int(first * (h - thumb_h)) if h > thumb_h else 0
            w = sb.winfo_width()
            sb.coords(self._log_sb_geo, 0, y0, w, y0 + thumb_h)
            sb.coords(self._log_sb_item,
                      *_round_rect_pts(0, y0, w, y0 + thumb_h, 4))
        except Exception:
            pass

    def _log_sb_press(self, event):
        self._log_sb_kick(event)
        try:
            c = self._log_sb.coords(self._log_sb_geo)
            self._log_sb_drag_off = event.y - c[1]
        except Exception:
            self._log_sb_drag_off = 0

    def _log_sb_drag(self, event):
        self._log_sb_kick(event)
        try:
            h = self._log_sb.winfo_height()
            c = self._log_sb.coords(self._log_sb_geo)
            thumb_h = c[3] - c[1]
            track = h - thumb_h
            if track <= 0:
                return
            frac = min(1.0, max(0.0,
                                (event.y - self._log_sb_drag_off) / track))
            self.log_text.yview_moveto(frac)
        except Exception:
            pass

    def _log_sb_hide(self):
        self._log_sb_hide_after = None
        sb = getattr(self, '_log_sb', None)
        if sb is None:
            return
        try:
            if sb.winfo_ismapped():
                sb.place_forget()
        except Exception:
            pass

    def _toggle_log_panel(self):
        if self._log_anim:
            return
        self._log_anim = True
        if self._log_visible:
            self._log_target = 0
        else:
            self._log_visible = True
            self._log_cur = 0
            self.log_panel.place(relx=1.0, rely=0.0, anchor='ne',
                                 relheight=1.0, width=0)
            self.titlebar_left.lift()
            self.titlebar_mid.lift()
            self.titlebar_right.lift()
            self._set_log_btn_state(True)
            self._log_target = 300
        self._log_step()

    def _log_step(self):
        step = 28
        if self._log_target > self._log_cur:
            self._log_cur = min(self._log_cur + step, self._log_target)
        else:
            self._log_cur = max(self._log_cur - step, 0)
        self.log_panel.place(relx=1.0, rely=0.0, anchor='ne',
                             relheight=1.0, width=self._log_cur)
        self.content.place_configure(x=196, y=0, relheight=1, relwidth=1,
                                     width=-(196 + self._log_cur), height=0)
        if self._log_cur != self._log_target:
            self.root.after(12, self._log_step)
        else:
            self._log_anim = False
            if self._log_target == 0:
                self.log_panel.place_forget()
                self.content.place_configure(x=196, y=0, relwidth=1,
                                             relheight=1, width=-196, height=0)
                self._log_visible = False
                self._set_log_btn_state(False)

    def _draw_cards(self, w):
        for c in (self.gw_card, self.info_card):
            c.delete('all')
        if w < 40:
            return
        cw = (w - 16) // 2
        ch = 172

        # 当前 IPv4 默认网关卡（route print 实时解析）
        self._rounded(self.gw_card, 0, 0, cw, ch, 18, fill=self.CARD_BG,
                      outline='', width=0)
        self.gw_card.create_text(22, 34, anchor='w', text='当前 IPv4 默认网关',
                                 fill=self.TEXT_DIM, font=(self.FONT, 12))
        self.gw_card.create_text(22, 52, anchor='w', text='route print 实时解析',
                                 fill='#888888', font=(self.FONT, 8))
        self.gw_value = self.gw_card.create_text(cw // 2, 108, text='--',
                                                 fill=self.ACCENT,
                                                 font=(self.FONT, 26, 'bold'))
        self.gw_card.create_text(22, 152, anchor='w', text='0.0.0.0/0 默认路由',
                                 fill='#888888', font=(self.FONT, 8))

        # 网络配置文件类别标签（右上角可点击：专用<->公用切换；未知不可点击）
        cat = getattr(self, '_net_cat', '未知')
        if cat == '未知':
            btext, bfill = '未知', '#bbbbbb'
        elif cat == '专用':
            btext, bfill = '专用网络', self.ACCENT
        else:
            btext, bfill = '公用网络', '#f5a623'
        bw, bh = 90, 26
        bx0 = cw - 8 - bw
        by0 = 20
        self._net_cat_badge_bg = self._rounded(
            self.gw_card, bx0, by0, bx0 + bw, by0 + bh, 13,
            fill=bfill, outline='', width=0, tags='net_cat')
        self._net_cat_badge_text = self.gw_card.create_text(
            bx0 + bw // 2, by0 + bh // 2, text=btext, fill='#ffffff',
            font=(self.FONT, 9, 'bold'), tags='net_cat')

        # 网卡信息卡（默认路由所在活动网卡：接口名 / 子网掩码 / IPv4 / 候选网关）
        self._rounded(self.info_card, 0, 0, cw, ch, 18, fill=self.CARD_BG,
                      outline='', width=0)
        self.info_card.create_text(22, 34, anchor='w', text='网卡信息',
                                   fill=self.TEXT_DIM, font=(self.FONT, 12))
        self.info_sub = self.info_card.create_text(
            22, 52, anchor='w',
            text='%s / %s' % (self._last_ifname, self._last_mask),
            fill='#888888', font=(self.FONT, 8))
        self.info_value = self.info_card.create_text(cw // 2, 108, text='--',
                                                     fill=self.TEXT_MAIN,
                                                     font=(self.FONT, 26, 'bold'))
        self.info_card.create_text(
            22, 152, anchor='w',
            text='候选网关 %s / %s' % (self.gw_a_var.get(), self.gw_b_var.get()),
            fill='#888888', font=(self.FONT, 8))
        try:
            self.gw_card.itemconfig(self.gw_value, text=self._last_gw,
                                    fill=self._gw_fill(self._last_gw))
            self.info_card.itemconfig(self.info_value, text=self._last_ip)
            self.info_card.itemconfig(self.info_sub,
                                      text='%s / %s' % (self._last_ifname,
                                                        self._last_mask))
        except Exception:
            pass

    def _draw_site_cards(self, w):
        """绘制 4 个横向站点卡片：站点名 + 主机名 + 延迟 + 网速。"""
        if not getattr(self, 'site_cards', None):
            return
        if w < 40:
            return
        gap = 8
        cw = (w - gap * (len(self.site_cards) - 1)) // len(self.site_cards)
        ch = 118
        for i, sc in enumerate(self.site_cards):
            sc.delete('all')
            self._rounded(sc, 0, 0, cw, ch, 14, fill=self.CARD_BG,
                          outline='', width=0)
            name, host = SITES[i]
            sc.create_text(cw // 2, 22, text=name,
                           fill=self.TEXT_MAIN, font=(self.FONT, 11, 'bold'))
            sc.create_text(cw // 2, 40, text=host,
                           fill='#888888', font=(self.FONT, 8))
            delay = self._site_delay.get(i)
            if delay is None:
                dtext, dcolor = '--', '#bbbbbb'
            else:
                dtext = '%.0f ms' % delay
                dcolor = self.ACCENT if delay < 100 else (
                    '#f5b73d' if delay < 300 else '#ff5d5d')
            sc.create_text(cw // 2, 72, text=dtext,
                           fill=dcolor, font=(self.FONT, 17, 'bold'))
            speed = self._site_speed.get(i)
            stext = '网速 --' if speed is None else '网速 %.1f Mbps' % speed
            sc.create_text(cw // 2, 96, text=stext,
                           fill='#888888', font=(self.FONT, 9))

    def _probe_now(self):
        """立即触发一轮站点探测（切换网关后调用）。"""
        try:
            self._probe_event.set()
        except Exception:
            pass

    def _probe_once(self):
        """并行探测全部站点，结果写入队列刷新卡片。"""
        try:
            futs = {self._probe_pool.submit(probe_site, host): idx
                    for idx, (_name, host) in enumerate(SITES)}
            for fut, idx in futs.items():
                try:
                    delay, speed, _ok = fut.result()
                except Exception:
                    delay, speed = None, None
                try:
                    self.q.put(('siteinfo', idx, SITES[idx][0], delay, speed))
                    self._queue_notify()
                except Exception:
                    pass
        except Exception:
            pass

    def _probe_worker(self):
        """后台探测线程：空闲等待触发事件；触发后按 PROBE_INTERVAL 采样
        PROBE_DURATION 时长后自动停止，等待下一次触发（启动/切换成功触发）。"""
        while not self._probe_stop:
            self._probe_event.wait()
            self._probe_event.clear()
            end = time.time() + PROBE_DURATION
            while time.time() < end and not self._probe_stop:
                self._probe_once()
                self._probe_event.wait(timeout=PROBE_INTERVAL)
                self._probe_event.clear()

    def _gw_fill(self, gs):
        """当前网关命中任一候选网关显示绿色，否则橙色警示；未知/占位显示绿色。"""
        try:
            if gs in (self.gw_a_var.get(), self.gw_b_var.get()):
                return self.ACCENT
            if gs in ('--', '未知'):
                return self.ACCENT
            return '#f5b73d'
        except Exception:
            return '#f5b73d'

    # ---------------- 设置页 ----------------
    def _build_page_settings(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='设置', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        tk.Button(head, text='关于', command=lambda: self._switch('关于'),
                  bg='#333333', fg='#ffffff', activebackground='#555555',
                  activeforeground='#ffffff', relief='flat', bd=0,
                  font=(self.FONT, 10, 'bold'), cursor='hand2',
                  padx=14, pady=2).pack(side='right')

        card = tk.Frame(page, bg='#fbfbfb', highlightbackground=self.CARD_BORDER,
                        highlightthickness=1)
        card.pack(fill='x', padx=28, pady=(18, 10))

        row3 = tk.Frame(card, bg='#fbfbfb')
        row3.pack(fill='x', padx=24, pady=(20, 6))
        tk.Label(row3, text='网关 A', bg='#fbfbfb', fg=self.TEXT_MAIN,
                 font=(self.FONT, 12)).pack(side='left')
        self.gw_a_entry = tk.Entry(row3, textvariable=self.gw_a_var, width=14,
                                   bg='#fbfbfb', fg='#333333', relief='flat',
                                   insertbackground='#333333',
                                   font=(self.FONT, 12))
        self.gw_a_entry.pack(side='left', padx=(10, 0))
        tk.Label(row3, text='网关 B', bg='#fbfbfb', fg=self.TEXT_MAIN,
                 font=(self.FONT, 12)).pack(side='left', padx=(30, 0))
        self.gw_b_entry = tk.Entry(row3, textvariable=self.gw_b_var, width=14,
                                   bg='#fbfbfb', fg='#333333', relief='flat',
                                   insertbackground='#333333',
                                   font=(self.FONT, 12))
        self.gw_b_entry.pack(side='left', padx=(10, 0))
        tk.Label(row3, text='切换目标网关（默认 192.168.2.1 / 192.168.2.2）',
                 bg='#fbfbfb', fg='#888888', font=(self.FONT, 9)).pack(side='left', padx=(16, 0))
        tk.Button(row3, text='应用', width=10, bg='#333333', fg='#ffffff',
                  activebackground='#555555', activeforeground='#ffffff',
                  relief='flat', bd=0, font=(self.FONT, 11, 'bold'), cursor='hand2',
                  command=self._apply_gateways).pack(side='left', padx=(24, 0))

        row4 = tk.Frame(card, bg='#fbfbfb')
        row4.pack(fill='x', padx=24, pady=(12, 14))
        tk.Label(row4, text='启动设置', bg='#fbfbfb', fg=self.TEXT_MAIN,
                 font=(self.FONT, 12)).pack(side='left')
        self.auto_start_var = tk.BooleanVar(value=False)
        tk.Button(row4, text='开机自启', command=self._apply_auto_start,
                  bg='#35d07f', fg='#ffffff', relief='flat', bd=0,
                  activebackground='#2bb26c', activeforeground='#ffffff',
                  font=(self.FONT, 10), cursor='hand2', padx=16, pady=2).pack(side='left', padx=(24, 0))
        tk.Label(row4, text='经计划任务 %s 生效' % AUTOSTART_TASK_NAME,
                 bg='#fbfbfb', fg='#888888', font=(self.FONT, 9)).pack(side='left', padx=(16, 0))

        return page

    # ---------------- 关于页 ----------------
    def _build_page_about(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='关于', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        tk.Button(head, text='返回', command=lambda: self._switch('设置'),
                  bg='#333333', fg='#ffffff', activebackground='#555555',
                  activeforeground='#ffffff', relief='flat', bd=0,
                  font=(self.FONT, 10, 'bold'), cursor='hand2',
                  padx=14, pady=2).pack(side='right')

        card = tk.Frame(page, bg='#fbfbfb', highlightbackground=self.CARD_BORDER,
                        highlightthickness=1)
        card.pack(fill='x', padx=28, pady=(18, 10))
        lines = [
            '· 切换方式：netsh interface ip delete/add route 0.0.0.0/0 增删默认路由',
            '· 目标网关：A = 192.168.2.1，B = 192.168.2.2（可在设置页修改）',
            '· 闪断提示：切换瞬间网络会短暂闪断，属正常现象，请避开关键业务时段',
            '· 权限要求：修改默认路由需要管理员权限（exe 已内置 UAC 提权清单）',
            '· 切换：点击程序页顶部绿色开关，在网关 A（192.168.2.1）与网关 B（192.168.2.2）之间切换默认路由',
            '· 日志：右侧栏记录每次切换命令与结果，可随时展开查看',
        ]
        for i, ln in enumerate(lines):
            tk.Label(card, text=ln, bg='#fbfbfb', fg='#888888',
                     font=(self.FONT, 9), anchor='w', justify='left').pack(fill='x', pady=2)

        return page

    # ---------------- 行为 ----------------
    def _switch(self, name):
        self.current_page = name
        for p in self.pages.values():
            p.place_forget()
        self.pages[name].place(relwidth=1, relheight=1)
        self._nav_redraw()
        self._nav_anim_start()

    def _toggle_gateway(self):
        """切换开关：按当前默认网关自动决定方向，在网关 A / B 之间切换。"""
        if self._switch_running:
            self.log('切换正在进行中，请稍候...')
            return
        ifname, cur_gw = detect_default_route()
        a = self._target_gw('a')
        b = self._target_gw('b')
        if cur_gw == b:
            which = 'a'
        else:
            which = 'b'
        target = a if which == 'a' else b
        if cur_gw == target:
            self.log('当前默认网关已是 %s，无需切换' % target)
            return
        self.log('正在切换默认网关 %s -> %s，网络将短暂闪断...'
                 % (cur_gw or '无', target))
        self._switch_running = True
        threading.Thread(target=self._switch_worker,
                         args=(ifname, target), daemon=True).start()

    def _target_gw(self, which):
        if which == 'a':
            return self.gw_a_var.get().strip() or DEFAULT_GW_A
        return self.gw_b_var.get().strip() or DEFAULT_GW_B

    def _switch_worker(self, ifname, target):
        ok, msg = do_switch_gateway(ifname, target)
        try:
            self.q.put(('log', msg))
            self._queue_notify()
        except Exception:
            pass
        try:
            self.q.put(('gateway_updated', ok, target))
            self._queue_notify()
        except Exception:
            pass
        if ok:
            # 切换成功后立即刷新网卡信息（接口/地址可能变化）
            niname, nip, nmask = get_active_ipv4()
            try:
                self.q.put(('netinfo', niname, nip, nmask))
                self._queue_notify()
            except Exception:
                pass

    def _apply_gateways(self):
        """应用网关 A/B：校验 IP 格式后保存，并刷新卡片候选网关展示。"""
        ip_pat = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
        a = self.gw_a_var.get().strip()
        b = self.gw_b_var.get().strip()
        if not ip_pat.match(a) or not ip_pat.match(b):
            self.log('网关地址格式不正确，请输入 IPv4 地址（如 192.168.2.1）')
            return
        self.gw_a_var.set(a)
        self.gw_b_var.set(b)
        save_config(bool(self.auto_start_var.get()), gw_a=a, gw_b=b)
        self._refresh_view()
        self.log('网关已保存：A=%s  B=%s' % (a, b))

    # ---------------- 启动设置（开机自启） ----------------
    def _apply_auto_start(self):
        import tkinter.messagebox as _mb
        enabled = not bool(self.auto_start_var.get())
        task = AUTOSTART_TASK_NAME
        if enabled:
            msg = ('将创建开机自启计划任务：\n\n'
                   '任务名：%s\n'
                   '触发：用户登录时（schtasks /sc onlogon）\n'
                   '权限：最高权限（/rl highest）\n'
                   '动作：静默启动 %s --silent\n\n'
                   '确认后立即创建，下次登录自动进入托盘。'
                   % (task, autostart_exe_path()))
        else:
            msg = ('将删除开机自启计划任务：\n\n'
                   '任务名：%s\n\n'
                   '确认后立即删除，开机不再自动启动。' % task)
        if not _mb.askyesno('开机自启确认', msg, parent=self.root):
            return
        ok, m = set_autostart_task(enabled)
        save_config(enabled)
        if ok:
            self.auto_start_var.set(enabled)
            self.log('开机自启已%s：%s' % ('开启' if enabled else '关闭', m))
        else:
            self.log('开机自启设置失败：%s' % m)

    def _apply_startup_settings(self):
        cfg = load_config()
        self.auto_start_var.set(cfg['auto_start'])
        self.gw_a_var.set(cfg['gw_a'])
        self.gw_b_var.set(cfg['gw_b'])
        if cfg['auto_start'] and not is_autostart_task_set():
            ok, msg = set_autostart_task(True)
            self.log('开机自启：%s' % msg)
        # 启动后立即按网关1.exe 方式实时解析当前默认网关与网卡信息并展示
        self.root.after(200, self._refresh_view)

    def _queue_notify(self):
        """事件驱动唤醒：后台线程入队后调用，空闲时无定时器轮询。"""
        if self._poll_scheduled:
            return
        self._poll_scheduled = True
        try:
            self.root.after(0, self._poll_queue)
        except Exception:
            self._poll_scheduled = False

    def _poll_queue(self):
        self._poll_scheduled = False
        if self._drag_hold:
            self._poll_active = False
            return
        self._poll_active = True
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == 'log':
                    self.log(item[1])
                elif item[0] == 'netinfo':
                    _, niname, nip, nmask = item
                    self._update_netinfo(niname, nip, nmask)
                elif item[0] == 'siteinfo':
                    _, sidx, _sname, sdelay, sspeed = item
                    self._site_delay[sidx] = sdelay
                    self._site_speed[sidx] = sspeed
                    try:
                        self._draw_site_cards(self._site_row.winfo_width())
                    except Exception:
                        pass
                    # 探测结果到达后同步托盘图标（网关 B 时按谷歌连通红/绿实时切换）
                    self._update_tray_icon()
                elif item[0] == 'gateway_updated':
                    _, ok, target = item
                    self._switch_running = False
                    if ok:
                        self._cur_gw = target
                        self.log('默认网关已切换为 %s' % target)
                    else:
                        self.log('切换未生效，请检查 netsh 权限或接口状态')
                    # 切换后立即按网关1.exe 方式实时刷新默认网关与网卡信息展示
                    self._refresh_view()
                elif item[0] == 'netcat':
                    _, nname, nguid, ncat = item
                    self._net_cat_name = nname
                    self._net_cat_guid = nguid
                    self._net_cat = ncat or '未知'
                    self._update_net_badge()
                elif item[0] == 'net_cat_updated':
                    _, ok, target, msg, pguid = item
                    self._net_switch_running = False
                    self.log(msg)
                    if ok:
                        if pguid:
                            # 立即按 ProfileGuid 从注册表读取最新类别，标签实时跟随实际状态
                            rname, rcat = _read_profile_category(pguid)
                            if rcat is not None:
                                self._net_cat_name = rname or self._net_cat_name
                                self._net_cat_guid = pguid
                                self._net_cat = rcat
                                self._update_net_badge()
                                self.log('界面标签已刷新：%s' % rcat)
                            else:
                                self._refresh_net_profile()
                        else:
                            self._refresh_net_profile()
        except queue.Empty:
            pass
        # 事件驱动：队列清空后不再自循环调度，等下次入队通知

    def _refresh_view(self):
        """按网关1.exe 方式实时解析并刷新展示：
        当前 IPv4 默认网关（route print）+ 网卡接口名 / IPv4 / 掩码。"""
        try:
            ifname, gw = detect_default_route()
        except Exception:
            ifname, gw = None, None
        self._last_gw = gw or '--'
        self._last_ifname = ifname or '--'
        if gw:
            self._cur_gw = gw
        try:
            niname, nip, nmask = get_active_ipv4()
        except Exception:
            niname, nip, nmask = None, None, '--'
        self._update_netinfo(niname, nip, nmask)
        try:
            self.gw_card.itemconfig(self.gw_value, text=self._last_gw,
                                    fill=self._gw_fill(self._last_gw))
        except Exception:
            pass
        # 切换网关后立即刷新 4 个站点延迟/网速卡片
        self._probe_now()
        # 网络配置文件类别实时刷新（后台线程读取，避免阻塞 UI）
        self._refresh_net_profile()
        # 同步托盘图标颜色（网关 B 红色 / 网关 A 原色）
        self._update_tray_icon()

    def _update_netinfo(self, ifname, ip, mask='--'):
        """更新程序页网卡信息：接口名 / 子网掩码 / IPv4 地址（大数字卡 + 信息条）。"""
        self._last_ifname = ifname or '--'
        self._last_ip = ip or '--'
        self._last_mask = mask or '--'
        try:
            self.info_card.itemconfig(self.info_value, text=self._last_ip)
            self.info_card.itemconfig(
                self.info_sub,
                text='%s / %s' % (self._last_ifname, self._last_mask))
        except Exception:
            pass

    # ---------------- 网络配置文件类别（专用/公用）显示与切换 ----------------
    def _refresh_net_profile(self):
        """后台线程读取当前活动网络配置文件类别，结果经队列回主线程刷新 UI。"""

        def _worker():
            try:
                nname, nguid, ncat = get_network_profile()
                self.q.put(('netcat', nname, nguid, ncat))
                self._queue_notify()
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _update_net_badge(self):
        """刷新右上角网络类别标签文本与颜色。"""
        try:
            if not (hasattr(self, 'gw_card')
                    and hasattr(self, '_net_cat_badge_text')
                    and hasattr(self, '_net_cat_badge_bg')):
                return
            cat = self._net_cat
            if cat == '未知':
                text, fill = '未知', '#bbbbbb'
            elif cat == '专用':
                text, fill = '专用网络', self.ACCENT
            else:
                text, fill = '公用网络', '#f5a623'
            self.gw_card.itemconfig(self._net_cat_badge_text, text=text, fill='#ffffff')
            self.gw_card.itemconfig(self._net_cat_badge_bg, fill=fill)
        except Exception:
            pass

    def _toggle_network_category(self, event=None):
        """点击右上角标签：切换网络配置文件类别（专用<->公用）。"""
        if self._net_switch_running:
            self.log('网络类别切换进行中，请稍候...')
            return
        if not self._net_cat or self._net_cat == '未知':
            self.log('未检测到活动网络配置文件，无法切换网络类别（显示为未知）')
            return
        target = '公用' if self._net_cat == '专用' else '专用'
        self._net_switch_running = True
        self.log('正在切换网络类别：%s -> %s ...' % (self._net_cat, target))
        threading.Thread(target=self._net_switch_worker,
                         args=(target,), daemon=True).start()

    def _net_switch_worker(self, target):
        ok, msg, pguid = set_network_category(target)
        try:
            self.q.put(('net_cat_updated', ok, target, msg, pguid))
            self._queue_notify()
        except Exception:
            pass

    def _net_badge_enter(self, event=None):
        """标签 hover：手型光标 + 加深底色（未知不可点击时无变化）。"""
        try:
            if getattr(self, '_net_cat', '未知') == '未知':
                return
            self.root.config(cursor='hand2')
            if hasattr(self, '_net_cat_badge_bg'):
                dark = '#2bb26c' if self._net_cat == '专用' else '#d9871a'
                self.gw_card.itemconfig(self._net_cat_badge_bg, fill=dark)
        except Exception:
            pass

    def _net_badge_leave(self, event=None):
        """标签离开：恢复光标与底色。"""
        try:
            self.root.config(cursor='')
        except Exception:
            pass
        self._update_net_badge()

    def log(self, msg):
        try:
            self.log_text.config(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        except Exception:
            pass

    def on_close(self):
        """点击窗口 X：先启动托盘，再隐藏主窗口（程序驻留托盘）。"""
        if not _HAS_TRAY:
            self.log('未安装 pystray/Pillow，无法托盘化，直接退出')
            self._quit()
            return
        self._start_tray()
        self.root.withdraw()
        self.log('已最小化到系统托盘，右键托盘图标可「显示窗口」或「退出」')

    def _start_tray(self):
        if self.tray is not None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem('切换网关', self._tray_click_gateway, default=True),
                pystray.MenuItem('显示/隐藏窗口', self._tray_toggle_window),
                pystray.MenuItem('退出', self._tray_quit),
            )
            self.tray = pystray.Icon('gateway_switch', make_tray_icon(),
                                     WINDOW_TITLE, menu)
            self._tray_icon_red = False
            self._tray_icon_variant = None
            self.tray.run_detached()
            threading.Timer(0.3, self._tray_make_visible).start()
            threading.Timer(1.5, self._tray_check_visible).start()
            # 托盘就绪后同步一次图标颜色（当前网关若为 B 则按谷歌连通显示红/绿）
            threading.Timer(0.8, lambda: self.root.after(0, self._update_tray_icon)).start()
        except Exception as e:
            self.log('托盘启动失败：%s' % e)
            self.tray = None
            self.root.deiconify()

    def _tray_make_visible(self):
        try:
            if self.tray is not None:
                self.tray.visible = True
        except Exception:
            pass

    def _tray_check_visible(self):
        try:
            t = self.tray
            if t is None:
                return
            if not t.visible:
                self.log('托盘图标未能显示，恢复主窗口')
                self.root.after(0, self._show_window)
        except Exception:
            pass

    def _tray_show(self, icon, item):
        self.root.after(0, self._show_window)

    def _tray_click_gateway(self, icon, item):
        """托盘左键单击（菜单默认项回调）：先挂起判定是否为双击。
        双击 -> 显示/隐藏主窗口；单击 -> 直接切换网关。"""
        try:
            self.root.after(0, self._tray_on_lclick)
        except Exception:
            pass

    def _tray_on_lclick(self):
        """左键单击事件（主线程）：双击判定 + 单击切换网关。"""
        try:
            dbl_ms = int(ctypes.windll.user32.GetDoubleClickTime())
        except Exception:
            dbl_ms = 500
        now = time.time()
        if (getattr(self, '_tray_click_ts', 0)
                and (now - self._tray_click_ts) * 1000.0 <= dbl_ms):
            # 判定为双击：取消挂起的单击切换，改为显示/隐藏主窗口
            self._tray_click_ts = 0
            if getattr(self, '_tray_click_after', None):
                try:
                    self.root.after_cancel(self._tray_click_after)
                except Exception:
                    pass
                self._tray_click_after = None
            self._tray_toggle_window()
            return
        # 记录本次单击，延迟到双击窗口结束后再执行切换
        self._tray_click_ts = now
        self._tray_click_after = self.root.after(
            dbl_ms + 80, self._tray_fire_switch)

    def _tray_fire_switch(self):
        """单击确认：执行网关切换。"""
        self._tray_click_after = None
        self._tray_click_ts = 0
        self._toggle_gateway()

    def _tray_toggle_window(self, icon=None, item=None):
        """显示/隐藏主窗口（托盘左键双击或右键菜单项触发）。"""
        try:
            if self.root.winfo_viewable():
                self.root.withdraw()
            else:
                self._show_window()
        except Exception:
            self._show_window()

    def _update_tray_icon(self):
        """按当前网关+谷歌连通同步托盘图标：
        网关 B（如 192.168.2.2）且谷歌不通 -> 红色地球；
        网关 B 且谷歌通                -> 绿色地球；
        网关 A / 其他                   -> 灰色地球（icon_gray.ico 同款）。"""
        try:
            if self.tray is None:
                return
            variant = 'gray'
            if self._last_gw == self._target_gw('b'):
                try:
                    google_ok = self._site_delay.get(3) is not None
                except Exception:
                    google_ok = False
                variant = 'green' if google_ok else 'red'
            if getattr(self, '_tray_icon_variant', None) == variant:
                return
            self._tray_icon_variant = variant
            img = make_tray_icon(64, variant=variant)
            self.tray.icon = img
            # 强制刷新托盘缓存（设置 icon 后翻转 visible 触发 NIM_MODIFY）
            try:
                self.tray.visible = False
                self.tray.visible = True
            except Exception:
                pass
        except Exception:
            pass

    def _show_window(self):
        self.root.deiconify()
        hwnd = self._hwnd()
        if hwnd:
            try:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            except Exception:
                pass
        self.root.lift()
        self.root.focus_force()

    def _tray_quit(self, icon, item):
        try:
            icon.stop()
        except Exception:
            pass
        self.root.after(0, self._quit)

    def _quit(self):
        try:
            self._probe_stop = True
            self._probe_event.set()
            self._probe_pool.shutdown(wait=False)
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.root.destroy()


_BUNDLED_FONTS = ('HartwellAlt-Bold.ttf', 'fa-solid-900.ttf')


def _register_bundled_font():
    """注册随包分发的字体（FR_PRIVATE 仅对当前进程生效，不污染系统字体库）。"""
    gdi32 = ctypes.windll.gdi32
    FR_PRIVATE = 0x10
    ok = True
    for name in _BUNDLED_FONTS:
        try:
            path = resource_path(name)
            if not os.path.isfile(path):
                ok = False
                continue
            n = gdi32.AddFontResourceExW(path, FR_PRIVATE, 0)
            if n == 0:
                n = gdi32.AddFontResourceW(path)
            if n <= 0:
                ok = False
        except Exception:
            ok = False
    return ok


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate_self():
    """非管理员运行时以 UAC 提权重启（源码/exe 通用）。返回 True 表示已发起提权。
    静默模式下向提权重启进程透传静默参数，避免提权后窗口弹出。"""
    try:
        silent_arg = ''
        for a in sys.argv[1:]:
            if a.lower() in SILENT_FLAGS:
                silent_arg = a
                break
        if getattr(sys, 'frozen', False):
            r = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', sys.executable, silent_arg, None, 1)
        else:
            r = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', sys.executable,
                '"%s"%s' % (os.path.abspath(__file__),
                            (' ' + silent_arg) if silent_arg else ''), None, 1)
        return r > 32
    except Exception:
        return False


def main():
    if sys.platform != 'win32':
        print('本程序仅支持 Windows。')
        sys.exit(1)
    # 修改默认路由需要管理员权限：非管理员先 UAC 提权重启
    if not is_admin():
        if not _elevate_self():
            try:
                import tkinter.messagebox as _mb
                _mb.showwarning(WINDOW_TITLE, '需要管理员权限才能修改默认路由，请以管理员身份运行。')
            except Exception:
                pass
        sys.exit(0)
    _register_bundled_font()
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    if not _acquire_single_instance():
        if IS_SILENT:
            # 静默模式下已运行实例：不打扰、直接退出
            sys.exit(0)
        _ok = _activate_existing_window()
        if _ok:
            sys.exit(0)
        try:
            import tkinter.messagebox as _mb
            _mb.showwarning(WINDOW_TITLE, '程序已在运行，请查看已打开的窗口。')
        except Exception:
            pass
        sys.exit(0)
    root = tk.Tk()
    if IS_SILENT:
        # 静默启动：先隐藏主窗口，仅保留托盘常驻
        root.withdraw()
    app = MarvisUI(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    # 启动即创建托盘图标（常驻），保证托盘切换/变红/双击交互始终可用
    try:
        if _HAS_TRAY:
            app._start_tray()
    except Exception:
        pass
    root.mainloop()


if __name__ == '__main__':
    if sys.platform != 'win32':
        print('本程序仅支持 Windows。')
        sys.exit(1)
    main()
