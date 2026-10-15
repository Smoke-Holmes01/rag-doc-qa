"""SiliconFlow-specific HTTP mappings, not a generic compatibility adapter.

Official endpoints and supported parameters are linked in README.
No retries, model substitution, or mock fallback.
"""
import json

import httpx

from app.config import Settings
from app.errors import AppError


def invalid_response() -> AppError:
    return AppError("MODEL_RESPONSE_INVALID", "模型响应字段或内容无效", 502)


async def post_json(client: httpx.AsyncClient, url: str, key: str,
                    payload: dict[str, object]) -> dict[str, object]:
    try:
        response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
    except httpx.TimeoutException:
        raise AppError("MODEL_TIMEOUT", "模型接口调用超时", 504) from None
    except httpx.HTTPError:
        raise AppError("MODEL_UPSTREAM_ERROR", "模型接口网络调用失败", 502) from None
    if response.status_code == 429:
        raise AppError("MODEL_RATE_LIMITED", "模型接口限流，请稍后重试")
    if response.status_code in {408, 504}:
        raise AppError("MODEL_TIMEOUT", "模型接口调用超时", 504)
    if not response.is_success:
        raise AppError("MODEL_UPSTREAM_ERROR", "模型接口返回错误，请检查授权、接口与参数", 502)
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise invalid_response() from None
    if not isinstance(data, dict):
        raise invalid_response()
    return data


class SiliconFlowEmbedding:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        settings = self.settings
        data = await post_json(self.client, settings.embedding_base_url.rstrip("/") + "/embeddings",
                               settings.embedding_api_key.get_secret_value(),
                               {"model": settings.embedding_model, "input": texts, "encoding_format": "float"})
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise invalid_response()
        mapped: dict[int, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise invalid_response()
            index, vector = row.get("index"), row.get("embedding")
            if (type(index) is not int or index not in range(len(texts)) or index in mapped
                    or not isinstance(vector, list) or not vector
                    or any(type(value) not in {int, float} for value in vector)):
                raise invalid_response()
            mapped[index] = vector
        return [mapped[index] for index in range(len(texts))]


class SiliconFlowChat:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    async def complete(self, messages: list[dict[str, str]]) -> str:
        settings = self.settings
        data = await post_json(self.client, settings.chat_base_url.rstrip("/") + "/chat/completions",
                               settings.chat_api_key.get_secret_value(),
                               {"model": settings.chat_model, "messages": messages, "stream": False,
                                "response_format": {"type": "json_object"},
                                "max_tokens": settings.chat_max_output_tokens, "temperature": 0})
        try:
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ValueError
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip() or len(content) > 40000:
                raise ValueError
            # Verify text really is JSON; schema and source membership are service responsibilities.
            json.loads(content)
            return content
        except (KeyError, TypeError, ValueError, AttributeError):
            raise invalid_response() from None
