"""Selectable speech engines with a shared 24 kHz PCM output contract."""


def engine_name(conf: dict) -> str:
    name = str(conf.get("engine") or "omnivoice").strip().lower()
    if name not in {"breezeblue", "breeze", "omnivoice"}:
        raise RuntimeError(f"不支持的语音引擎: {name}")
    return name


def create_tts(conf: dict):
    name = engine_name(conf)
    if name == "breezeblue":
        from .breezeblue import BreezeBlueTTS
        return BreezeBlueTTS(conf.get("breezeblue_base", "https://api.breeze.blue/v1"),
                             conf.get("breezeblue_key_file", ""),
                             conf.get("breezeblue_timeout_sec", 120))
    if name == "breeze":
        from .breeze import BreezeTTS
        return BreezeTTS(conf.get("breeze_base", "http://127.0.0.1:17860"),
                         conf.get("breeze_timeout_sec", 300))
    from .omnivoice import OmniVoiceTTS
    return OmniVoiceTTS(conf.get("omnivoice_base", ""),
                        conf.get("omnivoice_timeout_sec", 120))


def scan_voices(conf: dict) -> dict:
    if engine_name(conf) == "breezeblue":
        from .breezeblue import scan_voices as scan
    elif engine_name(conf) == "breeze":
        from .breeze import scan_voices as scan
    else:
        from .omnivoice import scan_voices as scan
    return scan(conf.get("voices_dir", ""))
