"""微信虚拟消息列表的新消息锚点状态。"""

from __future__ import annotations

from dataclasses import dataclass

import wechat_ui as w


def tail_of(msgs: list[w.ChatMessage]) -> w.ChatMessage | None:
    """取最后一条真正的内容消息（跳过时间分隔行）。"""
    for msg in reversed(msgs):
        if msg.kind != "time":
            return msg
    return None


def messages_after_anchor(
    msgs: list[w.ChatMessage],
    anchor: w.ChatMessage | None,
) -> tuple[bool, list[w.ChatMessage]]:
    """找出上一次尾部消息之后的全部内容行。

    优先按 RuntimeId 定位。控件重建导致 ID 变化时，只有内容组合在
    当前屏幕唯一出现才用文字兜底，避免连续相同文字时选错锚点。
    """
    content = [msg for msg in msgs if msg.kind != "time"]
    tail = content[-1] if content else None
    if anchor is None or tail is None:
        return False, [tail] if tail else []

    anchor_key = anchor.key()
    for index in range(len(content) - 1, -1, -1):
        if content[index].key() == anchor_key:
            return True, content[index + 1 :]

    anchor_content = (anchor.kind, anchor.text, anchor.cls)
    matches = [
        index
        for index, msg in enumerate(content)
        if (msg.kind, msg.text, msg.cls) == anchor_content
    ]
    if len(matches) == 1:
        return True, content[matches[0] + 1 :]
    return False, [tail]


@dataclass(frozen=True)
class MonitorUpdate:
    changed: bool
    anchor_found: bool
    fresh: list[w.ChatMessage]
    current_signature: tuple
    current_tail: w.ChatMessage | None


class MessageMonitor:
    """保存当前已提交的窗口签名与尾部锚点。"""

    def __init__(self, snapshot: list[w.ChatMessage]):
        self.signature = w.message_sequence_signature(snapshot)
        self.tail = tail_of(snapshot)

    def inspect(self, snapshot: list[w.ChatMessage]) -> MonitorUpdate:
        current_signature = w.message_sequence_signature(snapshot)
        current_tail = tail_of(snapshot)
        if current_signature == self.signature:
            # 内容不变时刷新 RuntimeId，降低后续控件重建造成锚点丢失的概率。
            self.tail = current_tail
            return MonitorUpdate(False, True, [], current_signature, current_tail)

        anchor_found, fresh = messages_after_anchor(snapshot, self.tail)
        if not fresh:
            self.commit(snapshot, current_tail)
        return MonitorUpdate(True, anchor_found, fresh, current_signature, current_tail)

    def commit(
        self, snapshot: list[w.ChatMessage], final_tail: w.ChatMessage | None = None
    ) -> None:
        self.signature = w.message_sequence_signature(snapshot)
        self.tail = final_tail if final_tail is not None else tail_of(snapshot)
