# -*- coding: utf-8 -*-
"""GitHub 直连 github.com 被重置时的兜底：改走 api.github.com 的 Git 数据 API 推送。

用法: python scripts/git_push_via_api.py "commit message" file1 file2 ...
（file 均为相对仓库根的路径，已在本地提交中存在）
"""
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / ".tmp" / "gh_token.txt").read_text(encoding="utf-8").strip()
REPO = "YukinoCarol/suhu-time-metro"
API = f"https://api.github.com/repos/{REPO}"


def api(method, path, payload=None, binary=None):
    """带重试的 REST 调用，返回解析后的 JSON。"""
    url = f"{API}{path}"
    for attempt in range(1, 9):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("Authorization", f"token {TOKEN}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "push-via-api")
            if binary is not None:
                data = binary
                req.add_header("Content-Type", "application/json")
            else:
                data = None if payload is None else json.dumps(payload).encode()
                if data is not None:
                    req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, data=data, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
            print(f"  {method} {path} attempt {attempt}: HTTP {e.code} {body}",
                  flush=True)
            time.sleep(6)
        except Exception as e:
            print(f"  {method} {path} attempt {attempt}: {e}", flush=True)
            time.sleep(6)
    raise RuntimeError(f"API failed: {method} {path}")


def main():
    msg, files = sys.argv[1], sys.argv[2]
    files = files.split()

    ref = api("GET", "/git/refs/heads/main")
    base_commit = ref["object"]["sha"]
    base_tree = api("GET", f"/git/commits/{base_commit}")["tree"]["sha"]
    print(f"base commit: {base_commit}", flush=True)

    entries = []
    for rel in files:
        p = ROOT / rel
        content = base64.b64encode(p.read_bytes()).decode()
        blob = api("POST", "/git/blobs",
                   {"content": content, "encoding": "base64"})
        entries.append({"path": rel, "mode": "100644", "type": "blob",
                        "sha": blob["sha"]})
        print(f"blob: {rel} -> {blob['sha'][:10]}", flush=True)

    tree = api("POST", "/git/trees",
               {"base_tree": base_tree, "tree": entries})
    print(f"tree: {tree['sha'][:10]}", flush=True)

    commit = api("POST", "/git/commits",
                 {"message": msg, "tree": tree["sha"],
                  "parents": [base_commit]})
    print(f"commit: {commit['sha'][:10]}", flush=True)

    api("PATCH", "/git/refs/heads/main",
        {"sha": commit["sha"], "force": False})
    print("ref updated -> pushed via API", flush=True)


if __name__ == "__main__":
    main()
