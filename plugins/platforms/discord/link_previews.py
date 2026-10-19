"""Conservative Discord Markdown normalization, without changing URL destinations."""

import re
from urllib.parse import urlsplit

_URL_LABEL = re.compile(r"^(?:https?://|www\.|[^\s/]+\.[a-z]{2,}(?:[/:?#]|$))", re.IGNORECASE)
_MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|png|svg|webp|mp4|webm|mov|mp3|wav|ogg|m4a)$", re.IGNORECASE)


def _closing(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 1
    pos = start + 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if not depth:
                return pos
        pos += 1
    return None


def _link_end(text: str, start: int) -> tuple[int, int] | None:
    label_end = _closing(text, start, "[", "]")
    if label_end is None:
        return None
    if text[label_end + 1:label_end + 2] != "(":
        return label_end, label_end
    end = _closing(text, label_end + 1, "(", ")")
    return (label_end, end) if end is not None else None


def suppress_descriptive_previews(text: str) -> str:
    """Angle-wrap complete descriptive HTTP link targets; preserve all other bytes.

    An unfinished code span/fence protects the remaining stream snapshot. This
    is intentionally conservative: ambiguous syntax stays unchanged rather than
    risking changes to code, image markup or a URL that an operator meant to embed.
    The adapter supplies accumulated stream text, so each edit is independent.
    """
    out = []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            out.append(text[pos:pos + 2])
            pos += 2
            continue
        if char in "`~":
            run = 1
            while pos + run < len(text) and text[pos + run] == char:
                run += 1
            if char == "`" or run >= 3:
                marker = char * run
                if run >= 3:
                    close = re.search(r"(?m)^ {0,3}" + re.escape(char) + "{" + str(run) + r",}[ \t]*(?:\n|$)", text[pos + run:])
                else:
                    close = re.search(r"(?<!`)" + re.escape(marker) + r"(?!`)", text[pos + run:])
                stop = len(text) if close is None else pos + run + close.end()
                out.append(text[pos:stop])
                pos = stop
                continue
        if char == "<":
            end = text.find(">", pos + 1)
            stop = len(text) if end < 0 else end + 1
            out.append(text[pos:stop])
            pos = stop
            continue
        bare = re.match(r"https?://[^\s<>]+", text[pos:]) if text.startswith(("http://", "https://"), pos) else None
        if bare:
            out.append(bare[0])
            pos += len(bare[0])
            continue
        image = char == "!" and text[pos + 1:pos + 2] == "["
        start = pos + 1 if image else pos
        link = _link_end(text, start) if text[start:start + 1] == "[" else None
        if text[start:start + 1] == "[" and link is None:
            # An unfinished label/destination protects the suffix. Never rescan
            # each nested opener in a malformed accumulated stream snapshot.
            out.append(text[pos:])
            break
        if link:
            label_end, end = link
            if end == label_end:
                out.append(text[pos:end + 1])
                pos = end + 1
                continue
            label = text[start + 1:label_end].strip()
            target = text[label_end + 2:end]
            # Titles, escapes and already-angle-wrapped targets are left verbatim.
            eligible = (not image and target.startswith(("https://", "http://"))
                        and not any(c.isspace() or c in "<>\\" for c in target)
                        and not _URL_LABEL.match(label))
            if eligible:
                try:
                    parsed = urlsplit(target)
                    eligible = bool(parsed.netloc) and not _MEDIA.search(parsed.path) and label != target.split("://", 1)[1]
                except ValueError:
                    eligible = False
            out.append(text[pos:label_end + 2] + "<" + target + ">)" if eligible else text[pos:end + 1])
            pos = end + 1
            continue
        out.append(char)
        pos += 1
    return "".join(out)
