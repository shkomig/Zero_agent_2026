"""Native desktop notifications — used to alert the user when the agent needs a
Human-in-the-Loop approval, so a prompt isn't missed while the browser tab is in
the background.

Windows-only (the project's target OS). Best-effort by design: it fires a toast
via a detached PowerShell process using the built-in WinRT API — **no extra
dependency** (consistent with the project's non-goals) — and NEVER raises into
the caller. If anything goes wrong (non-Windows, PowerShell missing, WinRT
unavailable) it silently no-ops; the in-chat Approve/Deny prompt is the source of
truth, the toast is only a heads-up.

Title/body are passed via environment variables (not interpolated into the
script) so a tool name or argument can't break out of the PowerShell command.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("zero_agent.notify")

# Raw WinRT toast — reads the text from env vars set on the child process, so no
# user/tool string is ever spliced into the script body.
_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $tpl.GetElementsByTagName("text")
$texts.Item(0).AppendChild($tpl.CreateTextNode($env:ZERO_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($tpl.CreateTextNode($env:ZERO_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def notify(title: str, body: str) -> None:
    """Fire a desktop toast. Non-blocking and never raises.

    Returns immediately — the toast is shown by a detached PowerShell process so
    it never stalls the asyncio event loop or the agent run.
    """
    if sys.platform != "win32":
        return  # only Windows is supported; silently no-op elsewhere
    try:
        env = dict(os.environ)
        # Toasts truncate; keep the body short and informative.
        env["ZERO_TOAST_TITLE"] = (title or "Zero Agent")[:120]
        env["ZERO_TOAST_BODY"] = (body or "")[:200]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _TOAST_PS,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:  # noqa: BLE001 — a notification must never break a run
        log.debug("desktop notification failed", exc_info=True)
