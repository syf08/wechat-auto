"""Windows DPAPI 本地密钥存储。

密钥由当前 Windows 用户的 DPAPI 凭据加密，离开这台用户配置文件后无法直接
解密。项目只保存密文，不把 API Key 写进源码或普通文本配置。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

SECRET_FILENAME = "config_secret.bin"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_DESCRIPTION = "WeChat auto reply DeepSeek API key"


class SecretStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(wintypes.BYTE)),
    ]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DataBlob),
    wintypes.LPCWSTR,
    ctypes.POINTER(_DataBlob),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DataBlob),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DataBlob),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(_DataBlob),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DataBlob),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
k32.LocalFree.argtypes = [ctypes.c_void_p]
k32.LocalFree.restype = ctypes.c_void_p


def default_secret_path() -> Path:
    return Path(__file__).with_name(SECRET_FILENAME)


def _blob_for(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)),
    )
    return blob, buffer


def _take_blob(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            k32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def protect_secret(plaintext: bytes) -> bytes:
    """用当前 Windows 用户的 DPAPI 凭据加密字节串。"""
    if not plaintext:
        raise SecretStoreError("不能保存空密钥")
    source, source_buffer = _blob_for(plaintext)
    # 新建 Windows 用户配置文件或安全软件短暂占用凭据库时，DPAPI 偶尔
    # 首次调用会失败；有限重试可以避免启动阶段的瞬时失败。
    for attempt in range(3):
        result = _DataBlob()
        ctypes.set_last_error(0)
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            _DESCRIPTION,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        )
        if ok:
            del source_buffer
            return _take_blob(result)
        error = ctypes.get_last_error()
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    del source_buffer
    raise SecretStoreError(f"DPAPI 加密失败（Windows 错误 {error}）")


def unprotect_secret(ciphertext: bytes) -> bytes:
    """解密由当前 Windows 用户创建的 DPAPI 密文。"""
    if not ciphertext:
        raise SecretStoreError("密钥文件为空")
    source, source_buffer = _blob_for(ciphertext)
    for attempt in range(3):
        result = _DataBlob()
        description = wintypes.LPWSTR()
        ctypes.set_last_error(0)
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        )
        if description:
            k32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        if ok:
            del source_buffer
            return _take_blob(result)
        error = ctypes.get_last_error()
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    del source_buffer
    raise SecretStoreError(f"DPAPI 解密失败（Windows 错误 {error}）")


def save_secret(api_key: str, path: Path | None = None) -> Path:
    """原子写加密密钥文件，并返回实际路径。"""
    value = (api_key or "").strip()
    if not value:
        raise SecretStoreError("API Key 不能为空")
    target = path or default_secret_path()
    encrypted = protect_secret(value.encode("utf-8"))
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SecretStoreError(f"写入加密密钥文件失败：{exc}") from exc
    return target


def load_secret(path: Path | None = None) -> str:
    """读取并解密密钥；文件不存在时返回空串。"""
    target = path or default_secret_path()
    try:
        encrypted = target.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise SecretStoreError(f"读取加密密钥文件失败：{exc}") from exc
    try:
        return unprotect_secret(encrypted).decode("utf-8").strip()
    except UnicodeError as exc:
        raise SecretStoreError("解密后的 API Key 不是有效 UTF-8") from exc
