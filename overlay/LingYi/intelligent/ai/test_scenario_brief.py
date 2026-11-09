"""Scenario container encoding checks; starts no model or simulation engine."""

import base64
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scenario_brief import read_scenario_brief


class ScenarioBriefTest(unittest.TestCase):
    def test_supported_container_encodings_preserve_utf8_brief(self):
        html = "<html><head><style>hidden</style></head><body><p>红方任务</p><p>蓝方目标</p></body></html>"
        encoded = base64.b64encode(html.encode("utf-8")).decode("ascii")
        for encoding, declaration in (("utf-8", '<?xml version="1.0"?>'),
                                      ("utf-8-sig", '<?xml version="1.0"?>'),
                                      ("gb18030", '<?xml version="1.0"?>'),
                                      ("gbk", '<?xml version="1.0"?>'),
                                      ("utf-8", '<?xml version="1.0" encoding="UTF-8"?>'),
                                      ("utf-16", '<?xml version="1.0" encoding="UTF-16"?>')):
            with self.subTest(encoding=encoding, declaration=declaration), tempfile.TemporaryDirectory() as directory:
                xml = (declaration + "\n<ScenContainer>\n<ScenTitle>诺曼底2027</ScenTitle>\n"
                       "<ScenDescription>%s</ScenDescription></ScenContainer>" % encoded)
                path = Path(directory) / "test.scen"
                path.write_bytes(xml.encode(encoding))
                self.assertEqual(read_scenario_brief(path), "红方任务\n蓝方目标")

    def test_invalid_xml_and_incorrect_declared_encoding_still_fail(self):
        cases = [b"<ScenContainer><bad&></ScenContainer>",
                 '<ScenContainer><ScenTitle>诺曼底</ScenTitle>'.encode("gbk"),
                 ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<ScenContainer><ScenTitle>诺曼底</ScenTitle></ScenContainer>').encode("gbk")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.scen"
            for raw in cases:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaises(ET.ParseError):
                        read_scenario_brief(path)

    def test_empty_description_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.scen"
            path.write_bytes("<ScenContainer><ScenTitle>诺曼底</ScenTitle></ScenContainer>".encode("gbk"))
            self.assertEqual(read_scenario_brief(path), "")


if __name__ == "__main__":
    unittest.main()
