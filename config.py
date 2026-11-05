"""配置。聊天记录只存在内存里，不会写入磁盘。"""

import os

from secret_store import SecretStoreError, load_secret
from user_settings import ProfileError, UserProfile, build_profile_prompt, load_profile

PROFILE_LOAD_ERROR = ""
try:
    USER_PROFILE = load_profile()
except ProfileError as exc:
    USER_PROFILE = UserProfile()
    PROFILE_LOAD_ERROR = str(exc)

OWNER_NAME = USER_PROFILE.name
PROFILE_PROMPT = build_profile_prompt(USER_PROFILE)

SECRET_LOAD_ERROR = ""
try:
    _STORED_DEEPSEEK_API_KEY = load_secret()
except SecretStoreError as exc:
    _STORED_DEEPSEEK_API_KEY = ""
    SECRET_LOAD_ERROR = str(exc)

# ------------------------------------------------------------------ DeepSeek
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip() or _STORED_DEEPSEEK_API_KEY
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_THINKING = False  # 闲聊要低延迟；关闭思考模式后才轮到 temperature 生效
MAX_TOKENS = 300  # 质量优先，给最多 100 字的回复留足生成空间
TEMPERATURE = 1.3  # DeepSeek 官方建议闲聊场景用 1.3
REQUEST_TIMEOUT = 20.0

# ------------------------------------------------------------------ 联网搜索
SEARCH_ENABLED = USER_PROFILE.search_enabled
SEARCH_COUNT = 8  # 每次最多取几条结果（按请求计费，不按条数）
SEARCH_TIMEOUT = 6.0  # 搜索与重试的总预算，不含模型调用
VISION_ENABLED = USER_PROFILE.vision_enabled
VISION_MAX_SIDE = 1024  # 发给模型的截图最长边，兼顾清晰度和请求大小
VISION_MEMORY_LIMIT = USER_PROFILE.vision_memory_limit  # 运行期保留的历史图片数量
VISION_RENDER_WAIT = 0.4  # 微信先插入控件再异步渲染，先等一帧再截图
VISION_RETRY_WAIT = 0.35  # 疑似仍在转圈时的重试间隔
VISION_CAPTURE_RETRIES = 2  # 首次截图后最多再重试几次

# ------------------------------------------------------------------ 原生贴纸
STICKER_ENABLED = USER_PROFILE.sticker_enabled  # 在“功能开关”页启用并粘贴目录
STICKER_SELECTOR_ENABLED = True  # 用独立的轻量模型判断语境并选择贴纸
STICKER_STRICTNESS = USER_PROFILE.sticker_strictness  # 严格=none 时只发文字；宽松=补选一张
STICKER_MIN_REPLY_GAP = USER_PROFILE.sticker_min_reply_gap
STICKER_DIVERSITY_WINDOW = USER_PROFILE.sticker_diversity_window
STICKER_SELECTOR_MAX_TOKENS = 16
STICKER_SELECTOR_TEMPERATURE = 0.25
STICKER_PANEL_TIMEOUT = 2.0  # 等待“自定义表情”面板出现的秒数
STICKER_SEND_TIMEOUT = 2.0  # 点击贴纸后等待聊天回显的秒数

# ------------------------------------------------------------------ 行为参数
VOICE_TRANSCRIBE_RETRIES = 2  # 微信明确报转写失败后，重新发起的次数
VOICE_RETRY_WAIT = 0.8  # 给微信留出准备语音数据的时间
HISTORY_LIMIT = USER_PROFILE.history_limit  # 启动时读取最近多少条聊天记录（0 = 不读取）
RUNTIME_HISTORY_LIMIT = 40  # 本次运行保留的最近往来条数，避免长期运行无限增长
POLL_INTERVAL = 0.15  # 轮询新消息的间隔（秒）
REPLY_COOLDOWN = 0.0  # 两条回复之间的最小间隔（秒），0 = 不限制
MESSAGE_CLASSIFY_RETRIES = 5  # 单条消息无法判定发送方时最多重试几次
GENERATION_RETRIES = 5  # 生成、准备输入或发送失败时最多尝试几次
BOTTOM_REFRESH_SECONDS = 1.5  # 即使判定在底部也定期轻推，兜底微信不自动滚动；0 = 关闭
STARTUP_BOTTOM_TIMEOUT = 20.0  # 启动时等待聊天页稳定到底部的最长秒数，超时安全退出
LABEL_HISTORY_SENDERS = True  # 历史记录也标注「对方/我」，给 AI 更完整的上下文
MAX_REPLY_CHARS = 100  # 回复长度硬上限；提示词要求日常 15~50 字、复杂问题最多 100 字
SLOW_REPLY_SECONDS = 10.0  # 超过这个耗时才标记「慢」，不设硬性中断
HEARTBEAT_SECONDS = 60  # 每隔多久打印一次"监听中"（0 = 不打印）
RESTORE_IF_MINIMIZED = True  # 聊天窗口被最小化/藏起来时自动还原（不会抢前台）

# ------------------------------------------------------------------ 提示词
PROMPT_FILE = "prompt.txt"
