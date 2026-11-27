"""可选的 ONNX 句尾节奏模型。

只作为发送门控的等待节奏信号，不作为最终语义裁判。
模型不存在或依赖缺失时自动降级为纯时间窗口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable


class OnnxRhythm:
    def __init__(
        self,
        *,
        config_getter: Callable[[str, Any], Any],
        data_dir_getter: Callable[[], Path],
        logger: Any,
    ) -> None:
        self._cfg = config_getter
        self._data_dir_getter = data_dir_getter
        self._logger = logger
        self._classifier: Any = None
        self._load_lock = asyncio.Lock()
        self._failed = False
        self._warned = False

    def _enabled(self) -> bool:
        return bool(self._cfg("send_gate_onnx_enable", False))

    def _model_path(self) -> Path:
        raw = str(self._cfg("send_gate_onnx_model_path", "") or "").strip()
        if raw:
            return Path(raw)
        model_type = str(
            self._cfg("send_gate_onnx_model_type", "normal") or "normal"
        )
        try:
            plugin_data = self._data_dir_getter().parent
        except Exception:
            plugin_data = Path("D:/QQbot/astrbot/data/plugin_data")
        return plugin_data / "astrbot_plugin_debounce" / "models" / model_type

    async def score(self, text: str) -> float | None:
        if not self._enabled() or self._failed:
            return None
        if not str(text or "").strip():
            return None
        try:
            classifier = await self._load()
            if classifier is None:
                return None
            value, _ = await asyncio.to_thread(
                classifier.predict, str(text)[:2000]
            )
            return max(0.0, min(1.0, float(value)))
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            if not self._warned:
                self._warned = True
                self._logger.warning("[send_gate] ONNX 节奏不可用，降级为纯窗口: %s", exc)
            return None

    async def _load(self) -> Any:
        if self._classifier is not None:
            return self._classifier
        async with self._load_lock:
            if self._classifier is not None:
                return self._classifier
            path = self._model_path()
            model_path = path / "model.onnx"
            tokenizer_path = path / "tokenizer"
            if not model_path.exists():
                raise FileNotFoundError(f"ONNX 模型不存在: {model_path}")
            if not tokenizer_path.exists():
                raise FileNotFoundError(f"ONNX tokenizer 不存在: {tokenizer_path}")
            self._classifier = await asyncio.to_thread(
                _load_classifier, str(model_path), str(tokenizer_path)
            )
            self._logger.info("[send_gate] ONNX 节奏模型已加载: %s", model_path)
            return self._classifier


class _Classifier:
    def __init__(self, model_path: str, tokenizer_path: str) -> None:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        self.session = ort.InferenceSession(model_path)

    def predict(self, text: str) -> tuple[float, float]:
        import numpy as np

        inputs = self.tokenizer(
            text,
            return_tensors="np",
            padding=True,
            truncation=True,
            max_length=64,
        )
        outputs = self.session.run(
            output_names=["logits"],
            input_feed={
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"],
            },
        )
        logits = outputs[0]
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / np.sum(exp_logits, axis=-1, keepdims=True)
        score = float(probs[0][1])
        return score, score


def _load_classifier(model_path: str, tokenizer_path: str) -> _Classifier:
    return _Classifier(model_path, tokenizer_path)
