"""pages/4_Notes.py — 每周交易策略笔记"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.github_storage import sync_to_github

# ─── 常量 ─────────────────────────────────────────────────────────────────────
NOTES_DIR = config.NOTES_DIR
NOTES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TEMPLATE = """\
## 📈 市场观察

> 本周大盘 / 板块整体情绪：

---

## 🔍 重点持仓动态

| 股票 | 变化 | 备注 |
|------|------|------|
|      |      |      |

---

## 💡 操作策略

**计划买入：**

**计划减仓：**

**继续持有理由：**

---

## ⚠️ 风险提示

---

## 📝 其他记录
"""

TAG_OPTIONS = ["光模块", "存储", "半导体", "AI算力", "动量加速", "减仓", "加仓", "观察", "宏观", "风险"]


# ─── 存储工具 ──────────────────────────────────────────────────────────────────

def _note_path(note_id: str) -> Path:
    return NOTES_DIR / f"{note_id}.json"


def _list_notes() -> list[dict]:
    """返回所有笔记的 meta 列表，按日期倒序。"""
    notes = []
    for f in sorted(NOTES_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            notes.append({
                "id":       f.stem,
                "title":    data.get("title", f.stem),
                "date":     data.get("date_created", f.stem),
                "tags":     data.get("tags", []),
                "preview":  data.get("content", "")[:80].replace("\n", " "),
            })
        except Exception:
            pass
    return notes


def _load_note(note_id: str) -> dict | None:
    p = _note_path(note_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save_note(note_id: str, title: str, content: str, tags: list[str]) -> dict:
    existing = _load_note(note_id) or {}
    note = {
        "id":            note_id,
        "title":         title,
        "content":       content,
        "tags":          tags,
        "date_created":  existing.get("date_created", date.today().isoformat()),
        "date_modified": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _note_path(note_id).write_text(
        json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return note


def _delete_note(note_id: str):
    p = _note_path(note_id)
    if p.exists():
        p.unlink()


def _new_note_id() -> str:
    """默认用今天日期作为 ID，若已存在则加序号。"""
    base = date.today().isoformat()
    if not _note_path(base).exists():
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"
        if not _note_path(candidate).exists():
            return candidate
    return f"{base}-x"


# ─── 页面 ─────────────────────────────────────────────────────────────────────
st.title("📓 交易策略笔记")
st.caption("记录每周市场观察、持仓动态与操作计划。保存后自动同步到 GitHub。")

notes = _list_notes()

# ─ 侧栏：笔记列表 + 新建 ─────────────────────────────────────────────────────
with st.sidebar:
    st.subheader("📋 笔记列表")

    if st.button("➕ 新建笔记", type="primary", use_container_width=True):
        st.session_state["active_note_id"] = _new_note_id()
        st.session_state["note_mode"] = "edit"
        st.session_state["new_note"] = True
        st.rerun()

    st.divider()

    if not notes:
        st.caption("暂无笔记，点击「➕ 新建笔记」开始。")
    else:
        # 搜索过滤
        search = st.text_input("🔍 搜索", placeholder="标题或内容关键词…", label_visibility="collapsed")
        filtered = [n for n in notes if not search or
                    search.lower() in n["title"].lower() or
                    search.lower() in n["preview"].lower()]

        for n in filtered:
            is_active = st.session_state.get("active_note_id") == n["id"]
            label = f"{'▶ ' if is_active else ''}{n['date']}  {n['title']}"
            if n["tags"]:
                label += f"\n`{'` `'.join(n['tags'][:2])}`"
            if st.button(label, key=f"btn_{n['id']}", use_container_width=True):
                st.session_state["active_note_id"] = n["id"]
                st.session_state["note_mode"] = "view"
                st.session_state.pop("new_note", None)
                st.rerun()

# ─ 主区域 ────────────────────────────────────────────────────────────────────
active_id   = st.session_state.get("active_note_id")
note_mode   = st.session_state.get("note_mode", "view")
is_new_note = st.session_state.get("new_note", False)

if active_id is None and not notes:
    # 完全空白状态
    st.info("👈 点击左侧「➕ 新建笔记」创建第一篇策略记录。")
    st.stop()

if active_id is None and notes:
    # 默认显示最新笔记
    active_id = notes[0]["id"]
    st.session_state["active_note_id"] = active_id
    note_mode = "view"

# 加载当前笔记
current = _load_note(active_id) if not is_new_note else None
init_title   = current["title"]   if current else f"Week of {date.today().isoformat()}"
init_content = current["content"] if current else DEFAULT_TEMPLATE
init_tags    = current["tags"]    if current else []

# ── 顶栏：标题 + 操作按钮 ──────────────────────────────────────────────────────
col_title, col_btns = st.columns([6, 3])

with col_title:
    if note_mode == "edit":
        edit_title = st.text_input("标题", value=init_title, label_visibility="collapsed",
                                   placeholder="笔记标题…")
    else:
        st.subheader(init_title)
        if current:
            st.caption(f"📅 创建：{current.get('date_created','')}  |  ✏️ 最后修改：{current.get('date_modified','')}")

with col_btns:
    btn_cols = st.columns(3)
    with btn_cols[0]:
        if note_mode == "view":
            if st.button("✏️ 编辑", use_container_width=True):
                st.session_state["note_mode"] = "edit"
                st.rerun()
        else:
            if st.button("👁 预览", use_container_width=True):
                st.session_state["note_mode"] = "view"
                st.session_state.pop("new_note", None)
                st.rerun()
    with btn_cols[1]:
        save_clicked = st.button("💾 保存", type="primary", use_container_width=True)
    with btn_cols[2]:
        if not is_new_note and st.button("🗑 删除", use_container_width=True):
            st.session_state["confirm_delete"] = True

# 删除确认
if st.session_state.get("confirm_delete"):
    st.warning(f"确定删除「{init_title}」？此操作不可撤销。")
    dc1, dc2, _ = st.columns([1, 1, 4])
    with dc1:
        if st.button("✅ 确认删除", type="primary"):
            _delete_note(active_id)
            st.session_state.pop("active_note_id", None)
            st.session_state.pop("confirm_delete", None)
            st.success("已删除")
            st.rerun()
    with dc2:
        if st.button("取消"):
            st.session_state.pop("confirm_delete", None)
            st.rerun()

st.divider()

# ── 标签 ──────────────────────────────────────────────────────────────────────
if note_mode == "edit":
    edit_tags = st.multiselect(
        "🏷 标签", TAG_OPTIONS,
        default=[t for t in init_tags if t in TAG_OPTIONS],
        placeholder="选择或输入标签…",
    )
else:
    if init_tags:
        st.markdown("🏷 " + "  ".join(f"`{t}`" for t in init_tags))

# ── 正文 ──────────────────────────────────────────────────────────────────────
if note_mode == "edit":
    edit_content = st.text_area(
        "内容（支持 Markdown）",
        value=init_content,
        height=600,
        label_visibility="collapsed",
        placeholder="用 Markdown 记录你的策略…",
        key=f"content_{active_id}",
    )
else:
    st.markdown(init_content)

# ── 保存逻辑 ──────────────────────────────────────────────────────────────────
if save_clicked:
    if note_mode != "edit":
        # 预览模式没有编辑框，直接用原始内容
        edit_title   = init_title
        edit_content = init_content
        edit_tags    = init_tags

    saved = _save_note(active_id, edit_title, edit_content, edit_tags)

    # GitHub 同步
    remote = f"data/notes/{active_id}.json"
    ok, msg = sync_to_github(_note_path(active_id), remote, f"note: {edit_title}")

    if ok is True:
        st.success(f"✅ 「{edit_title}」已保存并同步到 GitHub")
    elif ok is False:
        st.warning(f"✅ 已保存到本地，GitHub 同步失败：{msg}")
    else:
        st.success(f"✅ 「{edit_title}」已保存")

    st.session_state["note_mode"] = "view"
    st.session_state.pop("new_note", None)
    st.rerun()
