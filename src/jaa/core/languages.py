"""Natural-language names, so the same language written in English or Polish is one thing."""

from typing import Final

from jaa.core.dedupe import fold

# Language names as English words; Polish names and the stems of their inflected forms
# ("języka angielskiego") map to the same name.
LANGUAGES: Final[dict[str, tuple[str, ...]]] = {
    "english": ("english", "angielsk"),
    "polish": ("polish", "polsk"),
    "german": ("german", "niemieck"),
    "french": ("french", "francusk"),
    "spanish": ("spanish", "hiszpansk"),
    "italian": ("italian", "wlosk"),
    "russian": ("russian", "rosyjsk"),
    "ukrainian": ("ukrainian", "ukrainsk"),
    "czech": ("czech", "czesk"),
    "slovak": ("slovak", "slowack"),
    "dutch": ("dutch", "niderlandzk", "holendersk"),
    "swedish": ("swedish", "szwedzk"),
    "norwegian": ("norwegian", "norwesk"),
    "danish": ("danish", "dunsk"),
    "finnish": ("finnish", "finsk"),
    "portuguese": ("portuguese", "portugalsk"),
    "japanese": ("japanese", "japonsk"),
    "chinese": ("chinese", "chinsk"),
    "hungarian": ("hungarian", "wegiersk"),
    "romanian": ("romanian", "rumunsk"),
}


def language_name(text: str) -> str | None:
    """The English name of the language `text` names, or None."""
    words = fold(text).split()
    for name, stems in LANGUAGES.items():
        if any(w.startswith(stem) for w in words for stem in stems):
            return name
    return None
