"""前台站点的多语言文案处理。"""

from __future__ import annotations

from typing import Any

from .. import lang as lang_mod
from .instcfg import default_copy

COPY_KEYS = ("title", "subtitle", "welcome", "disclaimer")


def content_lang(texts: list[Any], fallback: str = "") -> str:
    blob = " ".join(str(x) for x in texts if x)
    detected = lang_mod.detect(blob)
    if detected in ("ug", "zh", "en"):
        return detected
    return fallback if fallback in ("ug", "zh", "en") else ""


def fill_copy(base: dict[str, Any], lang: str) -> dict[str, Any]:
    defaults = default_copy(lang)
    out: dict[str, Any] = {}
    for key in COPY_KEYS:
        value = str(base.get(key) or "").strip()
        if value and content_lang([value], fallback=lang) != lang:
            value = ""
        out[key] = value or defaults.get(key, "")
    samples = [str(s).strip() for s in (base.get("samples") or []) if str(s).strip()]
    out["samples"] = [s for s in samples if content_lang([s], fallback=lang) == lang]
    if not out["samples"]:
        out["samples"] = defaults.get("samples", [])
    return out
