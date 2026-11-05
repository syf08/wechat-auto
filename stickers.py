"""读取配置窗口中粘贴的微信“自定义表情”收藏贴纸目录。"""

from __future__ import annotations

import re
from collections.abc import Iterable

import user_settings

StickerSpec = user_settings.StickerEntry

try:
    _PROFILE = user_settings.load_profile()
    STICKERS = user_settings.parse_sticker_catalog(_PROFILE.sticker_catalog)
    CATALOG_LOAD_ERROR = ""
except (user_settings.ProfileError, user_settings.StickerCatalogError) as exc:
    _PROFILE = user_settings.UserProfile()
    STICKERS: tuple[StickerSpec, ...] = ()
    CATALOG_LOAD_ERROR = str(exc)

STICKERS_BY_ID = {item.id: item for item in STICKERS}
ENABLED = bool(_PROFILE.sticker_enabled and STICKERS)

_STICKER_DIRECTIVE_RE = re.compile(
    r"(?:"
    r"\[\[\s*(?:原生表情|sticker)\s*[:：]\s*([a-z0-9_-]+)\s*\]\]"
    r"|"
    r"【\s*(?:原生表情|sticker)\s*[:：]\s*([a-z0-9_-]+)\s*】"
    r")",
    flags=re.IGNORECASE,
)


def _recent_set(recent_ids: Iterable[str] | None) -> set[str]:
    """只保留目录中的近期贴纸 ID。"""
    return {
        str(sticker_id).strip().lower()
        for sticker_id in (recent_ids or ())
        if str(sticker_id).strip().lower() in STICKERS_BY_ID
    }


def eligible_stickers(
    recent_ids: Iterable[str] | None = None,
) -> tuple[StickerSpec, ...]:
    """返回本轮可选贴纸；近期用过的贴纸不参与选择。"""
    recent = _recent_set(recent_ids)
    return tuple(item for item in STICKERS if item.id not in recent)


def sticker_fallback_prompt(recent_ids: Iterable[str] | None = None) -> str:
    """严格选择器返回 none 后，用较宽松的标准再匹配一次。"""
    candidates = eligible_stickers(recent_ids)
    if not candidates:
        return "本轮没有可使用的贴纸，只输出 none。不要解释。"
    catalog = "\n".join(f"- {item.id}: {item.label}" for item in candidates)
    return f"""你是微信聊天回复的宽松贴纸补选器。
第一位严格选择器认为没有完美贴纸。用户选择了宽松模式，因此请尽量选一张，
但如果没有一张能自然使用，也可以不选。

请结合完整聊天上下文和“我的文字回复”，从下面近期未使用的候选里选择相对最贴合
当前语气、情绪或动作的一张。允许“基本合适”或只有较弱关联，不需要完美匹配。
只要贴纸不会明显改变原意，不会造成冷漠、暴怒、过分暧昧或与当前情绪方向相反，
就应尽量选择一张。如果聊天很平淡，优先选择温和、中性或轻量的回应贴纸。

只有所有候选都会明显破坏当前语气或语境时，才输出 none。
不要选择候选目录以外的 ID，不要解释。只输出一个贴纸 ID，或输出 none。

目录：
{catalog}
"""


def sticker_prompt_block(
    allowed: bool = True,
    recent_ids: Iterable[str] | None = None,
    dedicated_selector: bool = True,
) -> str:
    """把本轮贴纸策略加入正文模型提示词。

    默认由独立选择器决定贴纸，正文模型只写文字，避免正文里常见的大笑
    表情成为固定锚点。关闭独立选择器时仍保留原来的行内选择能力。
    """
    if dedicated_selector:
        lines = [
            "【原生收藏表情】",
            "贴纸会由后续独立步骤根据完整语境选择。本轮正文只写文字，",
            "不要输出 `[[原生表情:...]]` 或其他贴纸标记。",
        ]
        if not allowed:
            lines.append("本轮距离上一次原生贴纸太近，后续也不要选择贴纸。")
        return "\n".join(lines)

    candidates = eligible_stickers(recent_ids)
    lines = [
        "【原生收藏表情】",
        "你可以在文字回复末尾附加且最多附加一个贴纸指令，格式必须是",
        "`[[原生表情:贴纸ID]]`。先判断回复的核心情绪或动作，只选语义直接",
        "贴合的贴纸；不能只因为聊天轻松就默认选大笑。事实回答、严肃内容、",
        "争执、道歉、安慰重大事件或语义不够贴合时不加。",
        "近期用过的贴纸已经排除；有多张同类贴纸时优先换一张。",
        "指令不算正文，不要解释它，也不要输出目录外的 ID。",
        "可选贴纸：",
    ]
    if candidates:
        lines.extend(f"- `{item.id}`：{item.label}" for item in candidates)
    else:
        lines.append("本轮没有未使用的贴纸，不要附加贴纸指令。")
    if not allowed:
        lines.append("本轮距离上一次原生贴纸太近，不要附加贴纸指令。")
    return "\n".join(lines)


