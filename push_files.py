#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_files.py — 通用文件推送 GitHub（2026-10-26 v1.0）
复用 push_wb.py 已验证链路：镜像克隆 → 暂存任意文件 → credential helper 推送 → 回读校验

用法:
  WB_GITHUB_TOKEN=xxx python3 push_files.py docs/简报-2026-10-23.md daily_brief.py run_daily.sh
"""
import os
import subprocess
import sys
import tempfile
import shutil

REPO = "livsstock/xgl"
MIRRORS = ["https://ghfast.top", "https://gh-proxy.com", "https://ghproxy.net"]


def run(cmd, cwd=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def main():
    token = os.environ.get("WB_GITHUB_TOKEN", "")
    files = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not token:
        print("✗ 缺少 WB_GITHUB_TOKEN")
        return 1
    if not files:
        print("✗ 未指定要推送的文件")
        return 1

    workdir = tempfile.mkdtemp(prefix="xgl-push-")
    clone_url = None
    for m in MIRRORS:
        url = f"{m}/https://github.com/{REPO}.git"
        print(f"克隆 {m} ...")
        shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir)
        try:
            r = run(["git", "clone", "--depth", "5", url, workdir], timeout=180)
        except subprocess.TimeoutExpired:
            print(f"[WARN] {m} 克隆超时")
            continue
        if r.returncode == 0:
            clone_url = url
            break
    else:
        print("✗ 全部镜像克隆失败")
        return 2

    for f in files:
        src = os.path.abspath(f)
        if not os.path.exists(src):
            print(f"✗ 文件不存在: {f}")
            return 1
        dst = os.path.join(workdir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"已暂存 {f}")

    run(["git", "add", "-A"], cwd=workdir)
    r = run(["git", "-c", "user.name=WorkBuddy", "-c", "user.email=workbuddy@livsstock.local",
             "commit", "-m", f"📊 WB 推送: {', '.join(files)}"], cwd=workdir)
    if r.returncode != 0:
        print(f"✗ 提交失败: {r.stderr[-300:]}")
        return 3

    helper = f"!f(){{ echo username=livsstock; echo password={token}; }}; f"
    print("推送中 ...")
    r = run(["git", "-c", f"credential.helper={helper}", "push", clone_url, "HEAD:main"], cwd=workdir, timeout=300)
    if r.returncode != 0:
        print(f"✗ 推送失败: {r.stderr[-400:]}")
        return 3
    print("✓ 推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
