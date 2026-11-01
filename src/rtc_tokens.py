"""RTC 鉴权 token 签发(customer_rtc 自备频道时用)。

算法与 Vidu 官方 quickstart(lib/tokens.js)逐字段对齐:
- ARTC (阿里云)  : token = sha256(appId + appKey + channelId + userId + timestamp)  → 64 位裸 hex
                   timestamp = 过期 Unix 秒; 客户端可用 authinfo 信封包一层或裸传
- TRTC (腾讯云)  : UserSig (TLS 2.0, hmac-sha256 + zlib + base64 变体)
- Agora          : 暂不实现(官方 quickstart 自带 JS 版, 需要时再补)

签名密钥只在本文件(服务端)使用, 绝不下发浏览器。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import zlib

GSLB_DEFAULT = "https://rgslb.rtc.aliyuncs.com"


# ───────────────────────── ARTC(阿里云 RTC) ─────────────────────────

def artc_token(app_id: str, app_key: str, channel_id: str, user_id: str,
               timestamp: int) -> str:
    return hashlib.sha256(
        f"{app_id}{app_key}{channel_id}{user_id}{timestamp}".encode("utf-8")
    ).hexdigest()


def build_artc_join_envelope(app_id: str, channel_id: str, user_id: str,
                             token: str, timestamp: int,
                             gslb: str = GSLB_DEFAULT) -> str:
    """authinfo 信封(quickstart web/rtc.js 的 buildArtcJoinToken 'authinfo' 形态)。"""
    return json.dumps({
        "appid": app_id,
        "channelid": channel_id,
        "userid": user_id,
        "timestamp": int(timestamp),
        "token": token,
        "gslb": [gslb],
    }, ensure_ascii=False)


def artc_authinfo_b64(app_id: str, app_key: str, channel_id: str, user_id: str,
                      ttl_seconds: int = 24 * 3600, nonce: str = "") -> dict:
    """阿里 RTC 经典 authInfo(HMAC) —— **Vidu 文档说的「ARTC Base64 Token」就是它**:

        dict1 = {"appid","channelid","nonce","timestamp","userid"}   # 键序是协议的一部分
        token = hmac_sha256(app_key, json.dumps(dict1)).hexdigest()
        authInfo = base64(json.dumps(dict1 + {"token": token}))

    timestamp = 过期 Unix 秒。GUID 形态的新版应用(直播+ 实时音视频控制台)用这种。
    """
    ts = int(time.time()) + int(ttl_seconds)
    dict1 = {"appid": app_id, "channelid": channel_id, "nonce": nonce,
             "timestamp": ts, "userid": user_id}
    sig = hmac.new(app_key.encode("utf-8"), json.dumps(dict1).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    dict2 = dict(dict1)
    dict2["token"] = sig
    return {"token": base64.b64encode(json.dumps(dict2).encode("utf-8")).decode("utf-8"),
            "timestamp": ts, "shape": "authinfo_b64"}


def sign_artc(app_id: str, app_key: str, channel_id: str, user_id: str,
              ttl_seconds: int = 24 * 3600, token_shape: str = "authinfo_b64") -> dict:
    """token_shape:
    - authinfo_b64  HMAC authInfo(Base64 Token) —— 默认, 新版应用/Vidu customer_rtc 用
    - concat_json   sha256 裸 hex 包 authinfo JSON 信封(官方 quickstart 默认, 老 RTC 3.0 应用)
    - raw           sha256 裸 hex
    """
    if token_shape in ("", None, "authinfo_b64", "hmac_b64"):
        return artc_authinfo_b64(app_id, app_key, channel_id, user_id, ttl_seconds)
    ts = int(time.time()) + int(ttl_seconds)
    raw = artc_token(app_id, app_key, channel_id, user_id, ts)
    token = build_artc_join_envelope(app_id, channel_id, user_id, raw, ts) \
        if token_shape in ("authinfo", "concat_json") else raw
    return {"token": token, "timestamp": ts,
            "shape": "concat_json" if token != raw else "raw"}


# ───────────────────────── TRTC(腾讯云 RTC) ─────────────────────────

def trtc_usersig(sdk_app_id, secret_key: str, user_id: str,
                 ttl_seconds: int = 24 * 3600) -> str:
    now = int(time.time())
    base = {
        "TLS.ver": "2.0",
        "TLS.identifier": str(user_id),
        "TLS.sdkappid": int(sdk_app_id),
        "TLS.expire": int(ttl_seconds),
        "TLS.time": now,
    }
    # 待签名串的字段顺序是协议的一部分, 不能改
    content = (
        f"TLS.identifier:{base['TLS.identifier']}\n"
        f"TLS.sdkappid:{base['TLS.sdkappid']}\n"
        f"TLS.time:{base['TLS.time']}\n"
        f"TLS.expire:{base['TLS.expire']}\n"
    )
    base["TLS.sig"] = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    compressed = zlib.compress(json.dumps(base, separators=(",", ":")).encode("utf-8"))
    # TRTC base64 变体: +/ → *- , = → _
    return (base64.b64encode(compressed).decode()
            .replace("+", "*").replace("/", "-").replace("=", "_"))


def sign_trtc(sdk_app_id, secret_key: str, user_id: str,
              ttl_seconds: int = 24 * 3600) -> dict:
    return {"token": trtc_usersig(sdk_app_id, secret_key, user_id, ttl_seconds),
            "timestamp": int(time.time()) + int(ttl_seconds), "shape": "usersig"}


# ───────────────────────── 统一入口 ─────────────────────────

def sign_viewers(provider: str, rtc_conf: dict, channel_id: str,
                 users: list[str], ttl_seconds: int = 24 * 3600) -> dict[str, dict]:
    """给多个 user 各签一份观看端凭证: {user_id: {token, timestamp, shape}}。"""
    provider = (provider or "artc").strip().lower()
    out: dict[str, dict] = {}
    if provider == "artc":
        c = rtc_conf.get("artc") or {}
        if not c.get("app_id") or not c.get("app_key"):
            raise RuntimeError("rtc.artc.app_id / app_key 未配置(config.json)")
        for u in users:
            out[u] = sign_artc(c["app_id"], c["app_key"], channel_id, u,
                               ttl_seconds, c.get("token_shape", "authinfo_b64"))
    elif provider == "trtc":
        c = rtc_conf.get("trtc") or {}
        if not c.get("sdk_app_id") or not c.get("secret_key"):
            raise RuntimeError("rtc.trtc.sdk_app_id / secret_key 未配置(config.json)")
        for u in users:
            out[u] = sign_trtc(c["sdk_app_id"], c["secret_key"], u, ttl_seconds)
    else:
        raise RuntimeError(f"customer 模式暂不支持 provider={provider}(支持 artc/trtc)")
    return out