def sticker_selector_prompt(
    recent_ids: Iterable[str] | None = None,
) -> str:
    """用于正文生成后的独立贴纸选择。"""
    recent = _recent_set(recent_ids)
    candidates = eligible_stickers(recent)
    if not candidates:
        return "本轮没有可使用的贴纸，只输出 none。不要解释。"
    catalog = "\n".join(f"- {item.id}: {item.label}" for item in candidates)
    recent_note = (
        "本轮已排除最近成功发送过的贴纸："
        + "、".join(item for item in STICKERS_BY_ID if item in recent)
        + "。不要在输出里提到它们。"
        if recent
        else "目前没有需要排除的近期贴纸。"
    )
    return f"""你是微信聊天回复的原生贴纸选择器。
根据聊天上下文和“我的文字回复”，从目录里选一张能自然补充语气或情绪的贴纸。

选择顺序：
1. 先找出正文和最相关上下文里明确的核心情绪或动作，比如开心、无语、感谢、
   认同、困倦、心动、惊讶。不要被更早的无关聊天带偏。
2. 只在语义直接匹配这张贴纸的适用情绪时选择。不能只凭“气氛轻松”就挑一张
   大笑贴纸，也不能用随机轮换来牺牲语境。
3. 优先选恰好表达当前情绪强度的贴纸。温和场景别用暴怒、痛哭、强烈暧昧或
   节日专属贴纸；除非上下文明确支持。
4. 如果只有“差不多”“应该也能用”的候选，输出 none。宁可不配，也不要硬凑。
5. 贴纸 ID 只是标记，不要根据 ID 里的单词猜图片内容；必须以“适用语义”和
   当前聊天语境为准。

以下情况通常输出 none：事实结论或知识问答、工作/学习上的严肃建议、争执、
道歉、安慰重大事件、对方明显低落时需要认真安慰、以及正文没有明显情绪或动作。
轻松且有明确情绪的熟人闲聊可以正常选择。

{recent_note}
只输出一个贴纸 ID，或输出 none。不要解释。

目录：
{catalog}
"""


def parse_sticker_choice(
    text: str,
    recent_ids: Iterable[str] | None = None,
) -> str | None:
    """解析选择器的单个 ID；近期用过的 ID 不会被接受。"""
    value = (text or "").strip().lower()
    if not value or "none" in value:
        return None
    for item in eligible_stickers(recent_ids):
        if re.search(rf"(?<![a-z0-9_-]){re.escape(item.id)}" rf"(?![a-z0-9_-])", value):
            return item.id
    return None


def split_sticker_directive(reply: str) -> tuple[str, str | None]:
    """拆出正文和最后一个可用贴纸 ID；标记本身绝不会发给联系人。"""
    text = reply or ""
    matches = list(_STICKER_DIRECTIVE_RE.finditer(text))
    if not matches:
        return text.strip(), None
    sticker_id = None
    for match in reversed(matches):
        candidate = (match.group(1) or match.group(2) or "").strip().lower()
        if candidate in STICKERS_BY_ID:
            sticker_id = candidate
            break
    text = " ".join(_STICKER_DIRECTIVE_RE.sub("", text).split())
    if sticker_id not in STICKERS_BY_ID:
        sticker_id = None
    return text, sticker_id
