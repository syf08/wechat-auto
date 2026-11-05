"""DeepSeek API 客户端（OpenAI 兼容接口），支持按需联网搜索。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from deepseek_search import (
    WEB_SEARCH_TOOL,
    SearchError,
    format_results,
    normalize_query,
    search_web,
)

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_MAX_TOKENS = 300


class DeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    elapsed: float = 0.0


class DeepSeek:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE,
        timeout: float = 20.0,
        *,
        thinking: bool = False,
        search_enabled: bool = True,
        search_count: int = 8,
        search_timeout: float = 6.0,
    ):
        if not api_key:
            raise DeepSeekError("缺少 DeepSeek API Key")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.thinking = thinking
        self.search_enabled = search_enabled
        self.search_count = search_count
        self.search_timeout = search_timeout

        self.last_elapsed = 0.0
        self.last_search_query: str | None = None
        self.last_search_elapsed = 0.0
        self.last_search_count = 0
        self.last_search_error: str | None = None
        self._search_available = bool(search_enabled)

    # ------------------------------------------------------------------ 请求
    def _payload(self, messages, max_tokens, temperature, stream, tools=None, tool_choice=None):
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        return payload

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 1.3,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatResult:
        """发一次流式请求，返回正文、工具调用和本次耗时。"""
        t0 = time.time()
        try:
            result = self._stream(messages, max_tokens, temperature, tools, tool_choice)
        except DeepSeekError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DeepSeekError(f"调用 DeepSeek 失败：{exc}") from exc
        elapsed = time.time() - t0
        self.last_elapsed = elapsed
        return ChatResult(result.content, result.tool_calls, result.finish_reason, elapsed)

    def reply(
        self, messages: list[dict], max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = 1.3
    ) -> str:
        """无工具的单次回复，保留原行为供 ping 等调用。"""
        return self.chat(messages, max_tokens, temperature).content.strip()

    def reply_with_search(
        self, messages: list[dict], max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = 1.3
    ) -> str:
        """按需联网：模型先决定是否调用 web_search，再生成最终回复。

        一条消息最多搜索一次。搜索失败时仍会再走一次模型调用，让模型用
        “我看看”“不确定”这类自然说法兜底，而不是把搜索错误暴露给对方。
        """
        self.last_search_query = None
        self.last_search_elapsed = 0.0
        self.last_search_count = 0
        self.last_search_error = None
        self.last_elapsed = 0.0

        if not self._search_available:
            return self.reply(messages, max_tokens, temperature)

        msgs = [dict(m) for m in messages]
        model_elapsed = 0.0
        # 无工具调用时，这一次响应的正文会直接发给聊天对象，因此必须
        # 使用正式的闲聊温度和长度预算，而不是更低温的“工具决策参数”。
        first = self.chat(
            msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=[WEB_SEARCH_TOOL],
            tool_choice="auto",
        )
        model_elapsed += first.elapsed

        if not first.tool_calls:
            self.last_elapsed = model_elapsed
            return first.content.strip()

        # 把模型这一步的 tool_calls 原样放回对话，后面每个 id 都要有 tool 回复。
        assistant_calls = []
        for i, tc in enumerate(first.tool_calls):
            assistant_calls.append(
                {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
            )
        msgs.append(
            {
                "role": "assistant",
                "content": first.content or None,
                "tool_calls": assistant_calls,
            }
        )

        searched = False
        for i, tc in enumerate(first.tool_calls):
            call_id = tc.id or f"call_{i}"
            if tc.name != "web_search":
                tool_text = "未执行：未知工具。"
            elif searched:
                tool_text = "未执行：一条回复最多只允许一次搜索。"
            else:
                searched = True
                query = None
                try:
                    args = json.loads(tc.arguments or "{}")
                    if isinstance(args, dict):
                        query = normalize_query(str(args.get("query") or ""))
                except (json.JSONDecodeError, TypeError):
                    query = None

                if query is None:
                    self.last_search_error = "搜索词为空或包含个人隐私"
                    tool_text = (
                        "未执行：搜索词无效或包含个人隐私。"
                        "请自然带过，不要编造，也不要提搜索失败。"
                    )
                else:
                    self.last_search_query = query
                    t0 = time.time()
                    try:
                        results = search_web(
                            self.api_key,
                            query,
                            count=self.search_count,
                            base_url=self.base_url,
                            timeout=self.search_timeout,
                        )
                    except SearchError as exc:
                        self.last_search_elapsed = time.time() - t0
                        self.last_search_error = str(exc)
                        if exc.status in (401, 402):
                            self._search_available = False
                        tool_text = (
                            "搜索失败，没有可靠结果。请自然带过，" "不要编造，也不要提搜索失败。"
                        )
                    else:
                        self.last_search_elapsed = time.time() - t0
                        self.last_search_count = len(results)
                        if results:
                            tool_text = format_results(results)
                        else:
                            self.last_search_error = "没有找到相关结果"
                            tool_text = (
                                "没有找到可靠结果。请自然带过，" "不要编造，也不要提搜索失败。"
                            )

            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tc.name,
                    "content": tool_text,
                }
            )

        try:
            final = self.chat(msgs, max_tokens=max_tokens, temperature=temperature)
        except DeepSeekError:
            if first.content.strip():
                self.last_elapsed = model_elapsed
                return first.content.strip()
            raise

        model_elapsed += final.elapsed
        self.last_elapsed = model_elapsed
        text = final.content.strip()
        if text:
            return text
        if first.content.strip():
            return first.content.strip()
        return "我看看，待会儿和你说"

    def _stream(self, messages, max_tokens, temperature, tools, tool_choice) -> ChatResult:
        body = json.dumps(
            self._payload(messages, max_tokens, temperature, True, tools, tool_choice),
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        chunks: list[str] = []
        tool_slots: dict[int, dict] = {}
        finish_reason: str | None = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("error"):
                        detail = json.dumps(obj["error"], ensure_ascii=False)[:400]
                        raise DeepSeekError(f"DeepSeek 返回错误：{detail}")
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            chunks.append(piece)
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            if not isinstance(idx, int):
                                idx = 0
                            slot = tool_slots.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            name_piece = fn.get("name") or ""
                            if name_piece:
                                slot["name"] = merge_stream_tool_name(slot["name"], name_piece)
                            args_piece = fn.get("arguments") or ""
                            if args_piece:
                                slot["arguments"] += args_piece
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise DeepSeekError(f"DeepSeek 返回 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise DeepSeekError(f"连不上 DeepSeek（检查网络）：{exc.reason}") from exc
        except TimeoutError as exc:
            raise DeepSeekError("连接 DeepSeek 超时") from exc

        tool_calls = tuple(
            ToolCall(slot["id"] or f"call_{idx}", slot["name"], slot["arguments"] or "{}")
            for idx, slot in sorted(tool_slots.items())
            if slot["name"]
        )
        return ChatResult("".join(chunks), tool_calls, finish_reason)

    # ------------------------------------------------------------- 连通性自检
    def ping(self) -> str:
        text = self.reply(
            [{"role": "user", "content": "回复两个字：收到"}], max_tokens=8, temperature=1.0
        )
        return text


def merge_stream_tool_name(current: str, piece: str) -> str:
    """合并流式工具名，同时兼容增量与累计两种服务端行为。

    增量示例：``web_`` + ``search``；累计示例：``web`` + ``web_search``。
    """
    if not piece:
        return current
    if not current:
        return piece
    if piece.startswith(current):
        return piece
    if current.endswith(piece):
        return current
    return current + piece
