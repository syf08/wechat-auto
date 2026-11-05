"""微信自动回复主程序（纯 UI 自动化，后台收发）。

原生收藏贴纸需要一个短暂的表情弹层，点击完成后会还原原前台窗口。

用法：
    python bot.py              # 运行后按提示输入联系人名字
    python bot.py 联系人名      # 也可以直接带参数

聊天记录只在内存里流转，"每条消息都记下来"指的是内存里的上下文，
不会在本地留下任何聊天记录文件。

回复在单个后台任务中生成，主线程继续轮询微信。生成期间收到的新消息会
排队；当前回复先由主线程发送，成功写入上下文后，再按顺序生成后续回复。
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import config
import stickers as sticker_catalog
import wechat_session
import wechat_ui as w
from conversation import (
    Context,
    SentReplyTracker,
    VisualMemory,
    describe_message as _plain,
    is_visual_message as _is_visual_message,
    placeholder_for as _placeholder,
    SENDER_LABEL,
)
from deepseek_client import DeepSeek, DeepSeekError
from message_monitor import MessageMonitor
from secret_store import SecretStoreError, save_secret
from wechat_capture import INCOMING, OUTGOING, WindowCapture


class PromptError(RuntimeError):
    pass


class StartupCalibrationError(RuntimeError):
    """启动时无法获得可信的新消息基线。"""


def _remember_sent_reply(ctx: Context, reply: str, sent_replies: SentReplyTracker) -> None:
    """发送成功后统一更新上下文和回显判定队列。"""
    ctx.record(
        w.ChatMessage("text", reply, "mmui::ChatTextItemView", None, OUTGOING),
        OUTGOING,
    )
    sent_replies.remember(reply)


@dataclass
class GenerationJob:
    """后台生成中的一条回复。

    主线程继续读取微信并把后续消息放入 pending_followups；后台线程只调用
    DeepSeek，不碰 UIA，也不发送微信消息，避免并发操作同一个聊天窗口。
    """

    msg: w.ChatMessage
    future: Future
    started_at: float
    tag: str
    sticker_due: bool


@dataclass(frozen=True)
class ReplySendResult:
    """文字和可选原生贴纸的发送结果。"""

    text_sent: bool
    sticker_attempted: bool = False
    sticker_sent: bool = False
    sticker_id: str | None = None
    sticker_error: str | None = None


# ------------------------------------------------------------------ 上下文
def _build_user_content(
    ctx: Context,
    msg: w.ChatMessage,
    cap: WindowCapture | None,
    viewport: tuple | None,
    tag: str | None = None,
    resolve_rect: Callable[[], tuple | None] | None = None,
) -> tuple[str | list[dict], bool]:
    """给模型准备文本上下文，并附带运行期间记住的图片/表情包截图。"""
    text = ctx.build_user_message([msg])
    previous = ctx.visual_snapshot()
    visual = _is_visual_message(msg)
    if not config.VISION_ENABLED or not visual or cap is None or not msg.rect:
        if previous:
            return _visual_content(text, previous), True
        return text, False

    prefix = tag or f"[{_now()}]"
    try:
        data_url, size = cap.message_image(
            msg.rect,
            msg.sender,
            viewport=viewport,
            max_side=config.VISION_MAX_SIDE,
            settle_seconds=config.VISION_RENDER_WAIT,
            retry_seconds=config.VISION_RETRY_WAIT,
            retries=config.VISION_CAPTURE_RETRIES,
            resolve_rect=resolve_rect,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix} ! 图片截图失败，改用文字占位符：{exc}")
        if previous:
            return _visual_content(text, previous), True
        return text, False

    print(f"{prefix} - 已截取{_placeholder(msg)}（{size[0]}x{size[1]}），" f"交给 AI 分析")
    digest = ctx.remember_visual(data_url, size, _placeholder(msg))
    current = VisualMemory(digest, data_url, size, _placeholder(msg))
    previous = [v for v in ctx.visual_snapshot() if v.digest != digest]
    return _visual_content(text, previous, current), True


def _visual_content(
    text: str, previous: list[VisualMemory], current: VisualMemory | None = None
) -> list[dict]:
    """把文本上下文和历史/当前图片组装成 OpenAI 兼容内容块。"""
    blocks: list[dict] = [{"type": "text", "text": text}]
    if previous:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "【本次运行中之前收到的图片/表情包】\n"
                    "下面按收到时间从早到晚附带这些图片，和聊天记录里的 "
                    "[图片]/[表情包] 占位符对应。请把它们当作已经看过的聊天内容。"
                ),
            }
        )
        for i, visual in enumerate(previous, 1):
            blocks.append(
                {
                    "type": "text",
                    "text": f"【上文图片 {i}/{len(previous)}】{visual.label}",
                }
            )
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": visual.data_url},
                }
            )

    if current is not None:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "【当前消息附带的图片/表情包】\n"
                    "这张截图是当前这一条新消息。请先理解图片内容，再结合上面的"
                    "聊天记录和之前收到的图片回复。不要描述截图、头像或界面，"
                    "也不要提自己看到了图片。"
                ),
            }
        )
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": current.data_url},
            }
        )
    return blocks


def _prepare_ai_request(
    prompt: str,
    ctx: Context,
    msg: w.ChatMessage,
    cap: WindowCapture | None,
    viewport: tuple | None,
    tag: str | None = None,
    resolve_rect: Callable[[], tuple | None] | None = None,
    sticker_due: bool = True,
    recent_sticker_ids: tuple[str, ...] | None = None,
) -> tuple[list[dict], bool, str]:
    """在主线程准备模型输入，图片截图相关的 UIA 调用不会进入工作线程。"""
    content, image_used = _build_user_content(ctx, msg, cap, viewport, tag, resolve_rect)
    system_prompt = prompt.rstrip()
    if sticker_catalog.ENABLED:
        if recent_sticker_ids is None:
            recent_sticker_ids = ctx.recent_sticker_ids()
        system_prompt += "\n\n" + sticker_catalog.sticker_prompt_block(
            sticker_due,
            recent_sticker_ids,
            dedicated_selector=config.STICKER_SELECTOR_ENABLED,
        )
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
    fallback_text = ctx.build_user_message([msg])
    return messages, image_used, fallback_text


def _sticker_selector_content(
    messages: list[dict],
    fallback_text: str,
    reply: str,
    image_used: bool,
) -> str | list[dict]:
    """给贴纸选择器保留当前视觉语境，不只传图片占位符。"""
    source = fallback_text
    if image_used and len(messages) > 1:
        source = messages[1].get("content", fallback_text)

    instruction = (
        "\n\n【需要判断的文字回复】\n"
        f"{reply}\n\n"
        "请结合上面的完整聊天语境判断这张回复是否适合配一张贴纸，"
        "并遵守系统给出的语义匹配和近期去重规则。"
    )
    if isinstance(source, list):
        return [*source, {"type": "text", "text": instruction}]
    return "【聊天上下文】\n" f"{source}" f"{instruction}"


def _complete_ai_request(
    ai: DeepSeek,
    messages: list[dict],
    image_used: bool,
    fallback_text: str,
    tag: str | None = None,
    sticker_due: bool = True,
    recent_sticker_ids: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """执行一次模型调用；图片视觉失败时退回文字占位符。"""
    try:
        reply = ai.reply_with_search(
            messages, max_tokens=config.MAX_TOKENS, temperature=config.TEMPERATURE
        )
    except DeepSeekError as exc:
        if not image_used:
            raise
        prefix = tag or f"[{_now()}]"
        print(f"{prefix} ! 图片分析失败，改用文字占位符：{exc}")
        reply = ai.reply_with_search(
            [
                {"role": "system", "content": messages[0]["content"]},
                {"role": "user", "content": fallback_text},
            ],
            max_tokens=config.MAX_TOKENS,
            temperature=config.TEMPERATURE,
        )
        image_used = False

    text, inline_choice = sticker_catalog.split_sticker_directive(reply)
    reply = text
    factual = bool(ai.last_search_query)
    sticker_id = None
    sticker_allowed = sticker_catalog.ENABLED and sticker_due and not factual

    has_sticker_candidates = bool(sticker_catalog.eligible_stickers(recent_sticker_ids))
    selector_returned_none = False
    if sticker_allowed and config.STICKER_SELECTOR_ENABLED and has_sticker_candidates:
        main_elapsed = ai.last_elapsed
        selector_elapsed = 0.0
        selector_messages = [
            {
                "role": "system",
                "content": sticker_catalog.sticker_selector_prompt(recent_sticker_ids),
            },
            {
                "role": "user",
                "content": _sticker_selector_content(messages, fallback_text, reply, image_used),
            },
        ]
        try:
            selected = ai.reply(
                selector_messages,
                max_tokens=config.STICKER_SELECTOR_MAX_TOKENS,
                temperature=config.STICKER_SELECTOR_TEMPERATURE,
            )
            selector_elapsed = ai.last_elapsed
            selector_returned_none = "none" in selected.strip().lower()
            sticker_id = sticker_catalog.parse_sticker_choice(selected, recent_sticker_ids)
        except DeepSeekError as exc:
            prefix = tag or f"[{_now()}]"
            print(f"{prefix} ! 贴纸选择失败，本条只发文字：{exc}")
        finally:
            ai.last_elapsed = main_elapsed + selector_elapsed
    elif sticker_allowed and inline_choice is not None:
        sticker_id = sticker_catalog.parse_sticker_choice(inline_choice, recent_sticker_ids)

    if (
        sticker_allowed
        and selector_returned_none
        and sticker_id is None
        and config.STICKER_STRICTNESS == "宽松"
    ):
        fallback_started = time.time()
        try:
            selected = ai.reply(
                [
                    {
                        "role": "system",
                        "content": sticker_catalog.sticker_fallback_prompt(recent_sticker_ids),
                    },
                    {
                        "role": "user",
                        "content": _sticker_selector_content(
                            messages, fallback_text, reply, image_used
                        ),
                    },
                ],
                max_tokens=config.STICKER_SELECTOR_MAX_TOKENS,
                temperature=config.STICKER_SELECTOR_TEMPERATURE,
            )
            sticker_id = sticker_catalog.parse_sticker_choice(
                selected, recent_sticker_ids
            )
        except DeepSeekError as exc:
            prefix = tag or f"[{_now()}]"
            print(f"{prefix} ! 宽松贴纸补选失败，本条只发文字：{exc}")
        finally:
            ai.last_elapsed = main_elapsed + selector_elapsed + (time.time() - fallback_started)

    if sticker_id is not None:
        reply = f"{reply.rstrip()} [[原生表情:{sticker_id}]]"
    return reply, image_used


def _split_reply(reply: str) -> tuple[str, str | None]:
    """清洗模型回复，并拆出末尾的可选原生贴纸指令。"""
    text, sticker_id = sticker_catalog.split_sticker_directive(reply)
    if not sticker_catalog.ENABLED:
        sticker_id = None
    return text, sticker_id


def _send_reply_parts(chat: w.ChatWindow, text: str, sticker_id: str | None) -> ReplySendResult:
    """先发文字，再尝试搭配一张原生收藏贴纸。

    贴纸失败不会重复发送已经成功的文字；下一条消息仍可正常继续。
    """
    text_sent = bool(chat.send(text))
    if not text_sent or not sticker_id:
        return ReplySendResult(text_sent=text_sent, sticker_id=sticker_id)

    spec = sticker_catalog.STICKERS_BY_ID.get(sticker_id)
    if spec is None:
        return ReplySendResult(
            text_sent=True,
            sticker_attempted=True,
            sticker_id=sticker_id,
            sticker_error="贴纸 ID 不在目录中",
        )

    try:
        sent = chat.send_sticker(
            spec.index,
            panel_timeout=config.STICKER_PANEL_TIMEOUT,
            send_timeout=config.STICKER_SEND_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return ReplySendResult(
            text_sent=True,
            sticker_attempted=True,
            sticker_id=sticker_id,
            sticker_error=str(exc),
        )
    return ReplySendResult(
        text_sent=True,
        sticker_attempted=True,
        sticker_sent=bool(sent),
        sticker_id=sticker_id,
        sticker_error=None if sent else "没有等到原生贴纸的发送回显",
    )


def _ask_ai(
    ai: DeepSeek,
    prompt: str,
    ctx: Context,
    msg: w.ChatMessage,
    cap: WindowCapture | None,
    viewport: tuple | None,
    tag: str | None = None,
    resolve_rect: Callable[[], tuple | None] | None = None,
) -> tuple[str, bool]:
    """同步调用入口，供自检和补发等不需要异步流水线的路径使用。"""
    recent_sticker_ids = ctx.recent_sticker_ids()
    messages, image_used, fallback_text = _prepare_ai_request(
        prompt, ctx, msg, cap, viewport, tag, resolve_rect, recent_sticker_ids=recent_sticker_ids
    )
    return _complete_ai_request(
        ai, messages, image_used, fallback_text, tag, recent_sticker_ids=recent_sticker_ids
    )


def _calibrate_startup_baseline(
    chat: w.ChatWindow,
    timeout_seconds: float,
) -> list[w.ChatMessage]:
    """等待聊天页稳定到底，返回可作为已读基线的快照。

    未确认到底时绝不能建立正式锚点：微信列表是虚拟化的，继续向下滚动后
    可能暴露出更多旧消息，它们会被错误地当成启动后收到的新消息。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    attempts = 0
    while True:
        attempts += 1
        cur, bottom_confirmed = chat.settle_to_bottom()
        if bottom_confirmed:
            if attempts > 1:
                print(f"[{_now()}] - 启动校准完成（第 {attempts} 次确认到底）")
            return cur

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StartupCalibrationError(f"启动校准超时：{attempts} 次仍无法确认聊天页已到最底部")
        print(
            f"[{_now()}] ! 启动校准第 {attempts} 次未确认到底，"
            f"继续等待（剩余约 {remaining:.1f}s）"
        )
        time.sleep(min(0.25, remaining))


