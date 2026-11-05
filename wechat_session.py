"""配对流程：用搜索框找到联系人，并打开它的独立聊天窗口（之后全后台收发）。"""

from __future__ import annotations

import time

import uiautomation as auto

import wechat_ui as w


class ContactNotFound(RuntimeError):
    pass


def _search_field(main: auto.Control):
    for c, _d in w.iter_control(main):
        if c.ControlTypeName == "EditControl" and c.ClassName == w.CLS_SEARCH_FIELD:
            return c
    return None


def _popover(main: auto.Control):
    """搜索弹层。它既是一个独立顶层窗口，也会出现在主窗口的控件树里。"""
    pop = w.find_by_class(main, w.CLS_SEARCH_POPOVER, max_depth=12)
    if pop is not None:
        return pop
    return w.find_popover()


def _popover_items(pop: auto.Control):
    return [
        c
        for c, _d in w.iter_control(pop, 8)
        if c.ClassName in ("mmui::SearchContentCellView", "mmui::SearchMessageCellView")
    ]


def _popover_hwnd(pop: auto.Control, fallback: int) -> int:
    try:
        h = pop.NativeWindowHandle
    except Exception:  # noqa: BLE001
        h = 0
    return h or fallback


def _click_center(hwnd: int, ctrl, double: bool = False) -> None:
    r = ctrl.BoundingRectangle
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if double:
        w.post_double_click(hwnd, cx, cy)
    else:
        w.post_click(hwnd, cx, cy)


def _session_item(main: auto.Control, name: str):
    sl = w.find_by_aid(main, w.AID_SESSION_LIST)
    if sl is None:
        return None, None
    for k in sl.GetChildren():
        if k.AutomationId == f"session_item_{name}":
            return sl, k
    return sl, None


def _restore_pairing_state(
    main_hwnd: int, was_minimized: bool, previous_foreground: int, verbose: bool, log
) -> None:
    """无论配对成功还是中途报错，都尽量恢复窗口状态。"""
    if was_minimized and main_hwnd:
        try:
            w.minimize(main_hwnd)
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
    if previous_foreground:
        try:
            w.restore_foreground(previous_foreground)
        except Exception:  # noqa: BLE001
            pass
    if verbose:
        try:
            log(f"[配对] 前台窗口已还原：{w.foreground_hwnd():#x}")
        except Exception:  # noqa: BLE001
            pass


def open_contact_chat(name: str, log=print, verbose: bool = True) -> w.ChatWindow:
    """找到联系人 name 并打开它的独立聊天窗口。

    返回已连接的 ChatWindow。流程不主动激活微信；结束时仍会把前台窗口还原，
    作为微信版本差异的兼容兜底。
    """
    main = w.find_main_window()
    if main is None:
        raise RuntimeError("没找到微信主窗口，请先启动并登录微信")
    main_hwnd = main.NativeWindowHandle
    was_minimized = w.is_minimized(main_hwnd)
    prev_fg = w.foreground_hwnd()
    field = None

    try:
        if was_minimized:
            w.show_no_activate(main_hwnd)
            time.sleep(0.2)

        field = _search_field(main)
        if field is None:
            raise RuntimeError("没找到微信搜索框")

        # ---- 1. 在搜索框里输入联系人名字 ----
        # 搜索框必须先清空再写入，否则内容相同微信不会再弹搜索结果。
        item = None
        pop = None
        for attempt in range(1, 3):
            try:
                field.GetValuePattern().SetValue("")
                time.sleep(0.15)
                field.GetValuePattern().SetValue(name)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"写入微信搜索框失败：{exc}") from exc
            log(
                f"[配对] 已在微信搜索框输入：{name}"
                + (f"（第 {attempt} 次）" if attempt > 1 else "")
            )

            deadline = time.time() + 2.5
            while time.time() < deadline:
                pop = _popover(main)
                if pop is not None:
                    item = w.find_by_aid(pop, f"search_item_{name}", max_depth=8)
                    if item is not None:
                        break
                    cands = [
                        c
                        for c in _popover_items(pop)
                        if c.ClassName == "mmui::SearchContentCellView"
                    ]
                    if len(cands) == 1:
                        item = cands[0]
                        break
                time.sleep(0.1)
            if item is not None:
                break

        if item is None:
            cands = [c for c in _popover_items(pop)] if pop is not None else []
            names = "、".join(sorted({c.Name for c in cands}))
            hint = f"搜索里显示的候选是：{names}" if names else "搜索没有返回任何联系人"
            raise ContactNotFound(
                f"没能在微信里精确找到联系人“{name}”。{hint}\n"
                f"提示：请把联系人名写完整（例如备注名）。"
            )
        real_name = item.Name
        log(f"[配对] 搜索到联系人：{real_name}")

        # ---- 2. 点开会话，并清掉搜索状态 ----
        pop_hwnd = _popover_hwnd(pop, main_hwnd) if pop is not None else main_hwnd
        _click_center(pop_hwnd, item)
        time.sleep(0.5)
        if _popover(main) is not None:
            w.send_escape(pop_hwnd)
            time.sleep(0.3)
        try:
            field.GetValuePattern().SetValue("")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        if _popover(main) is not None:
            w.send_escape(pop_hwnd)
            time.sleep(0.2)

        # ---- 3. 在会话列表里双击，打开独立聊天窗口 ----
        main = w.find_main_window() or main
        row = None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            _sl, row = _session_item(main, real_name)
            if row is not None:
                break
            time.sleep(0.1)
        if row is None:
            raise RuntimeError(f"会话列表里没找到“{real_name}”，无法打开独立窗口")

        _click_center(main_hwnd, row, double=True)
        chat_ctrl = None
        deadline = time.time() + 4.0
        while time.time() < deadline:
            chat_ctrl = w.find_chat_window(real_name)
            if chat_ctrl is not None:
                break
            time.sleep(0.1)
        if chat_ctrl is None:
            raise RuntimeError(f"没能打开“{real_name}”的独立聊天窗口")
        log(f"[配对] 已打开独立聊天窗口：{real_name}")

        chat = w.ChatWindow(chat_ctrl)
        if w.is_minimized(chat.hwnd):
            w.show_no_activate(chat.hwnd)
        return chat
    finally:
        # ---- 4. 收尾：成功和失败都恢复原前台/最小化状态 ----
        if field is not None:
            try:
                field.GetValuePattern().SetValue("")
            except Exception:  # noqa: BLE001
                pass
        try:
            pop = _popover(main)
            if pop is not None:
                w.send_escape(_popover_hwnd(pop, main_hwnd))
        except Exception:  # noqa: BLE001
            pass
        _restore_pairing_state(main_hwnd, was_minimized, prev_fg, verbose, log)
