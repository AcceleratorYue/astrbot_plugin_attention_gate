"""ONNX 模型来源、自动下载与路径独立性测试。"""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from send_gate.onnx_rhythm import (  # noqa: E402
    MIN_FILE_BYTES,
    MODELSCOPE_FILE_URL,
    MODELSCOPE_PATHS,
    MODELSCOPE_REPOS,
    REQUIRED_FILES,
    OnnxRhythm,
)


class FakeLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes = b"", headers: dict | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class OnnxSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "attention"
        self.config = FakeConfig(
            {
                "send_gate_onnx_enable": True,
                "send_gate_onnx_model_type": "normal",
                "send_gate_onnx_auto_download": True,
            }
        )
        self.rhythm = OnnxRhythm(
            config_getter=self.config.get,
            data_dir_getter=lambda: self.data_dir,
            logger=FakeLogger(),
        )
        self.payloads: dict[str, bytes] = {}
        self.digests: dict[str, str] = {}
        for relative in REQUIRED_FILES:
            size = max(MIN_FILE_BYTES[relative], 1)
            seed = relative.encode("utf-8") + b"-"
            repeats = (size + len(seed) - 1) // len(seed)
            self.payloads[relative] = (seed * repeats)[:size]
            self.digests[relative] = hashlib.sha256(self.payloads[relative]).hexdigest()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_urlopen(self, request, timeout=0):
        url = request.full_url
        remote = url.split("/resolve/master/", 1)[1]
        local = next(key for key, value in MODELSCOPE_PATHS.items() if value == remote)
        data = self.payloads[local]
        if request.get_header("Range"):
            return FakeResponse(
                headers={
                    "Content-Range": f"bytes 0-0/{len(data)}",
                    "X-Linked-Etag": self.digests[local],
                }
            )
        return FakeResponse(data)

    def test_model_path_never_falls_back_to_other_plugin(self) -> None:
        legacy = Path(self.tmp.name) / "astrbot_plugin_debounce" / "models" / "normal"
        legacy.mkdir(parents=True)
        (legacy / "model.onnx").write_bytes(b"x" * 1_000_001)
        path = self.rhythm._model_path()
        self.assertEqual(path, self.data_dir / "models" / "normal")
        self.assertNotIn("debounce", path.as_posix())

    def test_custom_path_is_respected(self) -> None:
        custom = Path(self.tmp.name) / "custom-model"
        self.config.values["send_gate_onnx_model_path"] = str(custom)
        self.assertEqual(self.rhythm._model_path(), custom)

    def test_download_uses_modelscope_files_and_upstream_checksum(self) -> None:
        target = self.data_dir / "models" / "normal"
        seen_urls: list[str] = []

        def fake_urlopen(request, timeout=0):
            seen_urls.append(request.full_url)
            return self._fake_urlopen(request, timeout)

        with patch("urllib.request.urlopen", fake_urlopen):
            self.rhythm._download_all(target)

        self.assertIn(MODELSCOPE_REPOS["normal"], seen_urls[0])
        self.assertIn("/model/model.onnx", " ".join(seen_urls))
        self.assertNotIn("/master/model.onnx", " ".join(seen_urls))
        for relative in REQUIRED_FILES:
            self.assertTrue((target / relative).is_file(), relative)
            self.assertEqual((target / relative).read_bytes(), self.payloads[relative])
        self.assertTrue(self.rhythm._model_complete(target))

    def test_hash_mismatch_redownloads_existing_file(self) -> None:
        target = self.data_dir / "models" / "normal"
        for relative in REQUIRED_FILES:
            bad = target / relative
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_bytes(b"y" * len(self.payloads[relative]))

        with patch("urllib.request.urlopen", self._fake_urlopen):
            self.rhythm._download_all(target)
            self.assertTrue(self.rhythm._model_valid(target))
            self.assertEqual(
                (target / "model.onnx").read_bytes(), self.payloads["model.onnx"]
            )

    def test_remote_metadata_is_read_from_headers(self) -> None:
        url = MODELSCOPE_FILE_URL.format(
            repo=MODELSCOPE_REPOS["normal"], path="model/model.onnx"
        )
        with patch("urllib.request.urlopen", self._fake_urlopen):
            size, digest = self.rhythm._remote_file_meta(url)
        self.assertEqual(size, len(self.payloads["model.onnx"]))
        self.assertEqual(digest, self.digests["model.onnx"])

    def test_incomplete_default_path_is_not_ready(self) -> None:
        self.assertFalse(self.rhythm._model_complete())
        self.assertIsNone(self.rhythm._classifier)
        # 未下载完成时不会读取其它插件目录，也不会伪造分类器。
        self.assertFalse(self.rhythm._model_complete())


if __name__ == "__main__":
    unittest.main()
