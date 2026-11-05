"""微信 4.x（mmui 框架）纯 UI 自动化 —— 后台读取与回复。

文本收发全程不抢前台；原生收藏贴纸会短暂打开微信表情弹层，点击后还原前台。

设计要点（全部基于微信 4.x 的实测界面）：

* 微信 4.x 主窗口 / 独立聊天窗口 / 搜索弹层都是 Qt 窗口，但微信自己实现了
  UIA 无障碍层（类名前缀 ``mmui::``），所以能用 UI Automation 读到完整控件树。
* 所有"输入"都用向窗口 **投递**（PostMessage）键盘/鼠标消息实现，
  不移动真实光标、不调用 SendInput、不激活窗口 —— 因此不会抢占前台。
  已实测：投递 WM_CHAR 可以写入聊天输入框、投递鼠标左键可以点击
  "发送"按钮、投递 WM_MOUSEWHEEL 可以滚动历史消息，期间前台窗口不变。
* 搜索框用 UIA ValuePattern 写入，打开联系人也不主动激活窗口。配对过程仍会
  记录原前台窗口；若某个微信版本自行切换焦点，结束后会尽量还原。
"""

from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil
import uiautomation as auto

import config

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

auto.SetGlobalSearchTimeout(1.5)

# ---------------------------------------------------------------- 窗口消息常量
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_ACTIVATE = 0x0006
WM_ACTIVATEAPP = 0x001C
WM_NCACTIVATE = 0x0086
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

SW_MINIMIZE = 6
SW_SHOWNOACTIVATE = 4

# ------------------------------------------------------------------ UIA 类名
CLS_MAIN_WINDOW = "mmui::MainWindow"
CLS_CHAT_WINDOW = "mmui::ChatSingleWindow"
CLS_SEARCH_POPOVER = "mmui::SearchContentPopover"
CLS_SEARCH_FIELD = "mmui::XValidatorTextEdit"
CLS_EMOTICON_POPOVER = "mmui::EmoticonPopover"
CLS_FAV_EMOTICON_ITEM = "mmui::FavEmoticonItemView"
CLS_EMOTICON_TAB_ITEM = "mmui::EmoticonToolbarItem"
CLS_EMOTICON_GRID = "mmui::EmoticonGridView"
CLS_TEXT_ITEM = "mmui::ChatTextItemView"
CLS_TIME_ITEM = "mmui::ChatItemView"
CLS_VOICE_ITEM = "mmui::ChatVoiceItemView"
CLS_MENU = "mmui::XMenu"
CLS_MENU_ITEM = "mmui::XMenuView"

AID_SESSION_LIST = "session_list"
AID_MESSAGE_LIST = "chat_message_list"
AID_CHAT_INPUT = "chat_input_field"

MAIN_WINDOW_TITLE = "微信"
MENU_VOICE_TO_TEXT = "语音转文字"
TAB_FAVORITE_STICKERS = "自定义表情"
BUTTON_EMOTICON = "发送表情(Alt+E)"

# 转写结果不设时限：长语音或服务端较慢时只继续等，不能凭经过时间判失败。
# 下面这两个短等待只用于确认“右键菜单是否真的弹出来”，失败后会重试并明确报错。
_VOICE_MENU_WAIT = 2.0
_VOICE_MENU_RETRIES = 3
_VOICE_POLL_INTERVAL = 0.2

_VOICE_NAME_RE = re.compile(r'^语音(?P<seconds>\d+(?:\.\d+)?)"秒(?P<body>.*)$')
_VOICE_WAITING_BODIES = frozenset(
    {
        "",
        "未读",
        "未播放",
        "播放中",
        "正在播放",
        "暂停",
        "已暂停",
        "加载中",
    }
)
_VOICE_FAILED_BODIES = frozenset(
    {
        "转文字失败",
        "语音转文字失败",
        "转换失败",
        "转写失败",
    }
)

# 这些顶层窗口不承载界面（纯消息窗口 / 输入法 / 托盘），越过后再去问它们的
# UIA 树会永久阻塞（实测 Base_PowerMessageWindow 会卡死），直接跳过。
_SKIP_CLASSES = {
    "IME",
    "MSCTFIME UI",
    "Chrome_SystemMessageWindow",
    "DisplayICC_SystemMessageWindow",
    "TooltipWindow",
}
_SKIP_KEYWORDS = ("TrayIcon", "PowerMessage")


def configure_uia(connection_timeout_ms: int = 2500, transaction_timeout_ms: int = 2500) -> bool:
    """给 UIA 调用加超时，避免个别无响应的控件把脚本永久卡住。"""
    try:
        client = auto._AutomationClient.instance()
        core = client.UIAutomationCore
        uia2 = client.IUIAutomation.QueryInterface(core.IUIAutomation2)
        uia2.ConnectionTimeout = connection_timeout_ms
        uia2.TransactionTimeout = transaction_timeout_ms
        return True
    except Exception:
        return False


configure_uia()


# ==================================================================== 窗口查找
def weixin_pids() -> set[int]:
    out = set()
    for pr in psutil.process_iter(["pid", "name"]):
        name = pr.info["name"]
        if name and name.lower() in ("weixin.exe", "wechat.exe"):
            out.add(pr.info["pid"])
    return out


def top_windows(ui_only: bool = True) -> list[tuple[int, int, str, str]]:
    """本机微信进程的顶层窗口 (hwnd, pid, class, title)。

    ui_only=True 时只返回真正承载界面的窗口，避免碰到会让 UIA 卡死的辅助窗口。
    """
    pids = weixin_pids()
    res: list[tuple[int, int, str, str]] = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            if ui_only and (
                cls.value in _SKIP_CLASSES or any(k in cls.value for k in _SKIP_KEYWORDS)
            ):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            u32.GetWindowTextW(hwnd, buf, n + 2)
            res.append((hwnd, pid.value, cls.value, buf.value))
        return True

    u32.EnumWindows(CB(cb), 0)
    return res


