"""core/github_storage.py — 通过 GitHub Contents API 持久化 JSON 文件

用途
----
Streamlit Community Cloud 的文件系统是临时的（重启后丢失），
该模块在用户保存数据时同步写回 GitHub 仓库，实现跨会话持久化。

使用条件
--------
在 Streamlit Cloud → App Settings → Secrets 中添加：
    GITHUB_TOKEN = "ghp_xxxxxxxxxxxx"
    GITHUB_REPO  = "rickywsb/xiaozhu_xiaoyan"

本地运行时不需要配置（会自动跳过 GitHub 同步，仅写本地文件）。

权限要求
--------
Personal Access Token (classic) 需要 `repo` scope 权限。
或 Fine-grained PAT 需要 Contents: Read and write。
"""

import base64
import json
from pathlib import Path
from typing import Any


def _needs_requests() -> None:
    try:
        import requests  # noqa: F401
    except ImportError as e:
        raise ImportError("请在 requirements.txt 中添加 requests") from e


def get_file_sha(remote_path: str, token: str, repo: str) -> str | None:
    """获取 GitHub 上指定文件的当前 SHA（PUT 时必须提供）。"""
    _needs_requests()
    import requests

    url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("sha")
    except Exception:
        pass
    return None


def save_json_to_github(
    local_path: str | Path,
    remote_path: str,
    commit_msg: str,
    *,
    token: str,
    repo: str,
) -> tuple[bool, str]:
    """
    将本地 JSON 文件通过 GitHub Contents API 写回仓库。

    Parameters
    ----------
    local_path  : 本地文件路径
    remote_path : 仓库内相对路径，如 "data/portfolio.json"
    commit_msg  : git commit 消息
    token       : GitHub Personal Access Token
    repo        : "owner/repo" 格式，如 "rickywsb/xiaozhu_xiaoyan"

    Returns
    -------
    (success: bool, message: str)
    """
    _needs_requests()
    import requests

    try:
        content_bytes = Path(local_path).read_bytes()
        encoded = base64.b64encode(content_bytes).decode("ascii")
        sha = get_file_sha(remote_path, token, repo)

        url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        payload: dict[str, Any] = {
            "message": commit_msg,
            "content": encoded,
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, json=payload, headers=headers, timeout=15)

        if resp.status_code in (200, 201):
            return True, "已同步到 GitHub ✅"
        else:
            err = resp.json().get("message", resp.status_code)
            return False, f"GitHub API 错误：{err}"

    except Exception as e:
        return False, f"同步失败：{e}"


def _get_secrets() -> tuple[str | None, str]:
    """
    从 Streamlit Secrets 读取 GitHub 配置。
    本地环境没有 Secrets 时返回 (None, default_repo)。
    """
    try:
        import streamlit as st
        token = st.secrets.get("GITHUB_TOKEN", None)
        repo  = st.secrets.get("GITHUB_REPO", "rickywsb/xiaozhu_xiaoyan")
        return token, repo
    except Exception:
        return None, "rickywsb/xiaozhu_xiaoyan"


def sync_to_github(
    local_path: str | Path,
    remote_path: str,
    commit_msg: str,
) -> tuple[bool, str]:
    """
    便捷封装：自动从 st.secrets 获取 token/repo，失败时静默返回。
    在 Streamlit 页面中直接调用即可。

    Returns (success, message):
      success=None  → 未配置 token，跳过（本地模式）
      success=True  → 同步成功
      success=False → 同步失败
    """
    token, repo = _get_secrets()
    if not token:
        return None, "未配置 GITHUB_TOKEN，仅保存到本地"

    return save_json_to_github(
        local_path, remote_path, commit_msg,
        token=token, repo=repo,
    )
