"""把本地仓库推到 Hugging Face Space，触发 HF 自动构建。

用法：
    python push_hf.py --user NanXiang77 --token hf_xxxxxxxx --space trip-oracle
    python push_hf.py                 # 用上次保存的 data/hf.json

说明：
    - 令牌只用于这一次 git push 的 URL，不写进 .git/config，不落盘到仓库
    - 可选保存到 data/hf.json（已被 .gitignore 排除）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CONF = ROOT / "data" / "hf.json"
SPACE_URL = "https://huggingface.co/spaces/{user}/{space}"
REPO_URL = "https://{user}:{token}@huggingface.co/spaces/{user}/{space}"


def load_conf() -> dict:
    if CONF.exists():
        try:
            return json.loads(CONF.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), text=True, **kw)


def main() -> int:
    conf = load_conf()
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=conf.get("user", ""), help="HF 用户名")
    ap.add_argument("--space", default=conf.get("space", "trip-oracle"), help="Space 名")
    ap.add_argument("--token", default=conf.get("token", ""), help="HF write token (hf_...)")
    ap.add_argument("--save", action="store_true", help="把配置存到 data/hf.json")
    ap.add_argument("--force", action="store_true", default=True, help="强推（覆盖 HF 初始提交）")
    a = ap.parse_args()

    if not a.user or not a.token:
        print("[x] 缺用户名或令牌。用法：python push_hf.py --user 你的用户名 --token hf_xxx")
        return 2
    if not a.token.startswith("hf_"):
        print(f"[!] 令牌看起来不像 HF 的（应以 hf_ 开头），当前：{a.token[:6]}…")

    # 先把本地改动提交干净（数据库每天在变）
    st = run(["git", "status", "--porcelain"], capture_output=True)
    if st.stdout.strip():
        print("[i] 有未提交改动，先提交：")
        for line in st.stdout.strip().splitlines()[:8]:
            print("   ", line)
        run(["git", "add", "-A"])
        run(["git", "commit", "-q", "-m", "sync for HF Space deploy"])
        print("[ok] 已提交")

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                 capture_output=True).stdout.strip() or "main"
    print(f"[i] 推送分支 {branch} → {SPACE_URL.format(user=a.user, space=a.space)}")

    url = REPO_URL.format(user=a.user, token=a.token, space=a.space)
    r = run(["git", "push", url, f"{branch}:main", "--force"], capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip()[-1200:])

    if r.returncode != 0:
        if "401" in out or "authentication" in out.lower():
            print("\n[x] 认证失败：确认 token 类型是 Write，且用户名正确。")
        elif "404" in out:
            print("\n[x] 404：Space 不存在。先去 https://huggingface.co/new-space 建一个（SDK 选 Docker/Blank）。")
        else:
            print("\n[x] 推送失败，看上面的报错。")
        return 1

    if a.save:
        CONF.parent.mkdir(parents=True, exist_ok=True)
        CONF.write_text(json.dumps({"user": a.user, "space": a.space, "token": a.token},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] 配置已存到 {CONF}（.gitignore 已排除）")

    host = f"{a.user.lower()}-{a.space.lower()}.hf.space"
    print("\n" + "=" * 60)
    print("推送完成，HF 正在构建（3~5 分钟）。构建好之后：")
    print(f"  页面     https://huggingface.co/spaces/{a.user}/{a.space}")
    print(f"  健康检查 https://{host}/health")
    print(f"  MCP 地址 https://{host}/mcp")
    print("=" * 60)
    print("等它变成 Running，用这条验证：")
    print(f'  curl -s https://{host}/health')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
