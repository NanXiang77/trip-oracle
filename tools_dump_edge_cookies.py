# -*- coding: utf-8 -*-
"""从 Edge 的 Cookies 数据库解密指定域名的 Cookie（用户本人授权，用于自己的登录态）。"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from Crypto.Cipher import AES

EDGE_USER_DATA = r"C:\Users\Nan\AppData\Local\Microsoft\Edge\User Data"
DOMAIN_LIKE = sys.argv[1] if len(sys.argv) > 1 else "%baidu.com%"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(encrypted: bytes) -> bytes:
    blob_in = DATA_BLOB(len(encrypted), ctypes.cast(
        ctypes.create_string_buffer(encrypted, len(encrypted)), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData failed")
    data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return data


def get_master_key() -> bytes:
    with open(os.path.join(EDGE_USER_DATA, "Local State"), encoding="utf-8") as f:
        encrypted_key = json.load(f)["os_crypt"]["encrypted_key"]
    key = base64.b64decode(encrypted_key)[5:]  # 去掉 DPAPI 前缀
    return dpapi_decrypt(key)


def decrypt_cookie_value(blob: bytes, master_key: bytes) -> str:
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v11"):
        iv, payload = blob[3:15], blob[15:]
        cipher = AES.new(master_key, AES.MODE_GCM, nonce=iv)
        try:
            return cipher.decrypt_and_verify(payload[:-16], payload[-16:]).decode("utf-8", "replace")
        except ValueError:
            # Edge 新版 App-Bound 加密失败时的表现
            return "<APP-BOUND-ENCRYPTED>"
    # 旧版 DPAPI
    try:
        return dpapi_decrypt(blob).decode("utf-8", "replace")
    except OSError:
        return "<DPAPI-FAIL>"


def main():
    master_key = get_master_key()
    src = os.path.join(EDGE_USER_DATA, "Default", "Network", "Cookies")
    uri = "file:" + src.replace("\\", "/") + "?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA journal_mode=off")
    except sqlite3.Error:
        tmp = os.path.join(tempfile.gettempdir(), "edge_cookies_copy.db")
        subprocess.run(["esentutl", "/y", src, "/d", tmp, "/o"], check=True,
                       capture_output=True)
        conn = sqlite3.connect(tmp)
    rows = conn.execute(
        "SELECT host_key, name, value, encrypted_value, expires_utc "
        "FROM cookies WHERE host_key LIKE ? ORDER BY host_key, name", (DOMAIN_LIKE,)).fetchall()
    conn.close()
    os.remove(tmp)

    results = {}
    for host, name, plain, enc, exp in rows:
        val = plain or decrypt_cookie_value(enc, master_key)
        results[f"{host}|{name}"] = val
        shown = val if val not in ("<APP-BOUND-ENCRYPTED>", "<DPAPI-FAIL>") else val
        print(f"{host}\t{name}\t{shown[:80]}{'...' if len(shown) > 80 else ''}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_cookies.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"\n[total {len(rows)}] saved -> {out}")


if __name__ == "__main__":
    main()