def element(hwnd: int):
    """拿到 hwnd 对应的 UIA 元素，失败返回 None（不会抛出）。"""
    try:
        return auto.ControlFromHandle(hwnd)
    except Exception:
        return None


def find_main_window() -> auto.Control | None:
    """微信主窗口（会话列表所在的那个窗口）。"""
    for hwnd, _pid, _cls, title in top_windows():
        if title != MAIN_WINDOW_TITLE:
            continue
        c = element(hwnd)
        if c is not None and c.ClassName == CLS_MAIN_WINDOW:
            return c
    return None


def find_chat_window(title: str) -> auto.Control | None:
    """按联系人名找到独立聊天窗口（双击会话打开的窗口）。"""
    for hwnd, _pid, _cls, win_title in top_windows():
        if win_title != title:
            continue
        c = element(hwnd)
        if c is not None and c.ClassName == CLS_CHAT_WINDOW:
            return c
    return None


def find_popover() -> auto.Control | None:
    for hwnd, _pid, _cls, _title in top_windows():
        c = element(hwnd)
        if c is not None and c.ClassName == CLS_SEARCH_POPOVER:
            return c
    return None


def find_emoticon_popover() -> tuple[int, auto.Control] | None:
    """返回当前可见的微信表情面板 (hwnd, UIA control)。"""
    # top_windows() 已经先按 Win32 类名排除可能导致 UIA 卡死的辅助窗口。
    # 表情面板的 Win32 类名是 Qt...，UIA 类名才是 mmui::EmoticonPopover，
    # 两套类名不能混用比较。
    for hwnd, _pid, _win_class, _title in top_windows():
        if not u32.IsWindowVisible(hwnd):
            continue
        c = element(hwnd)
        if c is not None and c.ClassName == CLS_EMOTICON_POPOVER:
            return hwnd, c
    return None


def _favorite_sticker_controls(panel: auto.Control) -> list[auto.Control]:
    items = [
        ctrl
        for ctrl, _depth in iter_control(panel, max_depth=8)
        if ctrl.ClassName == CLS_FAV_EMOTICON_ITEM
    ]
    items.sort(
        key=lambda ctrl: (
            ctrl.BoundingRectangle.top,
            ctrl.BoundingRectangle.left,
        )
    )
    return items


def _sticker_controls_reverse_grid(controls: list[auto.Control]) -> list[auto.Control]:
    """按“最底下一行开始、每行从右到左、再逐行向上”的反向顺序排列收藏格。"""
    rows: list[list[auto.Control]] = []
    for ctrl in sorted(
        controls,
        key=lambda item: (
            item.BoundingRectangle.top,
            item.BoundingRectangle.left,
        ),
    ):
        if not rows or abs(ctrl.BoundingRectangle.top - rows[-1][0].BoundingRectangle.top) > 2:
            rows.append([ctrl])
        else:
            rows[-1].append(ctrl)

    ordered: list[auto.Control] = []
    for row in reversed(rows):
        ordered.extend(
            sorted(
                row,
                key=lambda item: item.BoundingRectangle.left,
                reverse=True,
            )
        )
    return ordered


def _sticker_grid_metrics(controls: list[auto.Control]) -> tuple[int, int]:
    """从当前收藏格估算每行格数和行高，供按绝对序号翻页。"""
    if not controls:
        return 1, 1
    tops = sorted(ctrl.BoundingRectangle.top for ctrl in controls)
    first_top = tops[0]
    first_row_count = sum(abs(top - first_top) <= 2 for top in tops)
    distinct_tops = sorted({top for top in tops if top - first_top > 2})
    row_pitch = distinct_tops[0] - first_top if distinct_tops else max(1, len(controls))
    return max(1, first_row_count), max(1, row_pitch)


def _sticker_control_runtime_ids(controls: list[auto.Control]) -> tuple[tuple[int, ...] | None, ...]:
    """读取收藏格 RuntimeId，用于判断翻页后整个列表移动了多少格。"""
    result = []
    for ctrl in controls:
        try:
            raw = ctrl.GetRuntimeId()
            result.append(tuple(int(value) for value in raw) if raw else None)
        except Exception:
            result.append(None)
    return tuple(result)


def _sticker_control_shift(
    previous: tuple[tuple[int, ...] | None, ...],
    current: tuple[tuple[int, ...] | None, ...],
) -> int | None:
    """用两屏控件的重叠关系返回当前屏相对上一屏移动的格数。"""
    if not previous or not current:
        return None
    previous = tuple(item for item in previous if item is not None)
    current = tuple(item for item in current if item is not None)
    if not previous or not current:
        return None
    if current[0] in previous:
        return previous.index(current[0])
    if previous[0] in current:
        return 0
    for shift in range(len(previous)):
        tail = previous[shift:]
        if tail and current[: len(tail)] == tail:
            return shift
    return None


