#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备访问层。

SSH 细节（dropbear 主机密钥缺陷、空密码提交）全部交给
app/kindle_ssh.py 处理，本模块只加连接管理、错误归一化和「人话」结果。

注意：kindle_ssh.py 是从 kwp3_tools/script/ 复制进来的副本，
这份仓库需要能被其他人单独克隆后直接运行，不能依赖同级目录。
"""
import asyncio
import os
import socket
import stat as statmod
import time
from contextlib import contextmanager
from pathlib import Path

import paramiko

from . import config
from . import kindle_ssh


# /mnt/us 下不允许删除的目录，误删会 brick 掉系统组件
PROTECTED = (
    "/mnt/us/system",
    "/mnt/us/koreader",
    "/mnt/us/extensions",
    "/mnt/us/documents",
    "/mnt/us/fonts",
    "/mnt/us",
)

SERIAL_MODEL = {
    "G090": "Kindle Paperwhite 3",
    "G091": "Kindle Paperwhite 3",
    "G000": "Kindle Paperwhite 2",
    "G090G": "Kindle Paperwhite 3",
}


class DeviceError(RuntimeError):
    """连不上 / 命令失败 / 路径非法，统一从这里抛。"""


def _friendly(exc):
    msg = str(exc)
    if isinstance(exc, paramiko.AuthenticationException):
        return "认证失败：请在 KOReader 里勾选 Login without password 后重启 SSH 服务"
    # 10060 = WSAETIMEDOUT，Windows 下 errno 可能为空、错因在 winerror 里
    codes = (getattr(exc, "errno", None), getattr(exc, "winerror", None))
    if isinstance(exc, (socket.timeout, TimeoutError)) or 10060 in codes or 110 in codes:
        return "连接超时，设备可能已休眠、KOReader 已关闭或 IP 变了"
    if "timed out" in msg.lower():
        return "连接超时，设备可能已休眠、KOReader 已关闭或 IP 变了"
    if 10061 in codes or "refused" in msg.lower():
        return "端口不通，KOReader 的 SSH 服务可能已关闭"
    return msg or exc.__class__.__name__


class Kindle:
    def __init__(self, host=None, port=None):
        cfg = config.load()
        self.host = host or cfg["host"]
        self.port = int(port or cfg["port"])

    def _probe_tcp(self, timeout=3):
        """先做一次快速的 TCP 探活。

        设备关机时 Windows 的 SYN 重试要 21 秒才报错，而 paramiko 的
        banner_timeout 只管握手后的阶段，管不到这一步。先探一下能把
        离线判断压到 timeout 秒内，避免界面长时间转圈。
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError as exc:
            raise DeviceError(_friendly(exc)) from exc

    def _connect(self, timeout=8):
        self._probe_tcp(min(timeout, 3))
        try:
            return kindle_ssh.connect(self.host, self.port, timeout)
        except Exception as exc:  # 统一成 DeviceError，前端只看 message
            raise DeviceError(_friendly(exc)) from exc

    # ---------- 命令 ----------

    def run(self, cmd, timeout=30):
        transport = self._connect()
        try:
            chan = transport.open_session()
            chan.settimeout(timeout)
            chan.exec_command(cmd)
            out = chan.makefile("rb").read().decode("utf-8", "replace")
            err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
            code = chan.recv_exit_status()
            return {"out": out, "err": err, "code": code}
        except Exception as exc:
            raise DeviceError(_friendly(exc)) from exc
        finally:
            transport.close()

    # ---------- SFTP ----------

    @contextmanager
    def sftp(self, timeout=8):
        transport = self._connect(timeout=timeout)
        try:
            yield paramiko.SFTPClient.from_transport(transport)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(_friendly(exc)) from exc
        finally:
            transport.close()

    def listdir(self, path):
        path = safe_path(path)
        try:
            with self.sftp() as sftp:
                entries = sftp.listdir_attr(path)
        except FileNotFoundError:
            raise DeviceError("目录不存在：{}".format(path))
        rows = []
        for e in entries:
            is_dir = statmod.S_ISDIR(e.st_mode or 0)
            rows.append({
                "name": e.filename,
                "is_dir": is_dir,
                "size": 0 if is_dir else (e.st_size or 0),
                "mtime": int(e.st_mtime or 0),
                "ext": "" if is_dir else os.path.splitext(e.filename)[1].lower().lstrip("."),
            })
        rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
        return rows

    def download(self, remote, local, timeout=8):
        remote = safe_path(remote)
        try:
            with self.sftp(timeout=timeout) as sftp:
                sftp.get(remote, str(local))
        except FileNotFoundError:
            raise DeviceError("文件不存在：{}".format(remote))
        return Path(local)

    def upload(self, fileobj, remote):
        remote = safe_path(remote)
        try:
            with self.sftp() as sftp:
                sftp.putfo(fileobj, remote, confirm=True)
        except Exception as exc:
            raise DeviceError(_friendly(exc)) from exc
        return remote

    def mkdir(self, remote):
        remote = safe_path(remote)
        with self.sftp() as sftp:
            try:
                sftp.mkdir(remote)
            except IOError:
                raise DeviceError("创建失败，同名目录或文件已存在")

    def remove(self, remote):
        remote = safe_path(remote)
        if remote.rstrip("/") in PROTECTED:
            raise DeviceError("{} 是受保护目录，拒绝删除".format(remote))
        with self.sftp() as sftp:
            self._remove(sftp, remote)

    def _remove(self, sftp, path, depth=0):
        if depth > 12:
            raise DeviceError("目录层级过深，已中止")
        try:
            attrs = sftp.listdir_attr(path)
        except IOError:
            sftp.remove(path)
            return
        for a in attrs:
            child = "{}/{}".format(path.rstrip("/"), a.filename)
            if statmod.S_ISDIR(a.st_mode or 0):
                self._remove(sftp, child, depth + 1)
            else:
                sftp.remove(child)
        sftp.rmdir(path)

    # ---------- 状态 ----------

    STATUS_CMD = r"""
echo '@@uptime'; cat /proc/uptime
echo '@@batt'; cat /sys/class/power_supply/max77696-battery/capacity 2>/dev/null; \
cat /sys/class/power_supply/max77696-battery/status 2>/dev/null
echo '@@kindle'; cat /etc/prettyversion.txt 2>/dev/null
echo '@@kernel'; uname -r
echo '@@ko'; cat /mnt/us/koreader/git-rev 2>/dev/null
echo '@@mem'; grep -E 'MemTotal|MemAvailable|MemFree|Buffers|^Cached' /proc/meminfo 2>/dev/null
echo '@@df'; df -k /mnt/us 2>/dev/null | tail -1
echo '@@usid'; cat /proc/usid 2>/dev/null; echo
echo '@@epoch'; date +%s; echo
"""

    def status(self):
        started = time.time()
        res = self.run(self.STATUS_CMD.strip(), timeout=25)
        latency_ms = int((time.time() - started) * 1000)
        blocks = {}
        key = None
        for line in res["out"].splitlines():
            if line.startswith("@@"):
                key = line[2:].strip()
                blocks[key] = []
            elif key:
                blocks[key].append(line)

        def first(name, default=""):
            vals = blocks.get(name, [])
            return vals[0].strip() if vals else default

        def lines(name):
            return [v.strip() for v in blocks.get(name, []) if v.strip()]

        uptime = 0.0
        try:
            uptime = float(first("uptime", "0").split()[0])
        except (ValueError, IndexError):
            pass

        battery, charging = None, None
        batt_lines = lines("batt")
        if batt_lines:
            try:
                battery = int(batt_lines[0])
            except ValueError:
                battery = None
            if len(batt_lines) > 1:
                charging = batt_lines[1]

        mem = {}
        for line in lines("mem"):
            parts = line.split(":")
            if len(parts) == 2:
                mem[parts[0].strip()] = int(parts[1].strip().split()[0]) // 1024
        # 3.0.35 内核不提供 MemAvailable，按 Linux 的估算口径自己拼：
        # 空闲 + Buffers + Cached，这部分缓存随时可回收，才算「真正可用」
        if "MemAvailable" in mem:
            available = mem["MemAvailable"]
        else:
            available = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)

        storage = None
        df_line = first("df")
        df_parts = df_line.split()
        if len(df_parts) >= 6:
            try:
                storage = {
                    "total_mb": int(df_parts[1]) // 1024,
                    "used_mb": int(df_parts[2]) // 1024,
                    "free_mb": int(df_parts[3]) // 1024,
                    "percent": int(df_parts[4].rstrip("%")),
                }
            except ValueError:
                storage = None

        serial = first("usid")
        model = "Kindle"
        for prefix, name in SERIAL_MODEL.items():
            if serial.startswith(prefix):
                model = name
                break

        skew = None
        try:
            skew = int(time.time()) - int(first("epoch", "0"))
        except ValueError:
            pass

        return {
            "host": self.host,
            "port": self.port,
            "online": True,
            "latency_ms": latency_ms,
            "model": model,
            "serial": serial,
            "firmware": first("kindle", "未知").replace("(~~otaVersion~~)", "").strip(),
            "kernel": first("kernel"),
            "koreader": first("ko", "未安装"),
            "uptime_sec": int(uptime),
            "battery": battery,
            "charging": charging,
            "memory": {
                "total_mb": mem.get("MemTotal", 0),
                "free_mb": mem.get("MemFree", 0),
                "available_mb": available,
            },
            "storage": storage,
            "clock_skew_sec": skew,
        }


