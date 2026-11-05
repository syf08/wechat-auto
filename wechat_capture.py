"""在后台抓取聊天窗口位图，并用像素特征判断一条消息是"对方发的"还是"我发的"。

原理：
* 微信聊天窗口里，对方的消息气泡靠左、头像是左侧；我发的靠右、头像在右侧。
* 取消息行最左/最右各一小条区域，计算像素方差：
  头像所在的条带方差极大，纯色背景方差接近 0。
    对方消息：左条带方差 ≈ 1230，右条带方差 ≈ 0
    我的消息：左条带方差 ≈ 0，右条带方差 ≈ 28908
* 该判断与主题配色无关（即便对方是默认灰色头像也成立）。
* PrintWindow 可以在窗口被完全遮挡时依然拿到完整画面，所以整个判断不影响前台。
"""

from __future__ import annotations

import base64
import ctypes
import io
import time
from ctypes import wintypes
from typing import Callable

from PIL import Image

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002

INCOMING = "in"
OUTGOING = "out"


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class WindowCapture:
    """复用 GDI 对象抓取某个窗口的画面（不激活窗口、不受遮挡影响）。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._hdc = None
        self._mem = None
        self._bmp = None
        self._old_bmp = None
        self._size = (0, 0)

    def _ensure(self):
        r = wintypes.RECT()
        u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if (w, h) != self._size:
            self.release()
            self._hdc = u32.GetWindowDC(self.hwnd)
            self._mem = g32.CreateCompatibleDC(self._hdc)
            self._bmp = g32.CreateCompatibleBitmap(self._hdc, w, h)
            self._old_bmp = g32.SelectObject(self._mem, self._bmp)
            self._size = (w, h)
        return r, w, h

    # ------------------------------------------------------------------ 抓图
    def grab(self, full_content: bool = True) -> tuple[Image.Image, int, int]:
        """返回 (图像, 窗口左上角 screen x, screen y)。"""
        r, w, h = self._ensure()
        flag = PW_RENDERFULLCONTENT if full_content else 0
        u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        u32.PrintWindow(self.hwnd, self._mem, flag)
        buf = ctypes.create_string_buffer(w * h * 4)
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        g32.GetDIBits(self._mem, self._bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        return img.convert("RGB"), r.left, r.top

    def release(self):
        if self._bmp:
            if self._mem and self._old_bmp:
                g32.SelectObject(self._mem, self._old_bmp)
            g32.DeleteObject(self._bmp)
        if self._mem:
            g32.DeleteDC(self._mem)
        if self._hdc:
            u32.ReleaseDC(self.hwnd, self._hdc)
        self._hdc = self._mem = self._bmp = self._old_bmp = None
        self._size = (0, 0)

    # -------------------------------------------------------------- 发送方判断
    def classify_row(
        self, img: Image.Image, ox: int, oy: int, rect: tuple, viewport: tuple | None = None
    ) -> str | None:
        """rect = (left, top, right, bottom)，消息行在屏幕上的坐标。

        viewport 是消息列表可视区域；行被视口裁切时直接放弃判定——
        半截行只剩气泡的一角，像素特征会给出错误结论。
        """
        if viewport is not None:
            vl, vt, vr, vb = viewport
            if rect[1] < vt - 2 or rect[3] > vb + 2 or rect[0] < vl - 2 or rect[2] > vr + 2:
                return None
        left, top, right, bottom = rect
        W, H = img.size
        x0, x1 = max(0, left - ox), min(W, right - ox)
        y0, y1 = max(0, top - oy), min(H, bottom - oy)
        # 行被视口裁掉太多就判不出来（滚动历史时会出现这种半截行）
        if x1 - x0 < 60 or y1 - y0 < 40:
            return None
        w = x1 - x0
        # 头像区固定贴着窗口左右两侧，用行宽的比例来定位（适应不同 DPI / 窗口大小）
        band = max(24, int(w * 0.075))
        pad = max(6, int(w * 0.025))
        lvar = _strip_variance(img, x0 + pad, x0 + pad + band, y0, y1)
        rvar = _strip_variance(img, x1 - pad - band, x1 - pad, y0, y1)
        if lvar >= 300 and lvar > rvar * 2.5:
            return INCOMING
        if rvar >= 300 and rvar > lvar * 2.5:
            return OUTGOING
        return None

    def classify_batch(self, rects: list[tuple], viewport: tuple | None = None) -> list[str | None]:
        """一次抓屏，批量判定多行（历史记录标注用）。"""
        img, ox, oy = self.grab()
        return [self.classify_row(img, ox, oy, r, viewport) if r else None for r in rects]

    # -------------------------------------------------------------- 图片消息
    def message_image(
        self,
        rect: tuple,
        sender: str | None = None,
        viewport: tuple | None = None,
        max_side: int = 1024,
        settle_seconds: float = 0.4,
        retry_seconds: float = 0.35,
        retries: int = 2,
        resolve_rect: Callable[[], tuple | None] | None = None,
    ) -> tuple[str, tuple[int, int]]:
        """裁出单条图片/表情包消息并编码成内联 data URL。

        只发送这一条消息的截图，不发送整个聊天窗口；截图只在内存中转成
        base64，不写文件。发出去前按发送方去掉头像一侧，减少无关内容。

        微信会先创建消息控件，再异步下载和渲染图片。看到新控件就立刻
        PrintWindow 很容易只截到转圈占位符，因此这里先等待渲染；若视觉主体
        仍像一个小转圈，则重新读取控件矩形并有限重试，最后裁到实际内容。
        """
        if viewport is not None:
            _vl, vt, _vr, vb = viewport
            if rect[3] <= vt or rect[1] >= vb:
                raise ValueError("消息不在当前可见区域")

        time.sleep(max(0.0, settle_seconds))
        attempts = max(0, int(retries)) + 1
        active_rect = rect
        row = None
        for attempt in range(attempts):
            refreshed = resolve_rect() if resolve_rect is not None else None
            if refreshed is not None:
                active_rect = refreshed
            if viewport is not None:
                _vl, vt, _vr, vb = viewport
                if active_rect[3] <= vt or active_rect[1] >= vb:
                    raise ValueError("消息不在当前可见区域")

            img, ox, oy = self.grab()
            row = _crop_message_row(img, ox, oy, active_rect, sender)
            if not _looks_like_loading_placeholder(row):
                break
            if attempt + 1 < attempts:
                time.sleep(max(0.0, retry_seconds))
        else:
            raise ValueError("图片/表情包仍在加载，重试后仍未渲染完成")

        crop = _trim_visual_content(row)
        crop = _fit_image(crop, max_side)
        return to_image_data_url(crop, max_side), crop.size


def _strip_variance(img: Image.Image, xa: int, xb: int, ya: int, yb: int) -> float:
    px = img.load()
    vals = [px[x, y] for y in range(ya, yb, 3) for x in range(xa, xb, 3)]
    if not vals:
        return 0.0
    n = len(vals)
    mean = [sum(v[c] for v in vals) / n for c in range(3)]
    return sum(sum((v[c] - mean[c]) ** 2 for c in range(3)) for v in vals) / n


def _crop_message_row(
    img: Image.Image, ox: int, oy: int, rect: tuple, sender: str | None = None
) -> Image.Image:
    """裁出消息行并去掉发送方头像，保留视觉主体周围的背景供后续识别。"""
    left, top, right, bottom = rect
    width, height = img.size
    x0 = max(0, left - ox)
    y0 = max(0, top - oy)
    x1 = min(width, right - ox)
    y1 = min(height, bottom - oy)
    if x1 - x0 < 20 or y1 - y0 < 20:
        raise ValueError("消息截图区域过小")

    # 头像尺寸跟随消息行高度，不能按宽得多的整行宽度估算；按宽度裁掉
    # 7% 时，宽表情行可能连视觉主体左边缘一起切掉。
    trim = max(36, min(72, int((y1 - y0) * 0.26)))
    if sender == INCOMING:
        x0 = min(x1 - 20, x0 + trim)
    elif sender == OUTGOING:
        x1 = max(x0 + 20, x1 - trim)
    if x1 - x0 < 20 or y1 - y0 < 20:
        raise ValueError("消息截图区域过小")

    # 去掉消息行边缘的抗锯齿和阴影，保留完整气泡主体。
    inset = max(2, int((y1 - y0) * 0.02))
    x0 = min(x1 - 1, x0 + inset)
    x1 = max(x0 + 1, x1 - inset)
    y0 = min(y1 - 1, y0 + inset)
    y1 = max(y0 + 1, y1 - inset)
    if x1 - x0 < 16 or y1 - y0 < 16:
        raise ValueError("消息截图区域过小")

    return img.crop((x0, y0, x1, y1))


def _dominant_edge_color(img: Image.Image) -> tuple[int, int, int]:
    """取边框的主色作为聊天背景，兼容浅色和深色主题。"""
    width, height = img.size
    px = img.load()
    step = max(1, min(width, height) // 160)
    buckets: dict[tuple[int, int, int], list[int]] = {}

    def record(x: int, y: int) -> None:
        r, g, b = px[x, y]
        key = (r // 8, g // 8, b // 8)
        item = buckets.setdefault(key, [0, 0, 0, 0])
        item[0] += 1
        item[1] += r
        item[2] += g
        item[3] += b

    for x in range(0, width, step):
        record(x, 0)
        record(x, height - 1)
    for y in range(0, height, step):
        record(0, y)
        record(width - 1, y)

    best = max(buckets.values(), key=lambda item: item[0])
    count = max(1, best[0])
    return best[1] // count, best[2] // count, best[3] // count


def _foreground_bounds(img: Image.Image) -> tuple[int, int, int, int] | None:
    """估计与聊天背景明显不同的像素范围，用来识别转圈和裁掉空白。"""
    width, height = img.size
    bg_r, bg_g, bg_b = _dominant_edge_color(img)
    px = img.load()
    left, top = width, height
    right = bottom = -1
    step = 2
    for y in range(0, height, step):
        for x in range(0, width, step):
            r, g, b = px[x, y]
            if max(abs(r - bg_r), abs(g - bg_g), abs(b - bg_b)) < 18:
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)
    if right < left or bottom < top:
        return None
    return left, top, right + step, bottom + step


def _looks_like_loading_placeholder(row: Image.Image) -> bool:
    """只把相对消息行明显过小的独立图形当作加载占位，避免误伤小图。"""
    bounds = _foreground_bounds(row)
    if bounds is None:
        return True
    x0, y0, x1, y1 = bounds
    body_w, body_h = x1 - x0, y1 - y0
    row_w, row_h = row.size
    large_side = max(body_w, body_h)
    sparse = body_w * body_h <= row_w * row_h * 0.035
    tiny = large_side <= max(40, int(row_h * 0.20))
    return tiny and sparse


def _trim_visual_content(row: Image.Image) -> Image.Image:
    """围绕真实前景收紧截图，去掉整行右侧或上方的大块聊天背景。"""
    bounds = _foreground_bounds(row)
    if bounds is None:
        return row
    x0, y0, x1, y1 = bounds
    pad = max(4, min(16, max(x1 - x0, y1 - y0) // 20))
    crop = row.crop(
        (
            max(0, x0 - pad),
            max(0, y0 - pad),
            min(row.width, x1 + pad),
            min(row.height, y1 + pad),
        )
    )
    if crop.width < 20 or crop.height < 20:
        return row
    return crop


def _fit_image(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img
    resample = getattr(Image, "Resampling", Image).LANCZOS
    fitted = img.copy()
    fitted.thumbnail((max_side, max_side), resample)
    return fitted


def to_image_data_url(img: Image.Image, max_side: int = 1024) -> str:
    """把图片转成 DeepSeek/OpenAI 兼容接口可接收的 data URL。"""
    img = img.convert("RGB")
    if max(img.size) > max_side:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img.thumbnail((max_side, max_side), resample)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