def _find_favorite_sticker_control(
    panel: auto.Control,
    panel_hwnd: int,
    grid: auto.Control,
    index: int,
) -> auto.Control:
    """按从右下角向左、再逐行向上的 0 起始序号定位；只滚动和读取。"""
    g = grid.BoundingRectangle
    center_x = (g.left + g.right) // 2
    center_y = (g.top + g.bottom) // 2

    # 新收藏会插到前部，旧贴纸向右、再向下移动。从右下角反向计数
    # 与这个插入方向一致，可减少从顶部新增贴纸时已有序号的偏移。
    for _ in range(12):
        post_wheel(panel_hwnd, center_x, center_y, -3)
        time.sleep(0.02)
    time.sleep(0.3)

    current_controls = _favorite_sticker_controls(panel)
    current_ordered = _sticker_controls_reverse_grid(current_controls)
    current_ids = _sticker_control_runtime_ids(current_ordered)
    current_base = 0
    for _step in range(80):
        if not current_ordered:
            break
        relative = index - current_base
        if 0 <= relative < len(current_ordered):
            target = current_ordered[relative]
            r = target.BoundingRectangle
            fully_visible = (
                r.left >= g.left - 1
                and r.right <= g.right + 1
                and r.top >= g.top - 1
                and r.bottom <= g.bottom + 1
            )
            if fully_visible:
                return target

        previous_top = min(ctrl.BoundingRectangle.top for ctrl in current_controls)
        previous_ids = current_ids
        post_wheel(panel_hwnd, center_x, center_y, 3)
        next_controls: list[auto.Control] = []
        next_ids: tuple[tuple[int, ...] | None, ...] = ()
        deadline = time.time() + 0.9
        while time.time() < deadline:
            time.sleep(0.06)
            candidate = _favorite_sticker_controls(panel)
            if not candidate:
                continue
            candidate_ordered = _sticker_controls_reverse_grid(candidate)
            candidate_ids = _sticker_control_runtime_ids(candidate_ordered)
            candidate_top = min(ctrl.BoundingRectangle.top for ctrl in candidate)
            if candidate_ids != previous_ids or candidate_top > previous_top + 1:
                next_controls = candidate
                next_ids = candidate_ids
                break
        if not next_controls:
            break
        time.sleep(0.12)
        stable_controls = _favorite_sticker_controls(panel)
        if stable_controls:
            next_controls = stable_controls
            next_ordered = _sticker_controls_reverse_grid(stable_controls)
            next_ids = _sticker_control_runtime_ids(next_ordered)

        shift = _sticker_control_shift(previous_ids, next_ids)
        if shift is None:
            # RuntimeId 不可用时才退回整行像素估算。
            columns, row_pitch = _sticker_grid_metrics(current_controls)
            next_top = min(ctrl.BoundingRectangle.top for ctrl in next_controls)
            rows_scrolled = round((next_top - previous_top) / row_pitch)
            shift = rows_scrolled * columns if rows_scrolled > 0 else None
        if shift is None:
            break
        if shift == 0 and next_ids == previous_ids:
            next_top = min(ctrl.BoundingRectangle.top for ctrl in next_controls)
            if next_top > previous_top + 1:
                # 控件集合未变，但内容仍在向下移动；继续等待上一行进入 UIA 树。
                current_controls = next_controls
                current_ordered = _sticker_controls_reverse_grid(next_controls)
                continue
            break
        current_base += shift
        current_controls = next_controls
        current_ordered = _sticker_controls_reverse_grid(next_controls)
        current_ids = next_ids

    raise RuntimeError(
        f"按收藏序号 {index} 没有找到贴纸。请确认序号从 0 开始，"
        "从收藏面板右下角开始、每行从右到左，再逐行向上编号。"
    )


# ============================================================ 后台输入原语
def _lp(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def post(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    u32.PostMessageW(hwnd, msg, wparam, lparam)


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = wintypes.POINT(x, y)
    u32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def post_click(hwnd: int, x: int, y: int) -> None:
    cx, cy = screen_to_client(hwnd, x, y)
    post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _lp(cx, cy))
    post(hwnd, WM_LBUTTONUP, 0, _lp(cx, cy))


def post_right_click(hwnd: int, x: int, y: int) -> None:
    cx, cy = screen_to_client(hwnd, x, y)
    post(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, _lp(cx, cy))
    post(hwnd, WM_RBUTTONUP, 0, _lp(cx, cy))


def post_double_click(hwnd: int, x: int, y: int) -> None:
    cx, cy = screen_to_client(hwnd, x, y)
    post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _lp(cx, cy))
    post(hwnd, WM_LBUTTONUP, 0, _lp(cx, cy))
    post(hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, _lp(cx, cy))
    post(hwnd, WM_LBUTTONUP, 0, _lp(cx, cy))


def post_wheel(hwnd: int, x: int, y: int, notches: int) -> None:
    """notches > 0 向上滚（看更早的消息），< 0 向下滚（看最新消息）。"""
    delta = 120 * notches
    post(hwnd, WM_MOUSEWHEEL, (delta << 16) & 0xFFFFFFFF, _lp(x, y))


def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


def type_text(hwnd: int, text: str, delay: float = 0.0) -> None:
    """把 text 逐字符投递给窗口（不抢前台、不动真实光标）。"""
    for unit in _utf16_units(text):
        post(hwnd, WM_CHAR, unit, 1)
        if delay:
            time.sleep(delay)


def press_key(hwnd: int, vk: int) -> None:
    post(hwnd, WM_KEYDOWN, vk, 0)
    post(hwnd, WM_KEYUP, vk, 0)


def send_enter(hwnd: int) -> None:
    press_key(hwnd, VK_RETURN)


def send_escape(hwnd: int) -> None:
    press_key(hwnd, VK_ESCAPE)


def backspace(hwnd: int, count: int) -> None:
    for _ in range(count):
        press_key(hwnd, VK_BACK)
        time.sleep(0.005)


# =========================================================== 窗口状态 / 前台
def is_minimized(hwnd: int) -> bool:
    return bool(u32.IsIconic(hwnd))


