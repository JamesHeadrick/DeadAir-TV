"""Phase 2: launch a deep link on a Chromecast with Google TV over ADB."""

from __future__ import annotations

import asyncio
import shlex


class ADBError(RuntimeError):
    pass


async def _run(*args: str, timeout: float = 15) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ADBError(f"timed out: {' '.join(args[:3])}")
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise ADBError(text or f"exit code {proc.returncode}")
    return text


async def play_on_tv(tv_ip: str, port: int, url: str) -> str:
    target = f"{tv_ip}:{port}"
    out = await _run("adb", "connect", target)
    # `adb connect` exits 0 even on failure; check its message instead.
    if "connected to" not in out:
        raise ADBError(f"adb connect failed: {out}")
    # `adb shell` joins its args into a remote shell command line, so the URL
    # must be quoted for the remote shell (URLs often contain & and ?).
    try:
        out = await _run(
            "adb", "-s", target, "shell",
            "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url),
        )
    except ADBError as e:
        if "unauthorized" in str(e):
            raise ADBError(f"{e} - accept the USB debugging prompt on the TV, then retry") from e
        raise
    if "Error" in out:
        raise ADBError(out)
    return out
