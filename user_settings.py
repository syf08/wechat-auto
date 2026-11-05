"""用户个性化配置的读取、保存和提示词生成。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROFILE_FILENAME = "user_config.json"
PROFILE_VERSION = 9

REPLY_LENGTHS = ("简洁", "适中", "详尽")
STICKER_STRICTNESS_LEVELS = ("严格", "宽松")
_STICKER_FIELD_RE = re.compile(r"\s*[|｜]\s*")
_STICKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ProfileError(RuntimeError):
    pass


class StickerCatalogError(ValueError):
    """用户粘贴的贴纸目录不符合约定格式。"""


@dataclass(frozen=True)
class StickerEntry:
    id: str
    label: str
    index: int


@dataclass(frozen=True)
class UserProfile:
    name: str = "我"
    humor: int = 45
    enthusiasm: int = 55
    directness: int = 55
    warmth: int = 65
    reply_length: str = "适中"
    custom_instructions: str = ""
    search_enabled: bool = True
    vision_enabled: bool = True
    vision_memory_limit: int = 8
    history_limit: int = 20
    sticker_enabled: bool = False
    sticker_diversity_window: int = 6
    sticker_strictness: str = "严格"
    sticker_min_reply_gap: int = 0
    sticker_catalog: str = ""


def default_profile_path() -> Path:
    return Path(__file__).with_name(PROFILE_FILENAME)


def _bounded_int(value: Any, default: int, minimum: int = 0, maximum: int = 100) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _choice(value: Any, options: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    return text if text in options else default


def normalize_profile(profile: UserProfile) -> UserProfile:
    name = " ".join((profile.name or "").split())[:24] or "我"
    custom = (profile.custom_instructions or "").strip()[:1200]
    sticker_catalog = str(profile.sticker_catalog or "").strip()
    return UserProfile(
        name=name,
        humor=_bounded_int(profile.humor, 45),
        enthusiasm=_bounded_int(profile.enthusiasm, 55),
        directness=_bounded_int(profile.directness, 55),
        warmth=_bounded_int(profile.warmth, 65),
        reply_length=_choice(profile.reply_length, REPLY_LENGTHS, "适中"),
        custom_instructions=custom,
        search_enabled=bool(profile.search_enabled),
        vision_enabled=bool(profile.vision_enabled),
        vision_memory_limit=_bounded_int(
            profile.vision_memory_limit,
            8,
            maximum=20,
        ),
        history_limit=_bounded_int(
            profile.history_limit,
            20,
            maximum=50,
        ),
        sticker_enabled=bool(profile.sticker_enabled),
        sticker_diversity_window=_bounded_int(
            profile.sticker_diversity_window,
            6,
            maximum=30,
        ),
        sticker_strictness=_choice(
            profile.sticker_strictness, STICKER_STRICTNESS_LEVELS, "严格"
        ),
        sticker_min_reply_gap=_bounded_int(
            profile.sticker_min_reply_gap,
            0,
            maximum=20,
        ),
        sticker_catalog=sticker_catalog,
    )


def parse_sticker_catalog(text: str) -> tuple[StickerEntry, ...]:
    """解析配置窗口中的贴纸目录。

    每行格式：ID | 适用语义 | 收藏序号。
    收藏序号从面板右下角开始、每行从右到左，再逐行向上。
    空白行和以 # 开头的注释会被忽略。
    """
    entries: list[StickerEntry] = []
    seen_ids: dict[str, int] = {}
    seen_indexes: dict[int, int] = {}
    for line_number, raw_line in enumerate((text or "").lstrip("\ufeff").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = _STICKER_FIELD_RE.split(line)
        if len(fields) != 3:
            raise StickerCatalogError(
                f"第 {line_number} 行有 {len(fields)} 栏，应严格为 3 栏。"
                "格式：贴纸ID | 适用语义 | 收藏序号"
            )

        sticker_id = fields[0].strip().lower()
        label = fields[1].strip()
        index_text = fields[2].strip()
        if not _STICKER_ID_RE.fullmatch(sticker_id):
            raise StickerCatalogError(
                f"第 {line_number} 行的贴纸 ID 无效：{sticker_id or '（空）'}。"
                "只能使用小写英文字母、数字、下划线和短横线，且不能以符号开头。"
            )
        if not label:
            raise StickerCatalogError(f"第 {line_number} 行的适用语义不能为空。")
        if len(label) > 160:
            raise StickerCatalogError(f"第 {line_number} 行的适用语义不能超过 160 个字符。")
        try:
            index = int(index_text)
        except ValueError as exc:
            raise StickerCatalogError(
                f"第 {line_number} 行的收藏序号必须是整数：{index_text or '（空）'}"
            ) from exc
        if index < 0 or index > 100000:
            raise StickerCatalogError(
                f"第 {line_number} 行的收藏序号必须在 0～100000 之间。"
            )
        if sticker_id in seen_ids:
            raise StickerCatalogError(
                f"第 {line_number} 行与第 {seen_ids[sticker_id]} 行使用了重复的贴纸 ID："
                f"{sticker_id}"
            )
        if index in seen_indexes:
            raise StickerCatalogError(
                f"第 {line_number} 行与第 {seen_indexes[index]} 行使用了重复的收藏序号："
                f"{index}"
            )

        seen_ids[sticker_id] = line_number
        seen_indexes[index] = line_number
        entries.append(StickerEntry(sticker_id, label, index))
    return tuple(entries)


def load_profile(path: Path | None = None) -> UserProfile:
    target = path or default_profile_path()
    if not target.exists():
        return UserProfile()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"读取个性化配置 {target.name} 失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileError(f"个性化配置 {target.name} 的根节点必须是对象")
    defaults = UserProfile()
    try:
        profile = UserProfile(
            name=payload.get("name", defaults.name),
            humor=payload.get("humor", defaults.humor),
            enthusiasm=payload.get("enthusiasm", defaults.enthusiasm),
            directness=payload.get("directness", defaults.directness),
            warmth=payload.get("warmth", defaults.warmth),
            reply_length=payload.get("reply_length", defaults.reply_length),
            custom_instructions=payload.get("custom_instructions", defaults.custom_instructions),
            search_enabled=payload.get("search_enabled", defaults.search_enabled),
            vision_enabled=payload.get("vision_enabled", defaults.vision_enabled),
            vision_memory_limit=payload.get(
                "vision_memory_limit", defaults.vision_memory_limit
            ),
            history_limit=payload.get("history_limit", defaults.history_limit),
            sticker_enabled=payload.get("sticker_enabled", defaults.sticker_enabled),
            sticker_diversity_window=payload.get(
                "sticker_diversity_window", defaults.sticker_diversity_window
            ),
            sticker_strictness=payload.get(
                "sticker_strictness", defaults.sticker_strictness
            ),
            sticker_min_reply_gap=payload.get(
                "sticker_min_reply_gap", defaults.sticker_min_reply_gap
            ),
            sticker_catalog=payload.get("sticker_catalog", defaults.sticker_catalog),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProfileError(f"个性化配置 {target.name} 格式无效：{exc}") from exc
    return normalize_profile(profile)


def save_profile(profile: UserProfile, path: Path | None = None) -> Path:
    target = path or default_profile_path()
    normalized = normalize_profile(profile)
    payload = {"version": PROFILE_VERSION, **asdict(normalized)}
    temporary = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProfileError(f"保存个性化配置失败：{exc}") from exc
    return target


def build_profile_prompt(profile: UserProfile | None = None) -> str:
    """把结构化选项翻译成不会覆盖 prompt.txt 硬规则的补充说明。"""
    profile = normalize_profile(profile or UserProfile())
    name_rule = (
        "账号本人没有额外的自称名字，需要自称时使用“我”。"
        if profile.name == "我"
        else f"账号本人在微信中的名字是“{profile.name}”；需要自称时优先说“我”，不要每句都带名字。"
    )
    length_rule = {
        "简洁": "日常回复尽量 10～30 字，先给结论。",
        "适中": "日常回复一般 15～50 字，保持自然。",
        "详尽": "日常回复通常 50～100 字；复杂问题在 100 字内说清关键信息。",
    }[profile.reply_length]
    custom = (
        f"\n- 用户补充偏好：{profile.custom_instructions}" if profile.custom_instructions else ""
    )
    return f"""【用户个性化设置】
- {name_rule}
- 幽默感：{profile.humor}/100；数字越高，越适合轻轻玩梗或接梗，但不能冒犯对方。
- 热情度：{profile.enthusiasm}/100；数字越高，语气越有活力和回应感。
- 直接程度：{profile.directness}/100；数字越高，表达越直接、少铺垫。
- 温柔度：{profile.warmth}/100；数字越高，越注意照顾对方情绪。
- 回复长度：{length_rule}
{custom}

个性化设置只影响语气和措辞，不能覆盖 prompt.txt 中关于真实性、安全、格式、
不暴露 AI 身份以及不编造个人事实的硬性要求。"""
