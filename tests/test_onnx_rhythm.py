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

from send_gate import onnx_rhythm  # noqa: E402
from send_gate.onnx_rhythm import (  # noqa: E402
    EXPECTED_FILES,
    MIN_FILE_BYTES,
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
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

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

    def tearDown(self) -> None:
        self.tmp.cleanup()

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

    def test_download_uses_modelscope_files(self) -> None:
        target = self.data_dir / "models" / "normal"
        seen_urls: list[str] = []
        payloads: dict[str, bytes] = {}
        expected: dict[str, tuple[int, str]] = {}
        for relative in REQUIRED_FILES:
            size = max(MIN_FILE_BYTES[relative], 1)
            payloads[relative] = b"x" * size
            expected[relative] = (
                size,
                hashlib.sha256(payloads[relative]).hexdigest(),
            )

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            seen_urls.append(url)
            remote = url.split("/resolve/master/", 1)[1]
            local = next(
                key for key, value in MODELSCOPE_PATHS.items() if value == remote
            )
            return FakeResponse(payloads[local])

        with (
            patch("urllib.request.urlopen", fake_urlopen),
            patch.dict(onnx_rhythm.EXPECTED_FILES, {"normal": expected}),
        ):
            self.rhythm._download_all(target)

        self.assertIn(MODELSCOPE_REPOS["normal"], seen_urls[0])
        self.assertIn("/model/model.onnx", seen_urls[0])
        self.assertNotIn("/master/model.onnx", " ".join(seen_urls))
        for relative in REQUIRED_FILES:
            self.assertTrue((target / relative).is_file(), relative)
        self.assertTrue(self.rhythm._model_complete(target))

    def test_hash_mismatch_redownloads_existing_file(self) -> None:
        target = self.data_dir / "models" / "normal"
        payloads: dict[str, bytes] = {}
        expected: dict[str, tuple[int, str]] = {}
        for relative in REQUIRED_FILES:
            size = max(MIN_FILE_BYTES[relative], 1)
            payloads[relative] = b"x" * size
            expected[relative] = (
                size,
                hashlib.sha256(payloads[relative]).hexdigest(),
            )
            bad = target / relative
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_bytes(b"y" * size)

        def fake_urlopen(request, timeout=0):
            remote = request.full_url.split("/resolve/master/", 1)[1]
            local = next(
                key for key, value in MODELSCOPE_PATHS.items() if value == remote
            )
            return FakeResponse(payloads[local])

        with (
            patch("urllib.request.urlopen", fake_urlopen),
            patch.dict(onnx_rhythm.EXPECTED_FILES, {"normal": expected}),
        ):
            self.rhythm._download_all(target)
            self.assertTrue(self.rhythm._model_valid(target))
            self.assertEqual(
                (target / "model.onnx").read_bytes(), payloads["model.onnx"]
            )

    def test_expected_table_matches_known_normal_model(self) -> None:
        size, digest = EXPECTED_FILES["normal"]["model.onnx"]
        self.assertEqual(size, 154_004_327)
        self.assertEqual(
            digest,
            "0e7b4c63cc01b84d1fdf09f7483969678ca2aa227d26fc82245fee7a4d6e6e7b",
        )

    def test_incomplete_default_path_is_not_ready(self) -> None:
        self.assertFalse(self.rhythm._model_complete())
        self.assertIsNone(self.rhythm._classifier)
        # 未下载完成时不会读取其它插件目录，也不会伪造分类器。
        self.assertFalse(self.rhythm._model_complete())


if __name__ == "__main__":
    unittest.main()