def show_no_activate(hwnd: int) -> None:
    """把窗口显示出来但不激活它（恢复最小化窗口也不会抢前台）。"""
    u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def minimize(hwnd: int) -> None:
    u32.ShowWindowAsync(hwnd, SW_MINIMIZE)


def foreground_hwnd() -> int:
    return u32.GetForegroundWindow()


def restore_foreground(hwnd: int) -> bool:
    """把前台还给 hwnd（用 AttachThreadInput 绕过 SetForegroundWindow 限制）。"""
    if not hwnd or hwnd == u32.GetForegroundWindow():
        return True
    fg = u32.GetForegroundWindow()
    fg_thread = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    my_thread = k32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != my_thread:
        attached = bool(u32.AttachThreadInput(my_thread, fg_thread, True))
    u32.BringWindowToTop(hwnd)
    ok = bool(u32.SetForegroundWindow(hwnd))
    if attached:
        u32.AttachThreadInput(my_thread, fg_thread, False)
    return ok


# ==================================================================== 控件查找
def iter_control(top: auto.Control, max_depth: int = 22):
    yield from auto.WalkControl(top, includeTop=True, maxDepth=max_depth)


def find_by_aid(top: auto.Control, aid: str, max_depth: int = 22):
    for c, _d in iter_control(top, max_depth):
        if c.AutomationId == aid:
            return c
    return None


def find_by_class(top: auto.Control, cls: str, max_depth: int = 22):
    for c, _d in iter_control(top, max_depth):
        if c.ClassName == cls:
            return c
    return None


@dataclass(frozen=True)
class ChatMessage:
    """聊天记录里的一条内容。kind: text / time / voice / other。"""

    kind: str
    text: str
    cls: str = ""
    rect: tuple | None = None  # (left, top, right, bottom) 屏幕坐标
    sender: str | None = None  # 'in' / 'out' / None(未知)
    frame_y: int = 0  # 在合并坐标系里的纵向位置（用于多轮拼页对齐）
    runtime_id: tuple[int, ...] | None = None  # UIA 元素唯一 ID
    transcript: str | None = None  # 运行期语音转写；启动历史始终不填

    def key(self) -> tuple:
        """优先用 UIA RuntimeId 作为消息身份，拿不到时退回内容。

        语音转写会让同一个控件从“未播放”变成带转写文字的 Name。若身份
        还包含 Name，转写完成时就会被误判成另一条新消息并再次分析。
        """
        if self.runtime_id is not None:
            return ("runtime", self.runtime_id)
        content_key = (self.kind, self.text, self.cls)
        return content_key

    def with_sender(self, sender: str | None) -> "ChatMessage":
        return ChatMessage(
            self.kind,
            self.text,
            self.cls,
            self.rect,
            sender,
            self.frame_y,
            self.runtime_id,
            self.transcript,
        )

    def with_transcript(self, transcript: str) -> "ChatMessage":
        return ChatMessage(
            self.kind,
            self.text,
            self.cls,
            self.rect,
            self.sender,
            self.frame_y,
            self.runtime_id,
            transcript,
        )


def _classify(ctrl) -> str:
    cls = ctrl.ClassName
    if cls == CLS_TEXT_ITEM:
        return "text"
    if cls == CLS_TIME_ITEM:
        return "time"
    if cls == CLS_VOICE_ITEM:
        return "voice"
    return "other"


class VoiceTranscriptionError(RuntimeError):
    """微信明确显示转写失败，或后台无法打开“语音转文字”菜单。"""


def _voice_name_state(name: str) -> tuple[str, str | None]:
    """把语音控件名称拆成 waiting / done / failed 状态和转写文字。"""
    match = _VOICE_NAME_RE.match(name or "")
    if match is None:
        return "waiting", None
    body = match.group("body").strip()
    if (
        body in _VOICE_WAITING_BODIES
        or body.startswith("正在转文字")
        or body.startswith("正在转换")
    ):
        return "waiting", None
    if body in _VOICE_FAILED_BODIES or body.startswith("转文字失败"):
        return "failed", None
    if not body:
        return "waiting", None
    return "done", body


def _runtime_id(ctrl) -> tuple[int, ...] | None:
    try:
        raw = ctrl.GetRuntimeId()
        if isinstance(raw, (tuple, list)) and raw:
            return tuple(int(x) for x in raw)
    except Exception:
        pass
    return None


def _find_voice_menu_item() -> tuple[int, auto.Control] | None:
    """查找微信语音右键菜单里的“语音转文字”，返回 (菜单 hwnd, 菜单项)。"""
    for hwnd, _pid, _cls, _title in top_windows():
        top = element(hwnd)
        if top is None or top.ClassName != CLS_MENU:
            continue
        for ctrl, _depth in iter_control(top, max_depth=8):
            if ctrl.ClassName == CLS_MENU_ITEM and ctrl.Name == MENU_VOICE_TO_TEXT:
                return hwnd, ctrl
    return None


