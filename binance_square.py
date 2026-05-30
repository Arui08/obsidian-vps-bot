"""币安广场发帖（基于官方 binance-skills-hub / square-post）

API 来源：scripts/lib.mjs
- 端点：https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add
- 鉴权：header X-Square-OpenAPI-Key
- 业务成功：JSON 返回 code == "000000"
- 504 + /content/add：视为 success_without_post_id
"""
import os
import requests

# 复用 common.load_env()，把 .env 加进 os.environ
from common import OPENAI_API_KEY  # noqa: F401  确保 common 被 import 时执行 load_env

BASE_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"

HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "clienttype": "binanceSkill",
}


class SquareError(Exception):
    pass


def _api_key() -> str:
    return os.environ.get("BINANCE_SQUARE_OPENAPI_KEY", "")


def _post(endpoint: str, body: dict, timeout: int = 30) -> dict:
    key = _api_key()
    if not key:
        raise SquareError("BINANCE_SQUARE_OPENAPI_KEY 未配置")
    headers = {**HEADERS_TEMPLATE, "X-Square-OpenAPI-Key": key}
    url = f"{BASE_V1}{endpoint}"
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        raise SquareError(f"网络异常: {e}") from e

    if r.status_code == 504 and endpoint == "/content/add":
        return {"id": None, "shareLink": None, "publishStatus": "success_without_post_id"}

    if not r.ok:
        raise SquareError(f"HTTP {r.status_code}: {r.text[:300]}")

    try:
        data = r.json()
    except ValueError:
        raise SquareError(f"非 JSON 响应: {r.text[:300]}")

    if data.get("code") != "000000":
        raise SquareError(f"API code={data.get('code')} msg={data.get('message')}")

    return data.get("data") or {}


def publish_text(text: str, title: str = "") -> dict:
    """
    发布到币安广场。
    - 没传 title：contentType=1（短文本帖）
    - 传了 title：contentType=2（长文章帖）
    返回 {"id":..., "shareLink":..., "publishStatus":...}
    """
    body = {
        "contentType": 2 if title else 1,
        "bodyTextOnly": text,
    }
    if title:
        body["title"] = title
    return _post("/content/add", body)


if __name__ == "__main__":
    import sys
    text = " ".join(sys.argv[1:]) or "测试一下币安广场发帖接口"
    res = publish_text(text)
    print(res)
