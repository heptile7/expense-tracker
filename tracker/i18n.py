"""English and Tamil wording.

One file holds every Tamil string: `static/i18n/ta.json`, a flat map from the English text to its
Tamil version. The page uses it for menus, labels and category names; this module uses it for the
sentences the server writes ("What stands out", "Where the money went"). Templates keep their
{placeholders}: "You kept {rate} of your income" -> "உங்கள் வரவில் {rate} மிச்சமானது".

The language is the signed-in person's choice (profiles.language). Outside a request (tests, the MCP
server) everything is English. A missing translation falls back to English, so a new sentence never
breaks the page: it just shows in English until it's added to ta.json.
"""

import functools
import json
import logging

import config
from tracker import profiles

log = logging.getLogger(__name__)

LANGS = ("en", "ta")
TA_FILE = config.BASE_DIR / "static" / "i18n" / "ta.json"


@functools.lru_cache(maxsize=1)
def tamil() -> dict[str, str]:
    try:
        return {k: v for k, v in json.loads(TA_FILE.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    except (OSError, ValueError) as exc:
        log.warning("Tamil wording unavailable: %s", exc)
        return {}


def lang() -> str:
    p = profiles.current()
    return p.language if p and p.language in LANGS else "en"


def say(en: str, **params) -> str:
    """`en` in the current person's language, with {placeholders} filled in."""
    template = tamil().get(en, en) if lang() == "ta" else en
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return en.format(**params)


def word(en: str) -> str:
    """A single label or category name, translated when there's a translation."""
    return tamil().get(en, en) if lang() == "ta" else en
