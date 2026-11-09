"""Read the human-readable briefing stored in a LingYi scenario container."""

import base64
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path


class _BriefText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.hidden += 1
        elif tag in ("p", "br", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.hidden -= 1
        elif tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _read_container(path):
    raw = Path(path).read_bytes()
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        # Some Windows exports omit encoding="GBK" while storing Chinese titles
        # as GBK. Preserve declared encodings and genuine malformed-XML errors.
        declaration = re.match(br'(?:\xef\xbb\xbf)?\s*<\?xml\b[^>]*\?>', raw)
        if declaration and re.search(br'\bencoding\s*=', declaration.group(), re.IGNORECASE):
            raise
        try:
            raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                return ET.fromstring(raw.decode("gb18030"))
            except (UnicodeDecodeError, ET.ParseError):
                raise error from None
        raise


def read_scenario_brief(path):
    encoded = _read_container(path).findtext("ScenDescription")
    if not encoded:
        return ""
    html = base64.b64decode(encoded, validate=True).decode("utf-8")
    parser = _BriefText()
    parser.feed(html)
    lines = (re.sub(r"[\t \u00a0]+", " ", line).strip()
             for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