class ChatWindow:
    """一个独立聊天窗口：读消息 + 发文字/原生贴纸。"""

    def __init__(self, control: auto.Control):
        self.ctrl = control
        self.hwnd = control.NativeWindowHandle
        self.title = control.Name
        self._msg_list = None
        self._input = None

    # -------------------------------------------------------------- 基础控件
    @property
    def msg_list(self):
        if self._msg_list is None:
            self._msg_list = find_by_aid(self.ctrl, AID_MESSAGE_LIST)
            if self._msg_list is None:
                raise RuntimeError("找不到消息列表 chat_message_list")
        return self._msg_list

    @property
    def input_field(self):
        if self._input is None:
            self._input = find_by_aid(self.ctrl, AID_CHAT_INPUT)
            if self._input is None:
                raise RuntimeError("找不到输入框 chat_input_field")
        return self._input

    def refresh(self) -> None:
        """窗口内容被重建后重新抓取控件。"""
        self._msg_list = None
        self._input = None
        self.ctrl = element(self.hwnd)
        if self.ctrl is None:
            raise RuntimeError("聊天窗口已关闭")

    def is_alive(self) -> bool:
        return bool(u32.IsWindow(self.hwnd))

    def ensure_visible(self) -> bool:
        """窗口被最小化或被藏起来时把它放回屏幕，返回是否真的做了动作。

        用 SW_SHOWNOACTIVATE，不会把前台抢过来。
        为什么必须做：消息列表是虚拟化的，后台截图也要靠窗口矩形——
        窗口一旦收起来，脚本就既读不到新消息、也判不出是谁发的，
        而且它自己"感觉不到"出了问题（UIA 还能返回旧的可见行）。
        """
        if u32.IsWindowVisible(self.hwnd) and not u32.IsIconic(self.hwnd):
            return False
        show_no_activate(self.hwnd)
        return True

    # ------------------------------------------------------------------ 读
    def messages(self) -> list[ChatMessage]:
        kids = self._children()
        if not kids:
            # 控件被微信重建时，旧元素会“假装”返回空列表，这里强制重抓一次
            self.refresh()
            kids = self._children()
        out = []
        for k in kids:
            try:
                r = k.BoundingRectangle
                runtime_id = None
                try:
                    raw_id = k.GetRuntimeId()
                    if isinstance(raw_id, (tuple, list)) and raw_id:
                        runtime_id = tuple(int(x) for x in raw_id)
                except Exception:
                    runtime_id = None
                out.append(
                    ChatMessage(
                        _classify(k),
                        k.Name or "",
                        k.ClassName,
                        (r.left, r.top, r.right, r.bottom),
                        runtime_id=runtime_id,
                    )
                )
            except Exception:
                continue
        return out

    def _children(self):
        try:
            return list(self.msg_list.GetChildren())
        except Exception:
            self.refresh()
            return list(self.msg_list.GetChildren())

    def _voice_control(self, msg: ChatMessage) -> auto.Control | None:
        """按 RuntimeId 找回语音控件；控件树重建时再按矩形兜底。"""
        candidates = []
        for ctrl in self._children():
            if ctrl.ClassName != CLS_VOICE_ITEM:
                continue
            candidates.append(ctrl)
            if msg.runtime_id is not None and _runtime_id(ctrl) == msg.runtime_id:
                return ctrl
        if not msg.rect:
            return None
        for ctrl in candidates:
            try:
                r = ctrl.BoundingRectangle
            except Exception:
                continue
            if abs(r.top - msg.rect[1]) <= 4 and abs(r.bottom - msg.rect[3]) <= 4:
                return ctrl
        return None

    def _voice_click_point(self, ctrl: auto.Control, sender: str | None) -> tuple[int, int]:
        """返回气泡内部的右键坐标；整行矩形的中心通常是空白区域。"""
        r = ctrl.BoundingRectangle
        width = max(1, r.right - r.left)
        pad = max(80, min(180, int(width * 0.12)))
        x = r.right - pad if sender == "out" else r.left + pad
        # 已转写语音会在波形下方展开文字，行中心可能落到文字区；统一点波形附近。
        y = r.top + max(24, min(56, (r.bottom - r.top) // 2))
        return x, y

    def _open_voice_to_text_menu(
        self, ctrl: auto.Control, sender: str | None
    ) -> tuple[int, auto.Control]:
        """右键语音气泡并返回可点击的“语音转文字”菜单项。"""
        for _attempt in range(_VOICE_MENU_RETRIES):
            x, y = self._voice_click_point(ctrl, sender)
            post_right_click(self.hwnd, x, y)
            deadline = time.time() + _VOICE_MENU_WAIT
            menu_hwnd = None
            while time.time() < deadline:
                found = _find_voice_menu_item()
                if found is not None:
                    return found
                for h, _pid, _cls, _title in top_windows():
                    top = element(h)
                    if top is not None and top.ClassName == CLS_MENU:
                        menu_hwnd = h
                        break
                time.sleep(0.05)
            if menu_hwnd is not None:
                send_escape(menu_hwnd)
                time.sleep(0.15)
        raise VoiceTranscriptionError("无法打开“语音转文字”菜单")

    def transcribe_voice(
        self, msg: ChatMessage, retries: int = 2, retry_wait: float = 0.8
    ) -> ChatMessage:
        """把一条运行期语音转成文字，返回附加了 transcript 的消息。

        启动时读取的历史语音不会调用本方法。点击转写后不设置结果超时：
        长语音和服务端较慢都可能需要等待。只有微信明确显示失败、聊天窗口
        关闭，或右键菜单无法打开时才报错。

        微信可能在语音尚未准备好时提前返回“转写失败”。不会把这种失败当成
        最终结果，而是稍微等待后重新右键发起；有限重试仍失败才交给调用方。
        """
        if msg.kind != "voice":
            return msg
        if msg.transcript:
            return msg

        state, transcript = _voice_name_state(msg.text)
        if state == "done" and transcript:
            return msg.with_transcript(transcript)

        attempts = max(0, int(retries)) + 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(max(0.0, retry_wait))

            ctrl = self._voice_control(msg)
            if ctrl is None:
                if attempt + 1 < attempts:
                    continue
                raise VoiceTranscriptionError("找不到当前语音控件")
            # 从轮询读到消息到真正操作之间，微信可能已经完成转写；即使
            # 当前仍是“失败”也重新发起一次，处理语音尚未准备好时的误失败。
            state, transcript = _voice_name_state(ctrl.Name)
            if state == "done" and transcript:
                return msg.with_transcript(transcript)

            menu_hwnd, item = self._open_voice_to_text_menu(ctrl, msg.sender)
            r = item.BoundingRectangle
            post_click(menu_hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)

            # 这里是真正的转写等待：不按经过时间判失败。只有微信明确显示
            # 失败才结束本轮，并在预算内重新发起。先让微信处理完点击，
            # 避免控件名称还没刷新时把上一轮的“失败”误当成新结果。
            time.sleep(_VOICE_POLL_INTERVAL)
            while True:
                if not self.is_alive():
                    raise VoiceTranscriptionError("等待语音转写时聊天窗口已关闭")
                ctrl = self._voice_control(msg)
                state, transcript = _voice_name_state(ctrl.Name if ctrl is not None else "")
                if state == "done" and transcript:
                    return msg.with_transcript(transcript)
                if state == "failed":
                    if attempt + 1 >= attempts:
                        raise VoiceTranscriptionError(
                            f"微信显示语音转文字失败（已尝试 {attempts} 次）"
                        )
                    break
                time.sleep(_VOICE_POLL_INTERVAL)
        raise VoiceTranscriptionError("语音转文字失败")

    def input_value(self) -> str:
        try:
            return self.input_field.GetValuePattern().Value or ""
        except Exception:
            self.refresh()
            return self.input_field.GetValuePattern().Value or ""

    @property
    def viewport(self) -> tuple:
        """消息列表的可视区域 (left, top, right, bottom)，屏幕坐标。"""
        r = self.msg_list.BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)

    def at_bottom(self, msgs: list[ChatMessage] | None = None, tol: int = 4) -> bool:
        """快速几何检查：最后一条可见行是否没有越过视口底边。

        列表是虚拟化的：只要视图不在底部，新消息就不会出现在控件树里，
        但该检查无法发现“视口下方还有尚未实例化的行”。需要可靠判断时
        使用 settle_to_bottom()。
        """
        msgs = self.messages() if msgs is None else msgs
        if not msgs:
            return True
        last = msgs[-1]
        if not last.rect:
            return True
        return last.rect[3] <= self.viewport[3] + tol

    # ------------------------------------------------------------------ 滚
    def scroll(self, notches: int, steps: int = 1, pause: float = 0.15) -> None:
        r = self.msg_list.BoundingRectangle
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        for _ in range(steps):
            post_wheel(self.hwnd, cx, cy, notches)
            time.sleep(pause)

    def scroll_to_bottom(self, steps: int = 16, settle: float = 0.25, pause: float = 0.01) -> None:
        """连续投递向下滚轮，尽快回到最新消息。

        微信 4.x 的 RecyclerListView 不暴露 UIA ScrollPattern，
        WM_VSCROLL/SB_BOTTOM 也不会作用到它；单次超大的滚轮增量会被微信
        截成一个普通滚动步。因此这里连续投递多步滚轮，最终是否真正到底
        仍由 settle_to_bottom() 确认。
        """
        self.scroll(
            -3,
            steps=max(1, int(steps)),
            pause=max(0.0, float(pause)),
        )
        time.sleep(max(0.0, float(settle)))

    def settle_to_bottom(
        self, max_steps: int = 12, settle: float = 0.12, stable_reads: int = 2
    ) -> tuple[list[ChatMessage], bool]:
        """连续向下轻推，直到消息序列连续几次完全不变。

        只检查最后一条可见消息是否贴着视口底边并不可靠：虚拟列表可能还有
        尚未实例化的新行，最后一条可见行却刚好在底边。这里以“向下滚动后
        内容不再变化”作为收敛条件，返回 (当前消息, 是否确认到底)。
        """
        stable = 0
        previous = None
        msgs = self.messages()
        for _ in range(max(1, int(max_steps))):
            signature = message_sequence_signature(msgs)
            if signature == previous:
                stable += 1
                if stable >= max(1, int(stable_reads)):
                    return msgs, True
            else:
                stable = 0
                previous = signature

            self.scroll(-1, steps=1, pause=0.03)
            time.sleep(max(0.0, settle))
            msgs = self.messages()
        return msgs, False

    def _classified(self, classify) -> list[ChatMessage]:
        msgs = self.messages()
        if classify is None:
            return msgs
        senders = list(classify([m.rect for m in msgs]))
        if len(senders) != len(msgs):
            senders = [None] * len(msgs)
        return [m.with_sender(s) for m, s in zip(msgs, senders)]

    def recent_messages(
        self, limit: int = 20, max_up: int = 14, classify=None
    ) -> list[ChatMessage]:
        """读取最近 limit 条聊天记录（不含时间分隔行），必要时向上滚动翻页。

        classify: 可选 callable([rect, ...]) -> ['in'/'out'/None, ...]，
                  一次抓屏批量判定每条消息的发送方（标注历史记录用）。
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []
        self.settle_to_bottom()
        seq = self._classified(classify)
        stalled = 0
        for _ in range(max_up):
            if len(_content(seq)) >= limit:
                break
            # 微信的列表是懒加载的：滚到已加载内容的边缘后，还要继续滚才会拉出更早的消息，
            # 所以"这一轮没变化"不能立刻放弃，连续几次都没动静才算到头。
            self.scroll(3, steps=2)
            time.sleep(0.35)
            merged = merge_sequences(seq, self._classified(classify))
            if len(merged) == len(seq):
                stalled += 1
                if stalled >= 3:
                    break
            else:
                stalled = 0
                seq = merged
        # 读取时已经向上翻了最多 max_up 屏。结束时不逐格试探，而是一轮连续
        # 投递足够覆盖这些页面的向下滚轮，再短暂确认，避免一格格慢慢翻回去。
        self.scroll_to_bottom(steps=max_up * 2 + 4)
        self.settle_to_bottom(max_steps=4)
        return _content(seq)[-limit:]

    # ------------------------------------------------------------------ 写
    def focus_input(self) -> None:
        """后台把内部焦点放回输入框，不激活聊天窗口。

        语音右键菜单会把焦点留在列表控件上；此时继续向窗口投递 WM_CHAR，
        字符不会进入输入框。点击输入框可以恢复微信自己的内部焦点。
        """
        r = self.input_field.BoundingRectangle
        post_click(self.hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)
        time.sleep(0.05)

    def clear_input(self) -> None:
        val = self.input_value()
        if val:
            # WM_CHAR/退格按 UTF-16 code unit 处理；Python len() 会把
            # emoji 当成一个字符，导致输入框里残留代理项。
            backspace(self.hwnd, len(_utf16_units(val)) + 2)
            time.sleep(0.05)

    def send(self, text: str, timeout: float = 1.5) -> bool:
        """后台发送一条文本，返回是否确认或已提交。

        微信偶尔已发出消息，但虚拟化列表暂时读不到新行。只要发送后输入框
        被清空，就说明 Enter 已被处理；不能再把它当作失败反复重发。
        """
        text = sanitize_outgoing(text)
        if not text:
            return False
        self.focus_input()
        self.clear_input()
        before = _content(self.messages())
        type_text(self.hwnd, text)
        deadline = time.time() + 0.35
        while time.time() < deadline:
            if self.input_value() == text:
                break
            time.sleep(0.02)
        else:
            # 万一输入没进去，清掉重来一次，避免发出半截内容
            self.focus_input()
            self.clear_input()
            type_text(self.hwnd, text)
            time.sleep(0.1)
            if self.input_value() != text:
                self.clear_input()
                return False
        send_enter(self.hwnd)
        deadline = time.time() + timeout
        submitted = False
        while time.time() < deadline:
            if not self.input_value():
                submitted = True
            tail = _content(self.messages())
            if (
                tail
                and tail[-1].text == text
                and (len(tail) != len(before) or (before and before[-1].text != text))
            ):
                return True
            time.sleep(0.04)
        return submitted

    def send_sticker(
        self,
        index: int,
        *,
        panel_timeout: float = 2.0,
        send_timeout: float = 2.0,
    ) -> bool:
        """发送一张“自定义表情”收藏贴纸。

        微信打开表情面板时会把该弹层切到前台。发送结束后会自动把前台还给
        调用前的窗口。``index`` 是从收藏面板右下角开始、向左再逐行向上的序号。
        """
        if not isinstance(index, int) or index < 0:
            raise ValueError("贴纸 index 必须是非负整数")

        old_fg = foreground_hwnd()
        existing_panel = find_emoticon_popover()
        panel_hwnd = existing_panel[0] if existing_panel else None
        panel = existing_panel[1] if existing_panel else None
        active_messages_posted = False
        try:
            if panel is None:
                # Qt 会把表情弹层绑定到“活动窗口”。PostMessage 不改变系统
                # 前台，但需要先让微信内部短暂认为自己被激活。
                post(self.hwnd, WM_ACTIVATEAPP, 1, 0)
                post(self.hwnd, WM_NCACTIVATE, 1, 0)
                post(self.hwnd, WM_ACTIVATE, 1, 0)
                active_messages_posted = True
                self.focus_input()
                button = None
                for ctrl, _depth in iter_control(self.ctrl, max_depth=22):
                    if ctrl.ControlTypeName == "ButtonControl" and ctrl.Name == BUTTON_EMOTICON:
                        button = ctrl
                        break
                if button is None:
                    raise RuntimeError("找不到微信“发送表情”按钮")
                r = button.BoundingRectangle
                post_click(self.hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)

                deadline = time.time() + max(0.2, panel_timeout)
                while time.time() < deadline:
                    found = find_emoticon_popover()
                    if found is not None:
                        panel_hwnd, panel = found
                        break
                    time.sleep(0.04)
                if panel is None:
                    raise RuntimeError("微信表情面板没有打开")

            # 面板会记住上次使用的分类，发送前明确切回“自定义表情”。
            tab = None
            for ctrl, _depth in iter_control(panel, max_depth=8):
                if ctrl.ClassName == CLS_EMOTICON_TAB_ITEM and ctrl.Name == TAB_FAVORITE_STICKERS:
                    tab = ctrl
                    break
            if tab is None:
                raise RuntimeError("表情面板里找不到“自定义表情”分类")
            r = tab.BoundingRectangle
            post_click(panel_hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)
            # 连续发送两张时，微信可能先显示旧面板再异步刷新收藏分类。
            time.sleep(0.3)

            deadline = time.time() + max(0.2, panel_timeout)
            controls: list[auto.Control] = []
            while time.time() < deadline:
                controls = _favorite_sticker_controls(panel)
                if controls:
                    break
                time.sleep(0.04)
            if not controls:
                raise RuntimeError("“自定义表情”面板里没有可发送的贴纸")

            grid = None
            for ctrl, _depth in iter_control(panel, max_depth=8):
                if ctrl.ClassName == CLS_EMOTICON_GRID:
                    grid = ctrl
                    break
            if grid is None:
                raise RuntimeError("表情面板里找不到收藏贴纸网格")

            target_control = _find_favorite_sticker_control(
                panel,
                panel_hwnd,
                grid,
                index,
            )

            before = _content(self.messages())
            before_keys = {m.key() for m in before}
            r = target_control.BoundingRectangle
            post_click(panel_hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)

            deadline = time.time() + max(0.2, send_timeout)
            while time.time() < deadline:
                after = _content(self.messages())
                if after:
                    tail = after[-1]
                    short = tail.cls.split("::")[-1] if tail.cls else ""
                    if short == "ChatBubbleReferItemView" and tail.key() not in before_keys:
                        return True
                    if len(after) > len(before) and short == ("ChatBubbleReferItemView"):
                        return True
                time.sleep(0.05)
            return False
        finally:
            if active_messages_posted:
                post(self.hwnd, WM_ACTIVATE, 0, 0)
                post(self.hwnd, WM_NCACTIVATE, 0, 0)
                post(self.hwnd, WM_ACTIVATEAPP, 0, 0)
            # 贴纸发出后微信不一定自动关闭面板。主动关闭，避免下一次发送
            # 复用已经失活或仍停留在旧滚动位置的弹层。
            if panel_hwnd and u32.IsWindow(panel_hwnd):
                send_escape(panel_hwnd)
                time.sleep(0.12)
            if old_fg and u32.IsWindow(old_fg):
                restore_foreground(old_fg)


_EMOJI_RE = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\ufe0e\ufe0f\u200d\u20e3]")


def sanitize_outgoing(text: str) -> str:
    """把换行等不适合"投递字符"的内容规整成单行。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ").replace("\t", " ")
    text = _EMOJI_RE.sub("", text)
    text = " ".join(text.split()).strip()
    if len(text) >= 2:
        pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
        if pairs.get(text[0]) == text[-1]:
            text = text[1:-1].strip()
    prefixes = ["我", "本人", "回复"]
    if config.OWNER_NAME and config.OWNER_NAME not in prefixes:
        prefixes.append(re.escape(config.OWNER_NAME))
    return re.sub(rf"^(?:{'|'.join(prefixes)})\s*[：:]\s*", "", text)


def fit_outgoing(text: str, limit: int) -> str:
    """清洗并按标点边界截断，避免超长回复从句子中间硬切。"""
    text = sanitize_outgoing(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    head = text[:limit]
    boundaries = "。！？!?；;，,、：: "
    cut = max(head.rfind(ch) for ch in boundaries)
    # 太靠前的标点会让回复只剩半句话，这时宁可截到上限。
    if cut >= limit // 2:
        return head[: cut + 1].rstrip()
    return head.rstrip()


def _content(msgs: list[ChatMessage]) -> list[ChatMessage]:
    """只保留真正的内容条目（去掉时间分隔行）。"""
    return [m for m in msgs if m.kind != "time"]


def message_sequence_signature(msgs: list[ChatMessage]) -> tuple:
    """生成用于确认“滚动位置是否稳定”的内容签名。

    签名刻意不使用 RuntimeId：微信虚拟列表回收再创建控件时 ID 会变化，
    但同一屏幕的文字、类型和位置保持不变。这样既能发现真正的新消息，
    也不会把一次重绘误认为新内容。
    """
    return tuple((m.kind, m.text, m.cls, m.rect) for m in msgs if m.kind != "time")


def merge_sequences(a: list[ChatMessage], b: list[ChatMessage]) -> list[ChatMessage]:
    """把两屏快照拼成一份完整记录（消息列表是虚拟化的，只能看到可见的几行）。

    纯靠文字对齐会在"1、2、3、1、2"这种重复短消息上错位，所以这里用几何位移：
    滚动会让每一行整体平移同一个像素量，把 b 的每一行按这个位移映射回 a 的
    坐标系，两侧就能按屏幕位置一一对上。重叠部分优先采用判得出的一方。
    """
    if not a:
        return list(b)
    if not b:
        return list(a)

    def raw_y(m: ChatMessage) -> int:
        return m.rect[1] if m.rect else 0

    def frame_y(m: ChatMessage) -> int:
        return m.frame_y or raw_y(m)  # 已经并入坐标系的用坐标系位置

    def vote_counts(key_func) -> dict[int, int]:
        counts: dict[int, int] = {}
        for x in a:
            for y in b:
                if key_func(x) == key_func(y):
                    d = frame_y(x) - raw_y(y)
                    counts[d] = counts.get(d, 0) + 1
        return counts

    # 先按 RuntimeId 对齐；虚拟列表重建导致 ID 全变时，再按内容组合兜底。
    runtime_votes = vote_counts(lambda m: m.key())
    votes = runtime_votes
    if not votes:
        votes = vote_counts(lambda m: (m.kind, m.text, m.cls))
    delta, hits = max(votes.items(), key=lambda kv: kv[1]) if votes else (0, 0)
    if hits < 2:
        content_votes = vote_counts(lambda m: (m.kind, m.text, m.cls))
        content_delta, content_hits = (
            max(content_votes.items(), key=lambda kv: kv[1]) if content_votes else (0, 0)
        )
        if content_hits >= 2:
            delta, hits = content_delta, content_hits
    if hits < 2:  # 只对上一行，说明拼接不可靠，放弃这一屏
        return list(a)

    merged: dict[int, ChatMessage] = {}
    for m in b:  # 先把 b 映射到 a 的坐标系
        y = raw_y(m) + delta
        merged[y] = ChatMessage(
            m.kind, m.text, m.cls, m.rect, m.sender, y, m.runtime_id, m.transcript
        )
    for m in a:
        y = frame_y(m)
        old = merged.get(y)
        item = (
            m
            if m.frame_y
            else ChatMessage(m.kind, m.text, m.cls, m.rect, m.sender, y, m.runtime_id, m.transcript)
        )
        if old is None or old.key() != item.key():
            merged[y] = item
        elif old.sender is None and item.sender is not None:
            merged[y] = item
    return [merged[y] for y in sorted(merged)]