def _read_startup_history(
    chat: w.ChatWindow, cap: WindowCapture, limit: int
) -> list[w.ChatMessage]:
    """读取启动历史；发送方识别失败时退回无标签历史，而不是中止启动。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    # 消息列表是虚拟化的；目标条数越多，需要向上翻页的次数也越多。
    max_up = max(14, min(100, limit + 10))
    classify = None
    if config.LABEL_HISTORY_SENDERS:
        viewport = chat.viewport
        classify = lambda rects: cap.classify_batch(rects, viewport)  # noqa: E731
        try:
            return chat.recent_messages(limit, max_up=max_up, classify=classify)
        except Exception as exc:  # noqa: BLE001
            print(f"[{_now()}] ! 历史消息发送方识别失败，先按无标签读取：{exc}")
    return chat.recent_messages(limit, max_up=max_up, classify=None)


def _merge_startup_baseline(
    history: list[w.ChatMessage],
    current: list[w.ChatMessage],
    chat: w.ChatWindow,
    cap: WindowCapture,
    limit: int,
) -> list[w.ChatMessage]:
    """把历史读取完成后、校准完成前刚出现的消息补进历史上下文。"""
    if limit <= 0:
        return []
    visible = [m for m in current if m.kind != "time"]
    if not visible:
        return history[-limit:]
    if not history:
        return visible[-limit:]
    if config.LABEL_HISTORY_SENDERS:
        try:
            senders = cap.classify_batch([m.rect for m in visible], chat.viewport)
            if len(senders) == len(visible):
                visible = [m.with_sender(sender) for m, sender in zip(visible, senders)]
        except Exception as exc:  # noqa: BLE001
            print(f"[{_now()}] ! 校准期间新消息发送方识别失败：{exc}")

    def same_runtime(left: w.ChatMessage, right: w.ChatMessage) -> bool:
        if left.runtime_id is not None and right.runtime_id is not None:
            return left.runtime_id == right.runtime_id
        return False

    def same_content(left: w.ChatMessage, right: w.ChatMessage) -> bool:
        return (left.kind, left.text, left.cls) == (right.kind, right.text, right.cls)

    # 当前屏可能以一条更早的半截消息开头，找到历史尾部在当前屏中的
    # 最长连续重叠，只把重叠之后的新消息追加进上下文。
    def find_append_from(predicate) -> int | None:
        for size in range(min(len(history), len(visible)), 0, -1):
            for start in range(0, len(visible) - size + 1):
                if all(predicate(history[-size + i], visible[start + i]) for i in range(size)):
                    return start + size
        return None

    # RuntimeId 会在虚拟列表重建时变化；先按 ID 对齐，找不到时再用
    # 内容序列兜底，避免丢掉历史读取完成后、校准完成前出现的消息。
    append_from = find_append_from(same_runtime)
    if append_from is None:
        append_from = find_append_from(same_content)
    if append_from is None:
        append_from = len(visible)
    merged = list(history) + visible[append_from:]
    return merged[-limit:]


def _note_generation_failure(
    ctx: Context,
    msg: w.ChatMessage,
    tag: str,
    reason: str,
    failures: dict[tuple, int],
    pending: deque[w.ChatMessage],
    now: float,
) -> float | None:
    """统一处理生成/清洗/发送失败，返回下次可重试时间。

    达到上限时把消息记为已处理，保证它仍进入后续上下文，而不是静默消失。
    """
    key = msg.key()
    count = failures.get(key, 0) + 1
    failures[key] = count
    if count <= config.GENERATION_RETRIES:
        print(f"[{tag}] ! {reason}（稍后重试 " f"{count}/{config.GENERATION_RETRIES}）")
        pending.appendleft(msg)
        return now + min(5.0, float(count))

    print(f"[{tag}] ! {reason}，连续失败，跳过这条")
    failures.pop(key, None)
    ctx.record(msg, INCOMING)
    return None


# ------------------------------------------------------------------ 主流程
def ask_contact_name() -> str:
    print("=" * 62)
    print(" 微信自动回复（后台运行，不会抢你正在用的窗口）")
    print("=" * 62)
    print(" 请输入联系人名字（要和微信里的备注名/昵称完全一致）")
    print("-" * 62)
    try:
        name = input(" 联系人名字: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return name


def run(contact: str, selftest: bool = False, reply_pending: bool = False) -> int:
    if selftest and reply_pending:
        print("✗ --selftest 和 --reply-pending 不能同时使用。")
        return 1
    if config.PROFILE_LOAD_ERROR:
        print(f"! 个性化配置读取失败，暂时使用默认设置：{config.PROFILE_LOAD_ERROR}")
    if config.SECRET_LOAD_ERROR and not config.DEEPSEEK_API_KEY:
        print(f"✗ 加密密钥读取失败：{config.SECRET_LOAD_ERROR}")
        print("  请双击“配置微信自动回复.bat”重新保存。")
        return 3
    if not config.DEEPSEEK_API_KEY:
        print("✗ 没有找到 DeepSeek API Key。")
        print("  请双击“配置微信自动回复.bat”完成设置，或设置 DEEPSEEK_API_KEY 环境变量。")
        return 3
    try:
        prompt = _read_prompt()
    except PromptError as exc:
        print(f"\n✗ {exc}")
        return 2
    prompt = f"{prompt.rstrip()}\n\n{config.PROFILE_PROMPT}"
    print("     系统提示词：prompt.txt + 个性化设置")
    print(f"     个性化：{config.OWNER_NAME}")

    print(f"\n[1/4] 正在微信里搜索并打开「{contact}」的独立聊天窗口 ...")
    t0 = time.time()
    try:
        chat = wechat_session.open_contact_chat(contact)
    except wechat_session.ContactNotFound as exc:
        print(f"\n✗ {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 打开聊天窗口失败：{exc}")
        return 2
    print(f"     已打开独立聊天窗口（{time.time() - t0:.1f}s），标题：{chat.title}")
    print("     （若微信版本切换了前台窗口，配对结束后会自动还原）")

    cap = WindowCapture(chat.hwnd)

    print(f"\n[2/4] 读取最近 {config.HISTORY_LIMIT} 条聊天记录 ...")
    try:
        history = _read_startup_history(chat, cap, config.HISTORY_LIMIT)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 读取历史聊天记录失败：{exc}")
        cap.release()
        return 2
    ctx = Context(history, config.LABEL_HISTORY_SENDERS)
    for m in history:
        if m.kind == "text":
            print(f"     {SENDER_LABEL.get(m.sender, '?'):>8} {m.text}")

    print("\n[3/4] 校准聊天页并连接 DeepSeek ...")
    # 必须尽早建立消息锚点。连接、连通性检查或自检可能耗时数秒，期间收到的
    # 新消息如果到这时才校准，会被当成“启动前旧消息”而永久漏回。
    try:
        cur = _calibrate_startup_baseline(chat, config.STARTUP_BOTTOM_TIMEOUT)
    except StartupCalibrationError as exc:
        print(f"[{_now()}] ✗ {exc}")
        print("  为避免误回复历史消息，脚本已停止。" "请保持聊天窗口可见后重试。")
        cap.release()
        return 5
    except Exception as exc:  # noqa: BLE001
        print(f"[{_now()}] ✗ 启动校准失败：{exc}")
        print("  为避免误回复历史消息，脚本已停止。")
        cap.release()
        return 5

    ctx.history = _merge_startup_baseline(ctx.history, cur, chat, cap, config.HISTORY_LIMIT)
    monitor = MessageMonitor(cur)
    sent_replies = SentReplyTracker()
    last_reply_at = 0.0
    replies_since_sticker = config.STICKER_MIN_REPLY_GAP

    print("     正在连接 DeepSeek ...")
    try:
        ai = DeepSeek(
            config.DEEPSEEK_API_KEY,
            config.DEEPSEEK_MODEL,
            config.DEEPSEEK_BASE_URL,
            config.REQUEST_TIMEOUT,
            thinking=config.DEEPSEEK_THINKING,
            search_enabled=config.SEARCH_ENABLED,
            search_count=config.SEARCH_COUNT,
            search_timeout=config.SEARCH_TIMEOUT,
        )
        t0 = time.time()
        ai.ping()
        thinking = "思考" if config.DEEPSEEK_THINKING else "非思考"
        print(
            f"     连接正常（{time.time() - t0:.2f}s），模型："
            f"{config.DEEPSEEK_MODEL}（{thinking}模式）"
        )
        if config.SEARCH_ENABLED:
            print("     联网搜索：按需开启（每条回复最多 1 次）")
        else:
            print("     联网搜索：未启用")
        if config.VISION_ENABLED:
            print(
                "     图片/表情包：截图后交给视觉模型分析"
                f"（运行期记忆最近 {config.VISION_MEMORY_LIMIT} 张）"
            )
        else:
            print("     图片/表情包：仅使用文字占位符")
        print("     语音：运行期收到对方语音时，调用微信“语音转文字”")
        if sticker_catalog.ENABLED:
            selector_mode = "独立选择" if config.STICKER_SELECTOR_ENABLED else "正文内联选择"
            print(
                f"     原生贴纸：按语境{selector_mode}（收藏目录 "
                f"{len(sticker_catalog.STICKERS)} 张，"
                f"间隔≥{config.STICKER_MIN_REPLY_GAP} 条文字，"
                f"近期 {config.STICKER_DIVERSITY_WINDOW} 张内不重复）"
            )
        elif config.STICKER_ENABLED:
            detail = sticker_catalog.CATALOG_LOAD_ERROR or "贴纸目录为空"
            print(f"     原生贴纸：配置无效，已停用（{detail}）")
        else:
            print("     原生贴纸：未启用")
    except DeepSeekError as exc:
        print(f"\n✗ {exc}")
        print("  请检查网络，以及 DEEPSEEK_API_KEY 环境变量或 config_secret.bin。")
        cap.release()
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 初始化 DeepSeek 失败：{exc}")
        cap.release()
        return 3

    print(
        f"\n[4/4] 开始监听（通常 2–4 秒；超过 "
        f"{config.SLOW_REPLY_SECONDS:g} 秒标慢，网络异常时可能更久）。"
        "按 Ctrl+C 退出。"
    )
    print("-" * 62)

    if selftest:
        if ctx.history:
            sent = _reply_once(
                chat,
                ctx,
                ai,
                prompt,
                ctx.history[-1].with_sender(INCOMING),
                sent_replies,
                "[自检]",
                cap,
            )
            if sent:
                last_reply_at = time.time()
        else:
            print("[自检] 没有历史消息，跳过。")
        print("-" * 62)
    elif reply_pending:
        last = ctx.history[-1] if ctx.history else None
        if last is not None and last.sender == INCOMING:
            sent = _reply_once(chat, ctx, ai, prompt, last, sent_replies, "[补发]", cap)
            if sent:
                last_reply_at = time.time()
        else:
            print("[补发] 最近一条不是对方发的消息，无需补发。")
        print("-" * 62)

    started_at = time.time()
    last_beat = time.time()
    warned_bottom_at = 0.0
    last_restore_at = 0.0
    restore_log_at = 0.0
    last_bottom_refresh = time.time()
    last_bottom_error_at = 0.0
    attempts: dict[tuple, int] = {}
    generation_failures: dict[tuple, int] = {}
    pending_followups: deque[w.ChatMessage] = deque()
    generation: GenerationJob | None = None
    ai_retry_at = 0.0

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-reply")

    try:
        while True:
            time.sleep(config.POLL_INTERVAL)
            if not chat.is_alive():
                print("\n✗ 聊天窗口被关闭了，脚本退出。")
                return 4

            # 窗口被最小化/藏起来时，脚本会变成"看不见消息"的瞎子，这里把它放回屏幕。
            # （限速 1.5s 一次，避免和微信的隐藏行为来回拉扯）
            now = time.time()
            if config.RESTORE_IF_MINIMIZED and now - last_restore_at > 1.5:
                try:
                    restored = chat.ensure_visible()
                except Exception as exc:  # noqa: BLE001
                    last_restore_at = now
                    if now - restore_log_at > 30:
                        restore_log_at = now
                        print(f"[{_now()}] ! 检查或还原聊天窗口失败：{exc}")
                else:
                    last_restore_at = now
                    if restored:
                        if now - restore_log_at > 30:
                            restore_log_at = now
                            print(
                                f"[{_now()}] 聊天窗口被最小化或隐藏了，" f"已自动还原（不抢前台）"
                            )
                        time.sleep(0.4)
                        continue

            try:
                cur = chat.messages()
            except Exception as exc:  # noqa: BLE001
                print(f"[{_now()}] ! 读取聊天窗口出错（稍后重试）：{exc}")
                time.sleep(1.0)
                continue

            # 列表是虚拟化的：视图不在底部时，新消息压根不在控件树里。
            # 几何位置可能误判，所以这里必须等待内容连续不变才算到底。
            if not chat.at_bottom(cur):
                if time.time() - warned_bottom_at > 30:
                    print(
                        f"[{_now()}] 聊天窗口没停在最新消息处，已自动滚到底部"
                        f"（否则会漏掉新消息）"
                    )
                    warned_bottom_at = time.time()
                try:
                    cur, bottom_confirmed = chat.settle_to_bottom()
                except Exception as exc:  # noqa: BLE001
                    print(f"[{_now()}] ! 滚动到底部失败：{exc}")
                    continue
                if not bottom_confirmed:
                    if time.time() - last_bottom_error_at > 30:
                        print(f"[{_now()}] ! 滚动后消息仍在变化，" f"暂时不处理这一屏")
                        last_bottom_error_at = time.time()
                    continue
                last_bottom_refresh = time.time()

            # 保险层：微信有时在后台收到新消息却不自动滚到底部。at_bottom()
            # 仍可能把“贴着底边的旧行”误认为最新行，所以定期向下滚动到
            # 内容连续不变，而不是只根据最后一条控件的几何位置判断。
            now = time.time()
            if (
                config.BOTTOM_REFRESH_SECONDS > 0
                and now - last_bottom_refresh >= config.BOTTOM_REFRESH_SECONDS
            ):
                last_bottom_refresh = now
                before_signature = w.message_sequence_signature(cur)
                try:
                    refreshed, bottom_confirmed = chat.settle_to_bottom()
                    if not bottom_confirmed:
                        if time.time() - last_bottom_error_at > 30:
                            print(f"[{_now()}] ! 保险滚动后消息仍在变化，" f"暂时不处理这一屏")
                            last_bottom_error_at = time.time()
                        continue
                    after_signature = w.message_sequence_signature(refreshed)
                    if after_signature != before_signature:
                        print(f"[{_now()}] - 聊天页未自动滚动，" f"保险滚动已拉到新消息")
                    cur = refreshed
                except Exception as exc:  # noqa: BLE001
                    if time.time() - last_bottom_error_at > 30:
                        print(f"[{_now()}] ! 保险滚动失败（稍后重试）：{exc}")
                        last_bottom_error_at = time.time()

            # 只处理上次尾部锚点之后的新增区间。RuntimeId 在虚拟列表重建时
            # 会变化，因此整屏内容签名只用于判断变化，不用它累积永久 seen 集。
            update = monitor.inspect(cur)
            current_tail = update.current_tail
            fresh = []
            if update.changed:
                fresh = update.fresh
                anchor_found = update.anchor_found
                if not anchor_found and fresh:
                    print(f"[{_now()}] - 上一次尾部消息已不在当前屏，" f"保守处理当前最后一条")

                if fresh:
                    last_beat = time.time()
                    last_bottom_refresh = time.time()
                    classified: list[w.ChatMessage] = []
                    unresolved = False
                    for item in fresh:
                        key = item.key()
                        sender = _classify_one(chat, cap, item, sent_replies)
                        if sender is None:
                            n = attempts.get(key, 0) + 1
                            attempts[key] = n
                            if n <= config.MESSAGE_CLASSIFY_RETRIES:
                                print(
                                    f"[{_now()}] - 这行判不出是谁发的，"
                                    f"稍后重试"
                                    f"（{n}/{config.MESSAGE_CLASSIFY_RETRIES}）: "
                                    f"{_plain(item)}"
                                )
                                unresolved = True
                            else:
                                print(f"[{_now()}] - 忽略（无法判定发送方）: " f"{_plain(item)}")
                                attempts.pop(key, None)
                            continue
                        attempts.pop(key, None)
                        classified.append(item.with_sender(sender))

                    # 一批消息必须整批判清后再提交，避免下一轮重复入队。
                    if not unresolved:
                        final_tail = current_tail
                        for msg in classified:
                            if current_tail is not None and msg.key() == current_tail.key():
                                final_tail = msg
                            if msg.sender == OUTGOING:
                                # 刚发送的回复已经提交到上下文；等 UIA 回显
                                # 出现时只清掉 echo 标记，不能再追加一遍。
                                if msg.kind == "text" and sent_replies.consume_echo(msg.text):
                                    print(f"[{_now()}] → 我方: {_plain(msg)}" f"（发送回显）")
                                else:
                                    ctx.record(msg, OUTGOING)
                                    print(f"[{_now()}] → 我方: {_plain(msg)}")
                                continue

                            if msg.kind == "voice":
                                print(f"[{_now()}] - 收到对方语音，" f"正在转文字…")
                                voice_started = time.time()
                                try:
                                    msg = chat.transcribe_voice(
                                        msg,
                                        retries=config.VOICE_TRANSCRIBE_RETRIES,
                                        retry_wait=config.VOICE_RETRY_WAIT,
                                    )
                                    print(
                                        f"[{_now()}] - 语音转文字完成"
                                        f"（{time.time() - voice_started:.2f}s）"
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    print(f"[{_now()}] ! 语音转文字失败：{exc}")
                                    msg = msg.with_transcript("（转写失败）")
                                if current_tail is not None and msg.key() == current_tail.key():
                                    final_tail = msg

                            pending_followups.append(msg)
                            if generation is not None:
                                print(
                                    f"[{_now()}] ← 对方: {_plain(msg)}"
                                    f"（生成期间收到，已加入待回复队列）"
                                )
                            else:
                                print(f"[{_now()}] ← 对方: {_plain(msg)}" f"（已加入待回复队列）")

                        monitor.commit(cur, final_tail)

            # 旧回复完成时先由主线程发送，成功提交上下文后才允许处理队列
            # 里的新消息。这样第二条提示词天然包含第一条及其回复。
            now = time.time()
            if generation is not None and generation.future.done():
                job = generation
                generation = None
                key = job.msg.key()
                try:
                    reply, image_used = job.future.result()
                except DeepSeekError as exc:
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        f"调用 DeepSeek 失败：{exc}",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue
                except Exception as exc:  # noqa: BLE001
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        f"后台生成异常：{exc}",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue

                if not reply:
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        "DeepSeek 返回空内容",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue

                reply, sticker_id = _split_reply(reply)
                reply = w.fit_outgoing(reply, config.MAX_REPLY_CHARS)
                if not reply:
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        "回复清洗后为空",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue

                try:
                    send_result = _send_reply_parts(chat, reply, sticker_id)
                    ok = send_result.text_sent
                except Exception as exc:  # noqa: BLE001
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        f"发送回复失败：{exc}",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue

                finished_at = time.time()
                cost = finished_at - job.started_at
                if ok:
                    last_reply_at = finished_at
                    generation_failures.pop(key, None)
                    ctx.record(job.msg, INCOMING)
                    _remember_sent_reply(ctx, reply, sent_replies)
                    if send_result.sticker_sent:
                        if send_result.sticker_id:
                            ctx.remember_sticker(send_result.sticker_id)
                        replies_since_sticker = 0
                    else:
                        replies_since_sticker += 1

                slow = cost > config.SLOW_REPLY_SECONDS
                if ai.last_search_query:
                    flag = "慢" if slow else "联网"
                    search_note = (
                        f"，联网「{ai.last_search_query}」"
                        f"{ai.last_search_count} 条，"
                        f"搜索 {ai.last_search_elapsed:.2f}s"
                    )
                else:
                    flag = "慢" if slow else "OK"
                    search_note = ""
                image_note = "，图片上下文" if image_used else ""
                if send_result.sticker_sent:
                    sticker_note = f"，原生贴纸已发送（{send_result.sticker_id}）"
                elif send_result.sticker_attempted:
                    sticker_note = f"，贴纸未发送（{send_result.sticker_error or '未确认'}）"
                else:
                    sticker_note = ""
                print(
                    f"[{job.tag}] → {SENDER_LABEL['out']}: {reply}   "
                    f"（{flag} 消息到回复 {cost:.2f}s，"
                    f"模型 {ai.last_elapsed:.2f}s{search_note}{image_note}"
                    f"{sticker_note}，"
                    f"发送{'成功' if ok else '未确认'}）"
                )
                if ai.last_search_error:
                    print(f"[{job.tag}]   ! 搜索提示：" f"{ai.last_search_error[:200]}")
                if not ok:
                    retry_at = _note_generation_failure(
                        ctx,
                        job.msg,
                        job.tag,
                        "发送结果未确认",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue
                if pending_followups:
                    print(
                        f"[{job.tag}] - 原回复已先发送；待回复队列还有 "
                        f"{len(pending_followups)} 条，接下来按顺序生成"
                    )

            # 同一时刻只允许一个生成任务。它完成并发送成功后，下一条消息
            # 才能在包含上一条回复的上下文中开始生成。
            now = time.time()
            cooldown_ready = (
                not config.REPLY_COOLDOWN or now - last_reply_at >= config.REPLY_COOLDOWN
            )
            if generation is None and pending_followups and now >= ai_retry_at and cooldown_ready:
                msg = pending_followups.popleft()
                tag = _now()
                job_started = time.time()
                sticker_due = (
                    sticker_catalog.ENABLED
                    and replies_since_sticker >= config.STICKER_MIN_REPLY_GAP
                )
                recent_sticker_ids = ctx.recent_sticker_ids()
                try:
                    messages, image_used, fallback_text = _prepare_ai_request(
                        prompt,
                        ctx,
                        msg,
                        cap,
                        chat.viewport,
                        tag,
                        resolve_rect=lambda m=msg: _current_visual_rect(chat, m),
                        sticker_due=sticker_due,
                        recent_sticker_ids=recent_sticker_ids,
                    )
                    future = executor.submit(
                        _complete_ai_request,
                        ai,
                        messages,
                        image_used,
                        fallback_text,
                        tag,
                        sticker_due=sticker_due,
                        recent_sticker_ids=recent_sticker_ids,
                    )
                except Exception as exc:  # noqa: BLE001
                    retry_at = _note_generation_failure(
                        ctx,
                        msg,
                        tag,
                        f"准备模型输入失败：{exc}",
                        generation_failures,
                        pending_followups,
                        now,
                    )
                    if retry_at is not None:
                        ai_retry_at = retry_at
                    continue
                generation = GenerationJob(msg, future, job_started, tag, sticker_due)
                print(f"[{tag}] - 正在后台生成回复：{_plain(msg)}")

            if (
                not fresh
                and generation is None
                and not pending_followups
                and config.HEARTBEAT_SECONDS
                and time.time() - last_beat >= config.HEARTBEAT_SECONDS
            ):
                last_beat = time.time()
                print(f"[{_now()}] 监听中…（已运行 " f"{int(time.time() - started_at)} 秒）")
    except KeyboardInterrupt:
        print("\n\n已停止。本次没有在本地保存任何聊天记录。")
        return 0
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        cap.release()


def _classify_one(
    chat: w.ChatWindow, cap: WindowCapture, msg: w.ChatMessage, sent_replies: SentReplyTracker
) -> str | None:
    """判定一条消息是谁发的：像素位置优先，相同文字只作兜底。

    不能先按文字匹配自己刚发的回复：对方完全可能发来同样的文字，但它仍在
    左侧。错误地先判成自己会让这条消息永远不回复，所以必须先看气泡位置；
    只有截图暂时判不出时，才用最近发送文字帮助识别自己的回显。

    判不出来就返回 None（调用方会选择不回复）——宁可漏回一条，
    也不能把自己的消息当成对方的，否则会自问自答停不下来。
    """
    if msg.rect:
        try:
            img, ox, oy = cap.grab()
            sender = cap.classify_row(img, ox, oy, msg.rect, chat.viewport)
            if sender is not None:
                return sender
        except Exception:  # noqa: BLE001
            pass
    if msg.kind == "text" and msg.text in sent_replies.recent(5):
        return OUTGOING
    return None


def _reply_once(
    chat: w.ChatWindow,
    ctx: Context,
    ai: DeepSeek,
    prompt: str,
    trigger: w.ChatMessage,
    sent_replies: SentReplyTracker,
    tag: str,
    cap: WindowCapture | None = None,
) -> bool:
    """按"对方的新消息 = trigger"走一遍完整回复流程（自检和补发共用）。"""
    print(f"{tag} 要回复的消息：{_plain(trigger)}")
    t0 = time.time()
    try:
        reply, image_used = _ask_ai(
            ai,
            prompt,
            ctx,
            trigger,
            cap,
            chat.viewport,
            tag,
            resolve_rect=lambda: _current_visual_rect(chat, trigger),
        )
    except DeepSeekError as exc:
        print(f"{tag} 调用 DeepSeek 失败：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"{tag} 生成回复异常：{exc}")
        return False
    reply, sticker_id = _split_reply(reply)
    reply = w.fit_outgoing(reply, config.MAX_REPLY_CHARS)
    if not reply:
        print(f"{tag} ! 回复清洗后为空，跳过")
        return False
    try:
        send_result = _send_reply_parts(chat, reply, sticker_id)
        ok = send_result.text_sent
    except Exception as exc:  # noqa: BLE001
        print(f"{tag} ! 发送回复失败：{exc}")
        return False
    cost = time.time() - t0
    if ok:
        _remember_sent_reply(ctx, reply, sent_replies)
        if send_result.sticker_sent and send_result.sticker_id:
            ctx.remember_sticker(send_result.sticker_id)
    search_note = ""
    if ai.last_search_query:
        search_note = (
            f"，联网「{ai.last_search_query}」"
            f"{ai.last_search_count} 条，"
            f"搜索 {ai.last_search_elapsed:.2f}s"
        )
    image_note = "，图片上下文" if image_used else ""
    if send_result.sticker_sent:
        sticker_note = f"，原生贴纸已发送（{send_result.sticker_id}）"
    elif send_result.sticker_attempted:
        sticker_note = f"，贴纸未发送（{send_result.sticker_error or '未确认'}）"
    else:
        sticker_note = ""
    print(
        f"{tag} → {SENDER_LABEL['out']}: {reply}   （模型 {ai.last_elapsed:.2f}s"
        f"{search_note}{image_note}{sticker_note}，"
        f"总耗时 {cost:.2f}s，发送{'成功' if ok else '未确认'}）"
    )
    if ai.last_search_error:
        print(f"{tag} ! 搜索提示：{ai.last_search_error[:200]}")
    return ok


def _current_visual_rect(chat: w.ChatWindow, msg: w.ChatMessage) -> tuple | None:
    """视觉图片加载后行高可能变化，截图前按 RuntimeId 再取一次最新矩形。"""
    try:
        for current in chat.messages():
            if current.key() == msg.key():
                return current.rect
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_prompt() -> str:
    p = Path(__file__).with_name(config.PROMPT_FILE)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptError(f"读取系统提示词 {p.name} 失败：{exc}") from exc
    if not text.strip():
        raise PromptError(f"系统提示词 {p.name} 为空")
    return text


def _now() -> str:
    return time.strftime("%H:%M:%S")


def send_sticker_once(contact: str, sticker_id: str) -> int:
    """手动发送一张目录中的原生贴纸，用于更新目录后的联调。"""
    spec = sticker_catalog.STICKERS_BY_ID.get(sticker_id)
    if spec is None:
        choices = "、".join(item.id for item in sticker_catalog.STICKERS)
        print(f"✗ 未知贴纸 ID：{sticker_id}\n可选：{choices}")
        return 2
    print(f"正在打开「{contact}」的独立聊天窗口 ...")
    try:
        chat = wechat_session.open_contact_chat(contact)
        print(f"正在后台发送收藏贴纸：{spec.id}（{spec.label}）...")
        ok = chat.send_sticker(
            spec.index,
            panel_timeout=config.STICKER_PANEL_TIMEOUT,
            send_timeout=config.STICKER_SEND_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 原生贴纸发送失败：{exc}")
        return 4
    if not ok:
        print("✗ 点击了贴纸，但没有等到消息列表里的原生贴纸回显。")
        return 4
    print("✓ 原生贴纸已发送，前台窗口已还原。")
    return 0


def configure_api_key() -> int:
    """交互设置本机 DPAPI 加密密钥，不把输入回显到终端。"""
    try:
        value = getpass.getpass(" DeepSeek API Key（输入不回显）: ").strip()
        if not value:
            print("✗ 没有输入 API Key。")
            return 1
        path = save_secret(value)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1
    except SecretStoreError as exc:
        print(f"✗ {exc}")
        return 2
    print(f"✓ API Key 已用 Windows DPAPI 加密保存：{path.name}")
    print("  打开新的启动进程后生效。")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="微信后台自动回复")
    parser.add_argument("contact", nargs="?", help="联系人名字（微信备注名）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--selftest", action="store_true", help="启动后立刻用最后一条聊天记录走一遍完整回复流程"
    )
    action.add_argument(
        "--reply-pending", action="store_true", help="启动时若最近一条是对方发的消息，先补回一条"
    )
    action.add_argument(
        "--send-sticker", metavar="ID", help="只向联系人发送一张目录中的原生收藏贴纸"
    )
    action.add_argument(
        "--set-api-key", action="store_true", help="用 Windows DPAPI 加密保存 DeepSeek API Key"
    )
    action.add_argument("--configure", action="store_true", help="打开可视化个性与密钥配置")
    args = parser.parse_args()
    if args.configure:
        from settings_ui import launch

        return launch()
    if args.set_api_key:
        return configure_api_key()
    contact = (args.contact or "").strip() or ask_contact_name()
    if not contact:
        print("没有输入联系人名字，退出。")
        return 1
    if args.send_sticker:
        return send_sticker_once(contact, args.send_sticker)
    return run(contact, selftest=args.selftest, reply_pending=args.reply_pending)


if __name__ == "__main__":
    sys.exit(main())