# ---------- 路径防护 ----------

def safe_path(path):
    if not path:
        raise DeviceError("路径为空")
    p = "/" + str(path).replace("\\", "/").strip("/")
    if not (p == "/mnt/us" or p.startswith("/mnt/us/")):
        raise DeviceError("只允许访问 /mnt/us 下的路径")
    if ".." in p.split("/"):
        raise DeviceError("路径非法")
    return p


# ---------- 局域网扫描 ----------

def _decode_banner(raw):
    """SSH 识别串一般是 ASCII，但个别设备会带本地编码的设备名。"""
    raw = raw.strip()
    if not raw:
        return ""
    text = ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _software_of(banner):
    """SSH-2.0-dropbear_2026.92 (KOReader) -> dropbear_2026.92 (KOReader)"""
    parts = banner.split("-", 2)
    return parts[2].strip() if len(parts) == 3 else banner


async def _probe(host, port, timeout):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
    except Exception:
        return None
    line = b""
    try:
        # 只读第一行：banner 之后紧跟的 SSH 二进制密钥交换包，
        # 一起读进来按文本解码就会变成一堆乱码
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    text = _decode_banner(line)
    # 只认 SSH 服务，避免把开放 2222 的别的服务当成 Kindle
    if not text.startswith("SSH-"):
        return None
    return {"host": host, "banner": text, "software": _software_of(text)}


def _subnet_of(host):
    parts = host.split(".")
    if len(parts) != 4:
        return "192.168.31"
    return ".".join(parts[:3])


async def discover(subnet=None, port=2222, timeout=0.45):
    subnet = subnet or _subnet_of(config.load()["host"])
    sem = asyncio.Semaphore(128)

    async def one(i):
        async with sem:
            return await _probe("{}.{}".format(subnet, i), port, timeout)

    results = await asyncio.gather(*(one(i) for i in range(1, 255)))
    return [r for r in results if r]
