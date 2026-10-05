"""Prepare an observed MT5 native ticket; never activate any trading button.

This product adapter uses targeted Win32 messages, not screen coordinates or
global keystrokes. An unsupported layout, an existing ticket or an unverified
absolute-price stop mode blocks preparation. No order-submission API is used.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import time


class TicketError(RuntimeError):
    def __init__(self, reason: str):
        allowed = {
            "unsupported_platform", "terminal_mismatch", "account_mismatch", "existing_ticket",
            "unsupported_ui", "absolute_mode_unverified", "chart_unavailable", "focus_unavailable",
            "ticket_unavailable", "ticket_changed", "readback_failed", "expired", "invalid_draft",
        }
        self.reason = reason if reason in allowed else "unsupported_ui"
        super().__init__(self.reason)


@dataclass(frozen=True)
class Draft:
    symbol: str
    direction: str
    volume: Decimal
    stop: Decimal
    target: Decimal
    digits: int
    expires_at: datetime

    def __post_init__(self):
        if (
            not isinstance(self.symbol, str) or not re.fullmatch(r"(?:XAUUSD|GOLD)[A-Za-z0-9._#-]{0,24}", self.symbol, re.I)
            or self.direction not in {"BUY", "SELL"}
            or self.volume != Decimal("0.01")
            or type(self.digits) is not int or not 0 <= self.digits <= 10
            or not isinstance(self.expires_at, datetime) or self.expires_at.utcoffset() is None
        ):
            raise TicketError("invalid_draft")
        for value in (self.stop, self.target):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise TicketError("invalid_draft")
        if not ((self.stop < self.target) if self.direction == "BUY" else (self.target < self.stop)):
            raise TicketError("invalid_draft")

    @property
    def comment(self):
        return f"{self.direction} exp {self.expires_at.astimezone(timezone.utc):%d/%m %H:%M}Z"


def require_unexpired(draft, now):
    if not isinstance(now, datetime) or now.utcoffset() is None or now >= draft.expires_at:
        raise TicketError("expired")


def _decimal_text(value):
    try:
        parsed = Decimal(value.strip())
    except (AttributeError, InvalidOperation):
        raise TicketError("readback_failed") from None
    if not parsed.is_finite():
        raise TicketError("readback_failed")
    return parsed


def read_stop_mode_control(dialog):
    """Read one explicitly labeled price/points combo in the selected Trade tab."""
    def normalized(value):
        return str(value).replace("&", "").strip().rstrip(":").casefold()

    combos = [control for control in dialog.descendants(control_type="ComboBox") if control.is_visible()]
    matches = [control for control in combos if normalized(control.element_info.name) == "stop levels"]
    if not matches:
        labels = [control for control in dialog.descendants(control_type="Text")
                  if control.is_visible() and normalized(control.window_text()) == "stop levels"]
        if len(labels) != 1:
            raise TicketError("absolute_mode_unverified")
        label = labels[0].rectangle()
        center = (label.top + label.bottom) / 2
        matches = [control for control in combos
                   if control.rectangle().left >= label.right
                   and control.rectangle().top <= center <= control.rectangle().bottom]
    if len(matches) != 1:
        raise TicketError("absolute_mode_unverified")
    selected = normalized(matches[0].selected_text())
    if selected in {"in prices", "prices"}:
        return "prices"
    if selected in {"in points", "points"}:
        return "points"
    raise TicketError("absolute_mode_unverified")


class NativeTicketAdapter:
    """High-level preparation with an injectable, strictly targeted backend."""

    def __init__(self, backend, *, clock=None):
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self, draft: Draft, *, recheck_account):
        self._preparing_ticket = None
        try:
            return self._prepare(draft, recheck_account=recheck_account, ticket_ready=self._remember_ticket)
        except BaseException:
            ticket = getattr(self, "_preparing_ticket", None)
            if ticket is not None:
                try:
                    self.backend.cancel_owned_ticket(ticket, draft.symbol)
                except Exception:
                    # A changed account/process/window must never cause a
                    # different window to be closed or an unsafe unlock.
                    pass
            raise
        finally:
            self._preparing_ticket = None

    def _remember_ticket(self, ticket):
        self._preparing_ticket = ticket

    def _prepare(self, draft: Draft, *, recheck_account, ticket_ready):
        require_unexpired(draft, self.clock())
        recheck_account()
        self.backend.verify_terminal()
        if self.backend.existing_tickets():
            raise TicketError("existing_ticket")
        self.backend.verify_absolute_stop_mode()
        chart = self.backend.activate_chart(draft.symbol, "M15")
        # An independently opened ticket must never be overwritten.
        if self.backend.existing_tickets():
            raise TicketError("existing_ticket")
        require_unexpired(draft, self.clock())
        recheck_account()
        ticket = self.backend.open_ticket(chart)
        ticket_ready(ticket)
        self.backend.verify_ticket(ticket, draft.symbol)
        self.backend.lock_ticket(ticket, draft.symbol)
        self.backend.verify_absolute_stop_mode()
        self.backend.verify_absolute_ticket_units(ticket)
        fields = {
            10333: format(draft.volume, ".2f"),
            10334: format(draft.stop, f".{draft.digits}f"),
            10336: format(draft.target, f".{draft.digits}f"),
        }
        for control_id, value in fields.items():
            require_unexpired(draft, self.clock())
            recheck_account()
            self.backend.verify_ticket(ticket, draft.symbol)
            self.backend.verify_ticket_locked(ticket, draft.symbol)
            self.backend.set_edit(ticket, control_id, value)
            if _decimal_text(self.backend.read_edit(ticket, control_id)) != Decimal(value):
                raise TicketError("readback_failed")
        self.backend.verify_ticket_locked(ticket, draft.symbol)
        self.backend.set_comment(ticket, draft.comment)
        if self.backend.read_comment(ticket) != draft.comment:
            raise TicketError("readback_failed")
        require_unexpired(draft, self.clock())
        recheck_account()
        self.backend.verify_terminal()
        self.backend.verify_absolute_stop_mode()
        self.backend.verify_ticket(ticket, draft.symbol)
        self.backend.verify_ticket_locked(ticket, draft.symbol)
        self.backend.verify_absolute_ticket_units(ticket)
        if any(_decimal_text(self.backend.read_edit(ticket, key)) != Decimal(value) for key, value in fields.items()):
            raise TicketError("readback_failed")
        # Options and native readbacks can take time. Refresh the full caller
        # guard after that work, immediately before reporting a valid draft.
        require_unexpired(draft, self.clock())
        recheck_account()
        self.backend.unlock_ticket(ticket, draft.symbol)
        return {"status": "prepared"}


class Win32Terminal:
    """Installed English MT5 layout observed on this laptop; fail closed elsewhere.

    Only the owned ticket's allowed fields and preparation lock are writable.
    F9 opens it; WM_CLOSE cancels a failed owned draft. No trading button,
    Enter event, trading command or global keystroke is invoked.
    """

    EDIT_IDS = frozenset({10333, 10334, 10336})
    SUBMIT_IDS = frozenset({10408, 10409})

    def __init__(self, terminal: Path, account_login: int, data_directory: Path, *, stop_mode_reader=None, account_guard=None):
        if os.name != "nt":
            raise TicketError("unsupported_platform")
        if type(account_login) is not int or account_login <= 0:
            raise TicketError("account_mismatch")
        self.terminal = terminal.resolve()
        self.account_login = account_login
        self.data_directory = Path(data_directory).resolve()
        self.stop_mode_reader = stop_mode_reader or self.read_stop_mode
        self.price_mode_attested = False
        self.attested_ticket = None
        self.locked_ticket = None
        self.account_guard = account_guard
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()
        candidates = []
        for handle in self.windows():
            pid = self.window_pid(handle)
            if self.process_path(pid) == self.terminal and self._account_caption_matches(self.text(handle)):
                candidates.append((handle, pid))
        if len(candidates) != 1:
            raise TicketError("terminal_mismatch")
        self.main, self.pid = candidates[0]
        self.verify_terminal()

    def _configure_functions(self):
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.callback_type = callback
        for name in ("EnumWindows", "EnumChildWindows"):
            function = getattr(self.user, name)
            function.argtypes = ([callback, wintypes.LPARAM] if name == "EnumWindows" else [wintypes.HWND, callback, wintypes.LPARAM])
            function.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetDlgCtrlID.argtypes = [wintypes.HWND]
        self.user.GetDlgCtrlID.restype = ctypes.c_int
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.restype = ctypes.c_int
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.IsWindowEnabled.argtypes = [wintypes.HWND]
        self.user.IsWindowEnabled.restype = wintypes.BOOL
        self.user.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.user.EnableWindow.restype = wintypes.BOOL
        self.user.GetMenu.argtypes = [wintypes.HWND]
        self.user.GetMenu.restype = wintypes.HMENU
        self.user.GetMenuItemCount.argtypes = [wintypes.HMENU]
        self.user.GetMenuItemCount.restype = ctypes.c_int
        self.user.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
        self.user.GetSubMenu.restype = wintypes.HMENU
        self.user.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
        self.user.GetMenuStringW.restype = ctypes.c_int
        self.user.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
        self.user.GetMenuItemID.restype = wintypes.UINT
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.SetForegroundWindow.restype = wintypes.BOOL
        self.user.GetForegroundWindow.argtypes = []
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL
        self.user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                                 wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.user.SendMessageTimeoutW.restype = wintypes.LPARAM
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def windows(self, parent=None):
        found = []
        callback = self.callback_type(lambda handle, parameter: found.append(handle) or True)
        if parent is None:
            self.user.EnumWindows(callback, 0)
        else:
            self.user.EnumChildWindows(parent, callback, 0)
        return found

    def window_pid(self, handle):
        result = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(result))
        return result.value

    def process_path(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            text = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(text))
            if self.kernel.QueryFullProcessImageNameW(process, 0, text, ctypes.byref(length)):
                return Path(text.value).resolve()
            return None
        finally:
            self.kernel.CloseHandle(process)

    def _message(self, handle, message, first=0, second=0):
        result = ctypes.c_size_t()
        if not self.user.SendMessageTimeoutW(handle, message, first, second, 0x2, 1500, ctypes.byref(result)):
            raise TicketError("unsupported_ui")
        return result.value

    def text(self, handle):
        buffer = ctypes.create_unicode_buffer(4096)
        self._message(handle, 0x000D, len(buffer), ctypes.addressof(buffer))  # WM_GETTEXT
        return buffer.value

    def class_name(self, handle):
        buffer = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value

    def _account_caption_matches(self, caption):
        return re.match(r"^" + str(self.account_login) + r"\s*[-–]", caption) is not None

    def verify_terminal(self):
        if (
            not self.user.IsWindow(self.main) or self.window_pid(self.main) != self.pid
            or self.process_path(self.pid) != self.terminal
        ):
            raise TicketError("terminal_mismatch")
        if not self._account_caption_matches(self.text(self.main)):
            raise TicketError("account_mismatch")

    def existing_tickets(self):
        self.verify_terminal()
        found = []
        for handle in self.windows():
            if handle == self.main or self.window_pid(handle) != self.pid or not self.user.IsWindowVisible(handle):
                continue
            children = self.windows(handle)
            ids = {self.user.GetDlgCtrlID(child) for child in children}
            if self.text(handle).startswith("Order:") or ids & self.SUBMIT_IDS:
                found.append(handle)
        return found

    def read_stop_mode(self):
        """Read the actual Trade setting, without changing or saving settings.

        The installed Order window is modeless. Each call reads Options again,
        including after ticket creation. An open ticket is allowed only when
        this adapter created that exact window under a freshly checked price
        setting; independent tickets must never be touched or reused.
        """
        self.verify_terminal()
        tickets = self.existing_tickets()
        if tickets and (not self.price_mode_attested or tickets != [self.attested_ticket]):
            raise TicketError("absolute_mode_unverified")
        self.price_mode_attested = False
        if not self.user.IsWindowEnabled(self.main):
            raise TicketError("absolute_mode_unverified")
        options = None
        try:
            from pywinauto import Application
            command = self._options_menu_command()
            # Only the discovered Options menu command is posted. Options is
            # modal, so a synchronous dispatch could block before discovery and
            # cleanup. No guessed command ID or trading command is accepted.
            if not self.user.PostMessageW(self.main, 0x0111, command, 0):
                raise TicketError("absolute_mode_unverified")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                matches = [handle for handle in self.windows() if self.window_pid(handle) == self.pid
                           and self.class_name(handle) == "#32770" and self.text(handle) == "Options"
                           and self.user.IsWindowVisible(handle)]
                if len(matches) == 1:
                    options = matches[0]
                    break
                if len(matches) > 1:
                    raise TicketError("absolute_mode_unverified")
                time.sleep(0.05)
            if options is None:
                raise TicketError("absolute_mode_unverified")
            application = Application(backend="uia").connect(process=self.pid)
            dialog = application.window(handle=options)
            tabs = dialog.child_window(control_type="Tab").wrapper_object()
            tabs.select("Trade")  # Selection pattern, not a mouse/keyboard event
            names = tabs.texts()
            selected = tabs.get_selected_tab()
            if selected is None or not 0 <= selected < len(names) or names[selected] != "Trade":
                raise TicketError("absolute_mode_unverified")
            mode = read_stop_mode_control(dialog)
            if mode != "prices":
                raise TicketError("absolute_mode_unverified")
        except TicketError:
            raise
        except Exception:
            raise TicketError("absolute_mode_unverified") from None
        finally:
            if options is not None and self.user.IsWindow(options) and self.window_pid(options) == self.pid:
                self._message(options, 0x0010)  # WM_CLOSE = Cancel; never save Options
                deadline = time.monotonic() + 3
                while self.user.IsWindow(options) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if self.user.IsWindow(options):
                    raise TicketError("absolute_mode_unverified")
        self.verify_terminal()
        if self.existing_tickets() != tickets or not self.user.IsWindowEnabled(self.main):
            raise TicketError("absolute_mode_unverified")
        self.price_mode_attested = True
        return "prices"

    def _options_menu_command(self):
        menu = self.user.GetMenu(self.main)
        if not menu:
            raise TicketError("absolute_mode_unverified")
        matches = []
        for index in range(self.user.GetMenuItemCount(menu)):
            title = ctypes.create_unicode_buffer(256)
            self.user.GetMenuStringW(menu, index, title, len(title), 0x400)
            if title.value.replace("&", "").split("\t", 1)[0].strip() != "Tools":
                continue
            submenu = self.user.GetSubMenu(menu, index)
            if not submenu:
                continue
            for item in range(self.user.GetMenuItemCount(submenu)):
                caption = ctypes.create_unicode_buffer(256)
                self.user.GetMenuStringW(submenu, item, caption, len(caption), 0x400)
                name = caption.value.replace("&", "").split("\t", 1)[0].strip().rstrip(".")
                if name != "Options":
                    continue
                command = self.user.GetMenuItemID(submenu, item)
                if 0 < command <= 0xFFFF and command not in self.SUBMIT_IDS:
                    matches.append(command)
        if len(matches) != 1:
            raise TicketError("absolute_mode_unverified")
        return matches[0]

    def verify_absolute_stop_mode(self):
        if self.stop_mode_reader() != "prices":
            raise TicketError("absolute_mode_unverified")

    def activate_chart(self, symbol, timeframe):
        self.verify_terminal()
        matches = [handle for handle in self.windows(self.main) if self.text(handle) == f"{symbol},{timeframe}"]
        if not matches:
            raise TicketError("chart_unavailable")
        parents = {self.user.GetParent(chart) for chart in matches}
        if (len(parents) != 1 or any(self.window_pid(chart) != self.pid for chart in matches)
                or self.class_name(next(iter(parents))) != "MDIClient"):
            raise TicketError("unsupported_ui")
        parent = next(iter(parents))
        active = self._message(parent, 0x0229)  # WM_MDIGETACTIVE
        # Duplicate charts of the same exact symbol/timeframe are legitimate.
        # Keep the user's current matching chart; otherwise select one stable
        # window handle, and verify the MDI selection before opening its ticket.
        chart = active if active in matches else min(matches)
        if self.user.GetForegroundWindow() != self.main and not self.user.SetForegroundWindow(self.main):
            raise TicketError("focus_unavailable")
        self._message(parent, 0x0222, chart, 0)  # WM_MDIACTIVATE, chart selection only
        active = self._message(parent, 0x0229)  # WM_MDIGETACTIVE
        if active != chart or self.user.GetForegroundWindow() != self.main:
            raise TicketError("focus_unavailable")
        return chart

    def open_ticket(self, chart):
        self.verify_terminal()
        if self.existing_tickets():
            raise TicketError("existing_ticket")
        if not self.price_mode_attested:
            raise TicketError("absolute_mode_unverified")
        self.attested_ticket = None
        if self.window_pid(chart) != self.pid or self.user.GetForegroundWindow() != self.main:
            raise TicketError("focus_unavailable")
        if not self.user.PostMessageW(chart, 0x0100, 0x78, 1):  # WM_KEYDOWN, F9 only
            raise TicketError("ticket_unavailable")
        self.user.PostMessageW(chart, 0x0101, 0x78, 0xC0000001)  # WM_KEYUP, F9 only
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = self.existing_tickets()
            if len(found) == 1:
                self.attested_ticket = found[0]
                return found[0]
            if len(found) > 1:
                raise TicketError("unsupported_ui")
            time.sleep(0.05)
        raise TicketError("ticket_unavailable")

    def control(self, parent, control_id, expected_class):
        if control_id in self.SUBMIT_IDS:
            raise TicketError("unsupported_ui")
        matches = [handle for handle in self.windows(parent) if self.user.GetDlgCtrlID(handle) == control_id]
        if len(matches) != 1 or self.class_name(matches[0]) != expected_class or self.window_pid(matches[0]) != self.pid:
            raise TicketError("unsupported_ui")
        return matches[0]

    def verify_ticket(self, ticket, symbol):
        self.verify_terminal()
        if ticket != self.attested_ticket or self.window_pid(ticket) != self.pid or self.existing_tickets() != [ticket]:
            raise TicketError("ticket_changed")
        if self.class_name(ticket) != "#32770" or not self.text(ticket).startswith(f"Order: {symbol} - "):
            raise TicketError("unsupported_ui")
        self.control(ticket, 10331, "ComboBox")
        selected = self.text(self.control(ticket, 10325, "Edit"))
        if selected not in (symbol, f"{symbol}, Gold vs US Dollar", f"{symbol} - Gold vs US Dollar"):
            raise TicketError("ticket_changed")
        execution = self.control(ticket, 11116, "Button")
        if self.text(execution) != "Market Execution":
            raise TicketError("unsupported_ui")
        for field in self.EDIT_IDS:
            self.control(ticket, field, "Edit")

    def verify_absolute_ticket_units(self, ticket):
        # Installed price mode has these blank conversion-label controls. Any
        # points indicator or changed layout blocks the adapter.
        for control_id in (11138, 11139):
            label = self.control(ticket, control_id, "Static")
            if self.text(label).strip():
                raise TicketError("absolute_mode_unverified")

    def lock_ticket(self, ticket, symbol):
        self.verify_ticket(ticket, symbol)
        if not self.user.IsWindowEnabled(ticket):
            raise TicketError("unsupported_ui")
        self.user.EnableWindow(ticket, False)
        if self.user.IsWindowEnabled(ticket):
            raise TicketError("readback_failed")
        self.locked_ticket = ticket

    def verify_ticket_locked(self, ticket, symbol):
        self.verify_ticket(ticket, symbol)
        if ticket != self.locked_ticket or self.user.IsWindowEnabled(ticket):
            raise TicketError("ticket_changed")

    def unlock_ticket(self, ticket, symbol):
        self.verify_ticket_locked(ticket, symbol)
        self.user.EnableWindow(ticket, True)
        if not self.user.IsWindowEnabled(ticket):
            raise TicketError("readback_failed")
        self.locked_ticket = None

    def cancel_owned_ticket(self, ticket, symbol):
        # Cancel only our verified, locked dialog. The independent SDK identity
        # guard checks server/account binding without requiring a fresh quote,
        # so a stale quote can still cancel its partial draft safely.
        if ticket != self.locked_ticket or not callable(self.account_guard):
            raise TicketError("ticket_changed")
        self.account_guard()
        self.verify_ticket(ticket, symbol)
        self._message(ticket, 0x0010)  # WM_CLOSE cancels; no button invocation
        deadline = time.monotonic() + 3
        while self.user.IsWindow(ticket) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.user.IsWindow(ticket):
            raise TicketError("ticket_changed")
        self.locked_ticket = self.attested_ticket = None
        self.price_mode_attested = False

    def set_edit(self, ticket, control_id, value):
        if control_id not in self.EDIT_IDS:
            raise TicketError("unsupported_ui")
        handle = self.control(ticket, control_id, "Edit")
        buffer = ctypes.create_unicode_buffer(value)
        if not self._message(handle, 0x000C, 0, ctypes.addressof(buffer)):  # WM_SETTEXT
            raise TicketError("readback_failed")

    def read_edit(self, ticket, control_id):
        if control_id not in self.EDIT_IDS:
            raise TicketError("unsupported_ui")
        return self.text(self.control(ticket, control_id, "Edit"))

    def _comment_edit(self, ticket):
        combo = self.control(ticket, 10339, "ComboBox")
        return self.control(combo, 1001, "Edit")

    def set_comment(self, ticket, value):
        if len(value) > 31 or not re.fullmatch(r"(?:BUY|SELL) exp \d{2}/\d{2} \d{2}:\d{2}Z", value):
            raise TicketError("invalid_draft")
        buffer = ctypes.create_unicode_buffer(value)
        if not self._message(self._comment_edit(ticket), 0x000C, 0, ctypes.addressof(buffer)):
            raise TicketError("readback_failed")

    def read_comment(self, ticket):
        return self.text(self._comment_edit(ticket))
