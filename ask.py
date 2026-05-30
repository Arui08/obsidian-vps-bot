"""跟你的 Obsidian vault 对话：扫描相关笔记 → 拼上下文 → AI 综合回答"""
import re
from pathlib import Path

from common import VAULT, MODEL_SMART, MODEL_FAST, ai_chat


SKIP_DIRS = {".git", ".obsidian", ".trash"}
SKIP_TOP = {
    "随记", "客户信息", "工作内容", "近期备忘", "附件目录", "常用代码 项目记录",
}


def list_vault_notes() -> list:
    """枚举所有 md 笔记（跳过敏感目录与系统目录）"""
    notes = []
    for md in VAULT.rglob("*.md"):
        rel = md.relative_to(VAULT)
        parts = rel.parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if parts and parts[0] in SKIP_TOP:
            continue
        notes.append(md)
    return notes


def tokenize(text: str) -> set:
    """简易中英混合切词：字母连续段 + 单个汉字"""
    text = text.lower()
    words = set(re.findall(r"[a-z0-9]{2,}", text))
    for ch in text:
        if "一" <= ch <= "鿿":
            words.add(ch)
    return words


def score_note(query_tokens: set, note: Path) -> tuple:
    """根据 token 重叠度打分；返回 (score, content)"""
    try:
        content = note.read_text(encoding="utf-8")
    except Exception:
        return 0, ""
    note_tokens = tokenize(content + " " + note.stem)
    overlap = query_tokens & note_tokens
    chinese_overlap = sum(1 for t in overlap if "一" <= t <= "鿿")
    word_overlap = len(overlap) - chinese_overlap
    score = word_overlap * 3 + chinese_overlap
    if any(t in note.stem.lower() for t in query_tokens if len(t) >= 2):
        score += 5
    return score, content


def find_relevant(question: str, top_k: int = 8) -> list:
    """打分挑出最相关的 top_k 篇笔记"""
    qt = tokenize(question)
    if not qt:
        return []
    scored = []
    for n in list_vault_notes():
        s, c = score_note(qt, n)
        if s > 0:
            scored.append((s, n, c))
    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


def answer(question: str) -> str:
    hits = find_relevant(question, top_k=8)
    if not hits:
        return ai_chat(
            f"用户问：{question}\n\n"
            "我在他的 Obsidian 笔记里没找到相关内容，请直接回答这个问题，"
            "并明确告诉用户'笔记里没有相关内容'。",
            model=MODEL_FAST,
        )

    chunks = []
    for s, note, content in hits:
        rel = note.relative_to(VAULT)
        snippet = content[:3000]
        chunks.append(f"### {rel} (相关度{s})\n{snippet}\n")

    context = "\n\n---\n\n".join(chunks)[:60000]

    prompt = f"""你是用户的私人知识库助手。下面是从他 Obsidian vault 里检索出来的相关笔记，请基于这些笔记回答他的问题。

要求：
1. 直接回答，不要客套
2. 引用具体笔记时用 [[笔记标题]] 格式（去掉 .md 和路径）
3. 如果笔记内容能回答，就基于笔记综合回答；如果不够充分，先用笔记内容回答，再补充你的判断（明确标注哪些是笔记里的、哪些是你的补充）
4. 如果笔记内容跟问题完全无关，直接说"笔记里没找到相关内容"，然后基于常识简答
5. 回答用中文，结构清晰，可以用 markdown

用户的问题：
{question}

检索到的笔记：
{context}
"""
    return ai_chat(prompt, model=MODEL_SMART, max_tokens=3000)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "我最近在关注什么主题？"
    print(answer(q))
