#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kindle (KOReader dropbear) SSH 客户端封装。

⚠️ 本文件是从 kwp3_tools/script/kindle_ssh.py 复制进来的副本。
kwp_ui 要能被其他人独立克隆运行，不能依赖同级的 kwp3_tools 目录。
上游若有改动，同步方式（在 kwp_ui 目录下执行）：
    cp ../kwp3_tools/script/kindle_ssh.py app/kindle_ssh.py
然后重新把这段说明补回来。

KOReader v2024.11 的 dropbear 有两个坑，本脚本已内置规避：
  1) ed25519 主机密钥路径错误 -> 强制 ecdsa/rsa，否则握手直接断
  2) 空密码在 OpenSSH BatchMode 下不被尝试 -> 这里显式传 password=''

用法：
    python script/kindle_ssh.py                       # 默认跑一组状态命令
    python script/kindle_ssh.py "df -h"
    python script/kindle_ssh.py --host 192.168.31.234 --port 2222 "ls /mnt/us"

其它：
    python script/kindle_ssh.py --put 本地文件 /mnt/us/documents/
    python script/kindle_ssh.py --get /mnt/us/documents/x.sdr/metadata.epub.lua .

注意（Git Bash / MSYS 环境）：
    命令行上的远端绝对路径会被 MSYS 自动改写成 Windows 路径
    （/mnt/us/... -> <Git安装目录>/mnt/us/...），表现为远端报
    FileNotFoundError 且本地留下 0 字节文件。传 --put/--get 时请加：
        MSYS2_ARG_CONV_EXCL="*" python script/kindle_ssh.py --get /mnt/us/... .
    普通命令字符串不受影响（只改写「以 / 开头」的独立参数，命令串以 ls/cat
    这类词开头，不会被误伤）。
"""
import argparse
import sys

import paramiko

DEFAULT_HOST = "192.168.31.234"
DEFAULT_PORT = 2222
DEFAULT_CMD = "date; uname -a; uptime"


def connect(host, port, timeout=15):
    t = paramiko.Transport((host, port))
    t.banner_timeout = timeout
    so = t.get_security_options()
    # 避开 ed25519 主机密钥缺陷（paramiko 5.x 已移除 ssh-rsa，传了会报 unknown cipher）
    so.key_types = ("ecdsa-sha2-nistp256", "rsa-sha2-256")
    so.ciphers = ("aes128-ctr", "aes256-ctr")
    # Transport.connect() 没有 timeout 参数，超时靠 banner_timeout
    t.connect(username="root", password="")
    return t


def run(host, port, cmd, timeout=30):
    t = connect(host, port)
    try:
        ch = t.open_session()
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        out = ch.makefile("rb").read().decode("utf-8", "replace")
        err = ch.makefile_stderr("rb").read().decode("utf-8", "replace")
        code = ch.recv_exit_status()
        return out, err, code
    finally:
        t.close()


def sftp_put(host, port, local, remote):
    t = connect(host, port)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.put(local, remote)
        print("已上传 {} -> {}".format(local, remote))
        sftp.close()
    finally:
        t.close()


def sftp_get(host, port, remote, local):
    t = connect(host, port)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.get(remote, local)
        print("已下载 {} -> {}".format(remote, local))
        sftp.close()
    finally:
        t.close()


def main():
    ap = argparse.ArgumentParser(description="Kindle SSH 客户端（KOReader dropbear）")
    ap.add_argument("command", nargs="*", help="要执行的远程命令，默认跑状态检查")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    args = ap.parse_args()

    try:
        if args.put:
            sftp_put(args.host, args.port, *args.put)
            return 0
        if args.get:
            sftp_get(args.host, args.port, *args.get)
            return 0
        cmd = " ".join(args.command) if args.command else DEFAULT_CMD
        out, err, code = run(args.host, args.port, cmd)
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write(err)
        return code
    except paramiko.AuthenticationException:
        sys.exit("认证失败：确认 KOReader 里已勾选 Login without password 并重启服务")
    except Exception as e:
        sys.exit("{}: {}".format(type(e).__name__, e))


if __name__ == "__main__":
    sys.exit(main())
