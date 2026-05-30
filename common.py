"""共用模块：环境变量、AI客户端、git同步、TG发送、链接抓取"""
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime

import pytz
import requests
from bs4 import BeautifulSoup
from openai import OpenAI


def load_env():
    """加载.env"""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_BASE_URL = os.environ["OPENAI_BASE_URL"]
MODEL_FAST = os.environ.get("MODEL_FAST", "gemini-2.5-flash")
MODEL_SMART = os.environ.get("MODEL_SMART", "gemini-2.5-pro")
VAULT = Path(os.environ["VAULT_PATH"])

CN_TZ = pytz.timezone("Asia/Shanghai")


def now_cn():
    return datetime.now(CN_TZ)


def today_str():
    return now_cn().strftime("%Y-%m-%d")


def now_str():
    return now_cn().strftime("%Y-%m-%d %H:%M:%S")


_client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


def ai_chat(prompt: str, model: str = None, system: str = None, max_tokens: int = 4000) -> str:
    """调用AI"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    resp = _client.chat.completions.create(
        model=model or MODEL_FAST,
        messages=msgs,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


def ai_chat_audio(prompt: str, audio_bytes: bytes, audio_format: str = "ogg",
                  model: str = None, max_tokens: int = 3000) -> str:
    """传音频给多模态模型（Gemini），返回文本响应。直接用 requests 走 OpenAI 兼容接口。"""
    import base64
    b64 = base64.b64encode(audio_bytes).decode()
    body = {
        "model": model or MODEL_FAST,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "audio_url",
                 "audio_url": {"url": f"data:audio/{audio_format};base64,{b64}"}},
            ],
        }],
        "max_tokens": max_tokens,
    }
    r = requests.post(
        f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def tg_send(text: str, chat_id: str = None, parse_mode: str = None) -> bool:
    """发送TG消息"""
    chat_id = chat_id or TG_CHAT_ID
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        for ch in chunks:
            data["text"] = ch
            r = requests.post(url, json=data, timeout=30)
            if not r.ok:
                print(f"TG发送失败: {r.text}")
                return False
        return True
    except Exception as e:
        print(f"TG异常: {e}")
        return False


def git_pull():
    """拉取最新vault"""
    subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=False, capture_output=True)


def git_commit_push(message: str) -> bool:
    """提交并推送"""
    try:
        subprocess.run(["git", "add", "-A"], cwd=VAULT, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=VAULT, capture_output=True
        )
        if result.returncode == 0:
            return True
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=VAULT, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"git失败: {e}")
        return False


def safe_filename(s: str, max_len: int = 80) -> str:
    """生成安全文件名"""
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", s)
    s = s.strip().strip(".")
    return s[:max_len] or "untitled"


def write_note(folder: str, filename: str, content: str) -> Path:
    """写入笔记到vault"""
    target_dir = VAULT / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    fp = target_dir / f"{filename}.md"
    fp.write_text(content, encoding="utf-8")
    return fp


URL_RE = re.compile(r'https?://[^\s）)】]+')


def extract_urls(text: str) -> list:
    return URL_RE.findall(text or "")


def fetch_url(url: str, timeout: int = 20) -> str:
    """抓取网页正文文本"""
    try:
        is_wechat = "mp.weixin.qq.com" in url
        if is_wechat:
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                              "Mobile/15E148 MicroMessenger/8.0.49(0x18003130) "
                              "NetType/WIFI Language/zh_CN",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://mp.weixin.qq.com/",
            }
        else:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "iframe"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""

        if is_wechat:
            if "环境异常" in r.text or "完成验证" in r.text:
                return f"抓取失败: 公众号反爬验证（VPS IP被识别），需手动复制正文"
            author_el = soup.select_one("#js_name") or soup.select_one(".rich_media_meta_text")
            author = author_el.get_text(strip=True) if author_el else ""
            content_el = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
            if content_el:
                body = content_el.get_text(separator="\n", strip=True)
                body = re.sub(r"\n{3,}", "\n\n", body)
                return f"# {title}\n\n作者：{author}\n\n{body[:30000]}"

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return f"# {title}\n\n{text[:30000]}"
    except Exception as e:
        return f"抓取失败: {e}"


def fetch_tweet(url: str, timeout: int = 20) -> dict:
    """通过 fxtwitter API 抓 X/Twitter 推文（不用 token）"""
    m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url)
    if not m:
        return {}
    user, tid = m.group(1), m.group(2)
    api = f"https://api.fxtwitter.com/{user}/status/{tid}"
    try:
        r = requests.get(api, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            return {}
        tw = data.get("tweet", {})
        author = tw.get("author", {}) or {}
        media_list = (tw.get("media") or {}).get("all") or []
        media = [
            {"type": m.get("type"), "url": m.get("url")}
            for m in media_list if m.get("url")
        ]
        article = tw.get("article") or {}
        article_title = article.get("title", "") or ""
        article_preview = article.get("preview_text", "") or ""
        article_full = ""
        content = article.get("content")
        if isinstance(content, dict):
            blocks = content.get("blocks") or []
            article_full = "\n".join(
                b.get("text", "") for b in blocks if isinstance(b, dict)
            ).strip()
        text = tw.get("text", "") or ""
        if (not text or text.startswith("https://t.co/")) and (article_full or article_preview):
            head = f"【长推/Article】{article_title}\n\n" if article_title else ""
            text = head + (article_full or article_preview)
        return {
            "id": tid,
            "url": tw.get("url", url),
            "text": text,
            "article_title": article_title,
            "author_name": author.get("name", "") or "",
            "author_handle": author.get("screen_name", user) or user,
            "created_at": tw.get("created_at", "") or "",
            "likes": tw.get("likes", 0),
            "retweets": tw.get("retweets", 0),
            "replies": tw.get("replies", 0),
            "media": media,
            "lang": tw.get("lang", "") or "",
            "quote": (tw.get("quote") or {}).get("text", "") or "",
        }
    except Exception as e:
        print(f"fxtwitter 失败: {e}")
        return {}


def fetch_github_repo(url: str) -> dict:
    """通过GitHub API拿仓库元信息+README"""
    m = re.search(r"github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return {}
    owner, repo = m.group(1), m.group(2).rstrip(".git")
    api = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json"}
    try:
        info = requests.get(api, headers=headers, timeout=15).json()
        readme_resp = requests.get(f"{api}/readme",
                                    headers={**headers, "Accept": "application/vnd.github.raw"},
                                    timeout=15)
        readme = readme_resp.text if readme_resp.ok else ""
        return {
            "owner": owner,
            "repo": repo,
            "full_name": info.get("full_name", f"{owner}/{repo}"),
            "description": info.get("description", "") or "",
            "stars": info.get("stargazers_count", 0),
            "language": info.get("language", "") or "",
            "topics": info.get("topics", []) or [],
            "url": info.get("html_url", url),
            "readme": readme[:30000],
        }
    except Exception as e:
        print(f"GitHub API失败: {e}")
        return {"owner": owner, "repo": repo, "url": url, "readme": ""}
