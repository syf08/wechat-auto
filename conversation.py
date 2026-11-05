"""纯内存对话状态：聊天上下文、图片记忆和已发送回复跟踪。"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import stickers as sticker_catalog
from wechat_ui import ChatMessage

_OWNER_LABEL = "我" if config.OWNER_NAME == "我" else f"我({config.OWNER_NAME})"
SENDER_LABEL = {"in": "对方", "out": _OWNER_LABEL, None: "?"}
BEIJING = timezone(timedelta(hours=8))

_PLACEHOLDER = {
    "ChatImageItemView": "[图片]",
    "ChatBubbleReferItemView": "[表情包]",
    "ChatVoiceItemView": "[语音]",
    "ChatVideoItemView": "[视频]",
    "ChatFileItemView": "[文件]",
    "ChatLocationItemView": "[位置]",
    "ChatCardItemView": "[名片]",
    "ChatAppReaderItemView": "[链接/公众号文章]",
}
_VISUAL_CLASSES = frozenset({"ChatImageItemView", "ChatBubbleReferItemView"})


@dataclass(frozen=True)
class VisualMemory:
    """运行期间保存在内存里的一条图片/表情包截图。"""

    digest: str
    data_url: str
    size: tuple[int, int]
    label: str


def placeholder_for(msg: ChatMessage) -> str:
    short = msg.cls.split("::")[-1] if msg.cls else ""
    return _PLACEHOLDER.get(short, f"[{short or '消息'}]")


def describe_message(msg: ChatMessage) -> str:
    if msg.kind == "text":
        return msg.text
    marker = placeholder_for(msg)
    if msg.kind == "voice" and msg.transcript:
        if msg.transcript.strip() == "（转写失败）":
            return f"{marker} （转写失败）"
        return f"[语音转写] {msg.transcript}"
    return marker


def is_visual_message(msg: ChatMessage) -> bool:
    short = msg.cls.split("::")[-1] if msg.cls else ""
    return short in _VISUAL_CLASSES


class SentReplyTracker:
    """统一保存最近成功发送的回复，供回显去重和发送方兜底判断使用。"""

    def __init__(self, limit: int = 20):
        self._items: deque[str] = deque(maxlen=max(1, int(limit)))

    def remember(self, text: str) -> None:
        self._items.append(text)

    def recent(self, limit: int = 5) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        return tuple(self._items)[-limit:]

    def consume_echo(self, text: str) -> bool:
        """消费一条刚由 UIA 回显的自己消息，避免重复写入上下文。"""
        try:
            self._items.remove(text)
        except ValueError:
            return False
        return True

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self._items)


class Context:
    """内存里的对话上下文：启动历史 + 运行期间产生的最近往来。"""

    def __init__(self, history: list[ChatMessage], label_history: bool):
        self.history = history
        self.runtime: list[tuple[str | None, str]] = []
        self.label_history = label_history
        self.visuals: list[VisualMemory] = []
        self._recent_sticker_ids: deque[str] = deque()

    def record(self, msg: ChatMessage, sender: str | None) -> None:
        self.runtime.append((sender, self.describe(msg, sender)))
        limit = max(0, int(config.RUNTIME_HISTORY_LIMIT))
        if not limit:
            self.runtime.clear()
        elif len(self.runtime) > limit:
            del self.runtime[:-limit]

    def remember_visual(self, data_url: str, size: tuple[int, int], label: str) -> str:
        """把截图留在内存；相同图片去重，超过上限时丢掉最旧的。"""
        digest = hashlib.sha256(data_url.encode("ascii")).hexdigest()
        self.visuals = [v for v in self.visuals if v.digest != digest]
        self.visuals.append(VisualMemory(digest, data_url, size, label))
        limit = max(0, int(config.VISION_MEMORY_LIMIT))
        if not limit:
            self.visuals.clear()
        elif len(self.visuals) > limit:
            self.visuals = self.visuals[-limit:]
        return digest

    def visual_snapshot(self) -> list[VisualMemory]:
        return list(self.visuals)

    def remember_sticker(self, sticker_id: str) -> None:
        """只记录发送成功的贴纸，用于后续去重。"""
        if sticker_id not in sticker_catalog.STICKERS_BY_ID:
            return
        self._recent_sticker_ids.append(sticker_id)
        limit = max(0, int(config.STICKER_DIVERSITY_WINDOW))
        while len(self._recent_sticker_ids) > limit:
            self._recent_sticker_ids.popleft()

    def recent_sticker_ids(self) -> tuple[str, ...]:
        """返回最近成功发送的贴纸快照，供一次生成任务稳定使用。"""
        limit = max(0, int(config.STICKER_DIVERSITY_WINDOW))
        values = list(self._recent_sticker_ids)
        return tuple(values[-limit:] if limit else ())

    def describe(self, msg: ChatMessage, sender: str | None) -> str:
        body = describe_message(msg)
        if sender is None:
            return body
        return f"{SENDER_LABEL[sender]}：{body}"

    def build_user_message(self, new_items: list[ChatMessage]) -> str:
        now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        lines = [
            f"【当前时间】{now}（北京时间，UTC+8）",
            "",
            "【历史聊天记录】（脚本启动前读取的最近 "
            f"{len(self.history)} 条，"
            + ("标注了发送方）" if self.label_history else "不分是谁发的）"),
        ]
        if not self.history:
            lines.append("(没有历史消息)")
        for msg in self.history:
            if self.label_history and msg.sender in SENDER_LABEL:
                lines.append(f"{SENDER_LABEL[msg.sender]}：{describe_message(msg)}")
            else:
                lines.append(describe_message(msg))

        lines += ["", "【本次运行中的聊天记录】"]
        if not self.runtime:
            lines.append("(暂无)")
        else:
            lines += [text for _sender, text in self.runtime]

        lines += ["", "【对方的新消息】"]
        for msg in new_items:
            lines.append(f"对方：{describe_message(msg)}")
        return "\n".join(lines)
