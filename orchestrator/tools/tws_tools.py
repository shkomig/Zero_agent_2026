"""TWS (Trader Workstation) automation — launch + fill username via WM_CHAR.

Approach history:
  pyautogui.write / SendKeys / WScript.Shell  → dropped leading chars (Java Swing EDT)
  Ctrl+V / Shift+Insert clipboard paste       → ignored by Swing
  IBC                                         → needs offline TWS, user has auto-updating
  WM_CHAR PostMessage (current)               → posts directly to Java's message queue,
                                               bypasses SendInput filtering

Current strategy:
  1. Launch tws.exe
  2. Wait for Login dialog (SunAwtFrame, title="Login")
  3. Focus it
  4. PostMessage WM_CHAR for each char of username → Tab → password field active
  5. Telegram nudge: "הקלד סיסמה + Enter"

Environment variables (.env):
  ZERO_AGENT_TWS_EXE          path to tws.exe (default: C:\\Jts\\interil\\tws.exe)
  ZERO_AGENT_IBC_USERNAME     IBKR username to fill automatically
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
import time
from pathlib import Path

from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.tws_tools")

# Win32 constants
WM_CHAR    = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
VK_TAB     = 0x09
VK_RETURN  = 0x0D


def _tws_exe() -> Path:
    raw = os.getenv("ZERO_AGENT_TWS_EXE", "").strip()
    return Path(raw) if raw else Path("C:/Jts/interil/tws.exe")


def _tws_username() -> str:
    return os.getenv("ZERO_AGENT_IBC_USERNAME", "").strip()


def _tws_password() -> str:
    return os.getenv("ZERO_AGENT_IBC_PASSWORD", "").strip()


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _get_login_hwnd() -> int:
    """Return HWND of the TWS Login dialog (SunAwtFrame titled 'Login'), or 0."""
    user32 = ctypes.windll.user32
    result = ctypes.c_void_p(0)

    @_EnumProc
    def _cb(hwnd, _):
        title = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, title, 64)
        if title.value == "Login":
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "SunAwtFrame":
                result.value = hwnd
                return False
        return True

    user32.EnumWindows(_cb, 0)
    return result.value or 0


def _focus_hwnd(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)               # SW_RESTORE
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)   # HWND_TOPMOST, SWP_NOMOVE|SIZE
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)   # HWND_NOTOPMOST
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def _wm_type(hwnd: int, text: str, char_delay: float = 0.07) -> None:
    """Post WM_CHAR messages for each character — bypasses SendInput EDT filtering."""
    user32 = ctypes.windll.user32
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
        time.sleep(char_delay)


def _wm_key(hwnd: int, vk: int, delay: float = 0.06) -> None:
    """Send a single key down+up via PostMessage."""
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk, 0)
    lp_dn = 1 | (scan << 16)
    lp_up = lp_dn | (1 << 30) | (1 << 31)
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, lp_dn)
    time.sleep(delay)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, lp_up)


def _fill_credentials(hwnd: int, username: str, password: str) -> None:
    """Focus dialog, type username + Tab + password + Enter via WM_CHAR PostMessage."""
    _focus_hwnd(hwnd)
    time.sleep(1.8)           # Java EDT needs time to set internal focus on field 1
    _wm_type(hwnd, username)
    time.sleep(0.3)
    _wm_key(hwnd, VK_TAB)    # move to password field
    time.sleep(0.5)           # give Java a moment to shift focus
    _wm_type(hwnd, password)
    time.sleep(0.3)
    _wm_key(hwnd, VK_RETURN)  # submit


# ---------------------------------------------------------------------------
# Async state helpers
# ---------------------------------------------------------------------------

async def _tws_running() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "tasklist", "/FI", "IMAGENAME eq tws.exe", "/NH", "/FO", "CSV",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return b"tws.exe" in out
    except Exception:
        return False


async def _at_login() -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: bool(_get_login_hwnd()))


# ---------------------------------------------------------------------------
# Registered tools
# ---------------------------------------------------------------------------

@registry.register
async def launch_tws(notify_telegram: bool = True, wait_seconds: int = 30) -> str:
    """Launch TWS and fill username + password automatically via WM_CHAR PostMessage.

    Opens tws.exe, waits for the Login dialog, types username + Tab + password + Enter
    using PostMessage (bypasses Java Swing's synthetic input filtering).

    Args:
        notify_telegram: Send a Telegram alert when done (default True).
        wait_seconds:    Seconds to wait for the Login dialog to appear (default 30).
    """
    exe = _tws_exe()
    username = _tws_username()
    password = _tws_password()

    if not exe.exists():
        return f"❌ tws.exe לא נמצא: {exe}"

    # --- Already running ---
    if await _tws_running():
        hwnd = await asyncio.get_event_loop().run_in_executor(None, _get_login_hwnd)
        if hwnd:
            if username and password:
                await asyncio.get_event_loop().run_in_executor(
                    None, _fill_credentials, hwnd, username, password
                )
                msg = "✅ TWS כבר פתוח — פרטי כניסה הוזנו אוטומטית."
            else:
                await asyncio.get_event_loop().run_in_executor(None, _focus_hwnd, hwnd)
                msg = "✅ TWS כבר פתוח בחלון הכניסה — הקלד פרטי כניסה."
        else:
            msg = "✅ TWS כבר פועל ומחובר."
        if notify_telegram:
            try:
                from orchestrator.telegram_notify import send_to_owner
                await send_to_owner(f"🏦 TWS\n\n{msg}")
            except Exception:
                pass
        return msg

    # --- Launch ---
    logger.info("tws_tools: launching %s", exe)
    try:
        subprocess.Popen(
            [str(exe)],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(exe.parent),
        )
    except Exception as exc:
        return f"❌ שגיאה בהפעלת TWS: {exc}"

    # --- Wait for Login dialog ---
    hwnd = 0
    for _ in range(wait_seconds + 20):
        await asyncio.sleep(1)
        hwnd = await asyncio.get_event_loop().run_in_executor(None, _get_login_hwnd)
        if hwnd:
            break

    if hwnd:
        if username and password:
            await asyncio.get_event_loop().run_in_executor(
                None, _fill_credentials, hwnd, username, password
            )
            result = "🏦 *TWS נפתח* ✅\n\nפרטי הכניסה הוזנו אוטומטית — מתחבר..."
        else:
            await asyncio.get_event_loop().run_in_executor(None, _focus_hwnd, hwnd)
            result = (
                "🏦 *TWS נפתח* ✅\n\n"
                "חלון הכניסה מוכן.\n"
                "📱 הקלד שם משתמש וסיסמה ולחץ Enter."
            )
    else:
        result = (
            "🏦 *TWS נפתח*\n\n"
            "חלון הכניסה לא זוהה בזמן — ייתכן שהתחברות אוטומטית קרתה, "
            "או שהחלון עדיין נטען. בדוק את המסך."
        )

    if notify_telegram:
        try:
            from orchestrator.telegram_notify import send_to_owner
            await send_to_owner(result)
        except Exception:
            pass

    return result


@registry.register
async def check_tws_status() -> str:
    """Check whether TWS is running and whether it's at the login screen."""
    running = await _tws_running()
    hwnd = await asyncio.get_event_loop().run_in_executor(None, _get_login_hwnd) if running else 0
    exe = _tws_exe()

    if not running:
        state = "🔴 TWS לא פועל"
    elif hwnd:
        state = "🟡 TWS פתוח בחלון הכניסה (ממתין לסיסמה)"
    else:
        state = "✅ TWS פועל ומחובר"

    return f"**סטטוס TWS:** {state}\n\n**נתיב:** `{exe}`"


@registry.register
async def stop_tws() -> str:
    """Stop TWS by terminating the tws.exe process."""
    if not await _tws_running():
        return "ℹ️ TWS לא פועל — אין מה לעצור."
    try:
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/IM", "tws.exe",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return "🛑 TWS נסגר."
        return f"⚠️ taskkill קוד {proc.returncode}: {err.decode(errors='replace')}"
    except Exception as exc:
        return f"❌ שגיאה: {exc}"
