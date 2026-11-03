"""从本机浏览器（Edge / Chrome）直接借抖音 cookie。

为什么要这个模块：抖音的 sessionid 存在浏览器 profile 的 SQLite 里，加密方式是
Chromium 在 Windows 上的老规矩：

    v10 前缀 + AES-256-GCM，密钥 = DPAPI 解开 Local State 里的 os_crypt.encrypted_key
    加密内容 = 明文去掉 v10 之后的 [nonce(12) + 密文 + tag(16)]

所以我们只要同机器同用户，就能把「已经登录着的 Edge」里的 cookie 原样搬进
Playwright 的 profile，用户不用再扫一次码。

两条硬限制（都实测过）：
- **浏览器开着就拿不到**。Edge 对 Cookies 库是独占打开，`shutil.copy2` 报
  `WinError 32`，`sqlite3` 用 `mode=ro` / `immutable=1` 也是 `unable to open database file`。
  所以先把 Edge 关掉再导。
- Edge 较新版本对 cookie 上了 App-Bound Encryption（v20 前缀），那种解不开。
  遇到就明说，退回扫码。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from astrbot.api import logger


class CookieImportError(RuntimeError):
    """导入失败，message 直接给用户看。"""


def _browser_roots() -> dict[str, Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    return {
        "edge": local / "Microsoft" / "Edge" / "User Data",
        "chrome": local / "Google" / "Chrome" / "User Data",
        "chrome-beta": local / "Google" / "Chrome Beta" / "User Data",
        "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        # 便携版常见位置，留个兜底
        "chromium": roaming / "Chromium" / "User Data",
    }


def available_browsers() -> dict[str, Path]:
    """本机有哪些 Chromium 系浏览器的用户数据目录。"""
    return {k: v for k, v in _browser_roots().items() if (v / "Local State").exists()}


def _cookie_db(root: Path, profile: str = "Default") -> Path:
    for rel in (
        Path(profile) / "Network" / "Cookies",
        Path(profile) / "Cookies",
    ):
        p = root / rel
        if p.exists():
            return p
    raise CookieImportError(f"{root} 下找不到 Cookies 库（找过 {profile}/Network/Cookies）")


def _dpapi_unprotect(blob: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(data: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(to_blob(blob)), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise CookieImportError("DPAPI 解密失败（不是同一 Windows 用户？）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _master_key(root: Path) -> bytes:
    state = root / "Local State"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CookieImportError(f"读不了 {state}：{exc}") from exc
    enc_b64 = (data.get("os_crypt") or {}).get("encrypted_key")
    if not enc_b64:
        raise CookieImportError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc_b64)
    if not raw.startswith(b"DPAPI"):
        raise CookieImportError("os_crypt.encrypted_key 不是 DPAPI 包（可能是系统级加密）")
    return _dpapi_unprotect(raw[5:])


def _decrypt_value(enc: bytes, key: bytes) -> str:
    if not enc:
        return ""
    if enc[:3] == b"v20":
        raise CookieImportError(
            "这个浏览器的 cookie 用了 App-Bound Encryption（v20），解不开。"
            "改用扫码登录（/dy login），或在浏览器里关掉「增强安全」后重试。"
        )
    if enc[:3] != b"v10":
        # 有些库是明文存的（老版本 / 测试环境）
        try:
            return enc.decode("utf-8")
        except Exception:  # noqa: BLE001
            return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # noqa: BLE001
        raise CookieImportError("缺 cryptography 库，装一下：pip install cryptography") from exc
    payload = enc[3:]
    nonce, ct = payload[:12], payload[12:]
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise CookieImportError(f"AES-GCM 解密失败：{type(exc).__name__}") from exc


def read_cookies(
    browser: str = "edge",
    *,
    profile: str = "Default",
    host_like: str = "douyin",
) -> list[dict]:
    """读出某个浏览器里 host 含 host_like 的 cookie，返回 Playwright add_cookies 能吃的格式。"""
    roots = _browser_roots()
    root = roots.get(browser)
    if root is None or not (root / "Local State").exists():
        raise CookieImportError(f"没找到 {browser} 的用户数据目录（{root}）")

    db = _cookie_db(root, profile)
    tmp_dir = Path(tempfile.mkdtemp(prefix="dyck_"))
    tmp = tmp_dir / "Cookies"
    try:
        shutil.copy2(db, tmp)
    except PermissionError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise CookieImportError(
            f"{browser} 正开着，Cookies 库被独占——先把 {browser} 完全退出（任务栏也退），再导一次。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise CookieImportError(f"复制 Cookies 库失败：{exc}") from exc

    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = con.execute(
            "select host_key, name, value, encrypted_value, path, expires_utc, "
            "is_secure, is_httponly from cookies "
            "where host_key like ? or host_key like ?",
            (f"%{host_like}%", f"%.{host_like}%"),
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise CookieImportError(f"读 Cookies 库失败：{exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    key = _master_key(root)
    out: list[dict] = []
    for host, name, value, enc, path, expires, secure, http_only in rows:
        plain = value or ""
        if not plain and enc:
            try:
                plain = _decrypt_value(enc, key)
            except CookieImportError as exc:
                logger.warning("解不开 %s：%s", name, exc)
                continue
        if not plain:
            continue
        # Chromium 的 expires_utc 是 1601-01-01 起的微秒
        exp = int(expires / 1_000_000 - 11644473600) if expires else -1
        out.append(
            {
                "name": name,
                "value": plain,
                "domain": host,
                "path": path or "/",
                "expires": exp,
                "secure": bool(secure),
                "httpOnly": bool(http_only),
                "sameSite": "Lax",
            }
        )
    return out


def read_douyin_session(browser: str = "edge", profile: str = "Default") -> list[dict]:
    """只要抖音那几个关键 cookie，顺序固定，方便日志里数。"""
    want = {
        "sessionid",
        "sessionid_ss",
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
        "sid_guard",
        "ttwid",
        "passport_csrf_token",
        "passport_csrf_token_default",
        "odin_tt",
        "msToken",
        "tt_scid",
    }
    cookies = read_cookies(browser, profile=profile, host_like="douyin")
    picked = [c for c in cookies if c["name"] in want]
    if not any(c["name"] in {"sessionid", "sessionid_ss"} for c in picked):
        raise CookieImportError(
            f"{browser} 里没有抖音的 sessionid —— 这个浏览器没登录抖音，先在里面登录一次。"
        )
    return picked