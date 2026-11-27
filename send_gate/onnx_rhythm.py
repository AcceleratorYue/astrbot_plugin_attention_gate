"""可选的 ONNX 句尾节奏模型。

只作为发送门控的等待节奏信号，不作为最终语义裁判。模型不存在、下载
失败或依赖缺失时自动降级为纯时间窗口。

模型来源（ModelScope，作者 advent259141）：
- small:  advent259141/astrbot_debouncer_small
- normal: advent259141/astrbot_debouncer_normal

本模块直接使用标准库下载公开模型文件，不依赖其它 AstrBot 插件的目录，
也不需要安装 modelscope 包。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

MODELSCOPE_REPOS = {
    "small": "advent259141/astrbot_debouncer_small",
    "normal": "advent259141/astrbot_debouncer_normal",
}
MODELSCOPE_FILE_URL = "https://modelscope.cn/models/{repo}/resolve/master/{path}"
REQUIRED_FILES = (
    "model.onnx",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/special_tokens_map.json",
    "tokenizer/vocab.txt",
)
# 本地文件名 -> ModelScope 仓库内路径。上游把 ONNX 放在 model/ 子目录，
# tokenizer 放在仓库根目录；两者不能混用同一个相对路径。
MODELSCOPE_PATHS = {
    "model.onnx": "model/model.onnx",
    "tokenizer/tokenizer.json": "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json": "tokenizer/tokenizer_config.json",
    "tokenizer/special_tokens_map.json": "tokenizer/special_tokens_map.json",
    "tokenizer/vocab.txt": "tokenizer/vocab.txt",
}
# 本地相对路径 -> (字节数, SHA256)。用于发现 CDN 截断、旧版残留和
# 上游文件被替换；固定的是当前 ModelScope 仓库 master 的 LFS 对象。
EXPECTED_FILES: dict[str, dict[str, tuple[int, str]]] = {
    "normal": {
        "model.onnx": (
            154_004_327,
            "0e7b4c63cc01b84d1fdf09f7483969678ca2aa227d26fc82245fee7a4d6e6e7b",
        ),
        "tokenizer/tokenizer.json": (
            439_387,
            "c9c9a74e179a68973f69f4d9dc6245c3563b350dffa313f29b875ce9619e99c9",
        ),
        "tokenizer/tokenizer_config.json": (
            1_219,
            "56cf9402c068e2d7e848cd0568c117052229a7c92e6ae4808e4aa892219f0067",
        ),
        "tokenizer/special_tokens_map.json": (
            695,
            "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
        ),
        "tokenizer/vocab.txt": (
            109_540,
            "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
        ),
    },
    "small": {
        "model.onnx": (
            12_801_841,
            "07b3b3fcfa3bed4f06cdf6acb0a0e31c1c5cc479c0347a40ed6856eca44347f9",
        ),
        "tokenizer/tokenizer.json": (
            439_387,
            "c9c9a74e179a68973f69f4d9dc6245c3563b350dffa313f29b875ce9619e99c9",
        ),
        "tokenizer/tokenizer_config.json": (
            1_270,
            "ae57eda34a3d4e3bbab5edd30c5b7e4ee3c493fa48c2e1af1443b6bd619afc19",
        ),
        "tokenizer/special_tokens_map.json": (
            125,
            "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
        ),
        "tokenizer/vocab.txt": (
            109_540,
            "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
        ),
    },
}
MIN_FILE_BYTES = {
    "model.onnx": 1_000_000,
    "tokenizer/tokenizer.json": 10_000,
    "tokenizer/tokenizer_config.json": 100,
    "tokenizer/special_tokens_map.json": 100,
    "tokenizer/vocab.txt": 10_000,
}
RETRY_SECONDS = 600.0


class OnnxRhythm:
    def __init__(
        self,
        *,
        config_getter: Callable[[str, Any], Any],
        data_dir_getter: Callable[[], Path],
        logger: Any,
        user_agent: str = "astrbot-plugin-attention-gate",
    ) -> None:
        self._cfg = config_getter
        self._data_dir_getter = data_dir_getter
        self._logger = logger
        self._user_agent = user_agent
        self._classifier: Any = None
        self._load_lock = asyncio.Lock()
        self._warmup_task: asyncio.Task[Any] | None = None
        self._warmup_tasks: set[asyncio.Task[Any]] = set()
        self._cancel_requested = False
        self._last_warmup_at = 0.0
        self._failed = False
        self._warned = False
        self._warned_download = False

    # ------------------------------------------------------------------
    # 配置 / 路径
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self._cfg("send_gate_onnx_enable", False))

    def _model_type(self) -> str:
        raw = str(self._cfg("send_gate_onnx_model_type", "normal") or "normal").strip()
        return raw if raw in MODELSCOPE_REPOS else "normal"

    def _custom_model_path(self) -> Path | None:
        raw = str(self._cfg("send_gate_onnx_model_path", "") or "").strip()
        return Path(raw) if raw else None

    def _model_path(self) -> Path:
        custom = self._custom_model_path()
        if custom is not None:
            return custom
        try:
            own_data = self._data_dir_getter()
        except Exception:
            own_data = Path(".")
        return own_data / "models" / self._model_type()

    def _model_complete(self, path: Path | None = None) -> bool:
        root = path or self._model_path()
        for relative, minimum in MIN_FILE_BYTES.items():
            candidate = root / relative
            try:
                if not candidate.is_file() or candidate.stat().st_size < minimum:
                    return False
            except OSError:
                return False
        return True

    # ------------------------------------------------------------------
    # 预热 / 下载
    # ------------------------------------------------------------------

    def start_warmup(self) -> asyncio.Task[Any] | None:
        """插件激活时调用：后台加载或下载模型，不阻塞启动。"""
        if not self._enabled() or self._classifier is not None:
            return None
        task = self._warmup_task
        if task is not None and not task.done():
            return task
        if time.monotonic() - self._last_warmup_at < 5.0:
            return task
        self._last_warmup_at = time.monotonic()
        try:
            task = asyncio.create_task(self._warmup())
        except RuntimeError:
            return None
        self._warmup_task = task
        self._warmup_tasks.add(task)
        task.add_done_callback(self._warmup_tasks.discard)
        return task

    def cancel_warmup(self) -> None:
        """插件卸载/重载时停止后台下载；下载线程会在下一个分块退出。"""
        self._cancel_requested = True
        for task in list(self._warmup_tasks):
            task.cancel()

    async def _warmup(self) -> None:
        try:
            await self._ensure_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log_warning_once(f"ONNX 预热失败，降级为纯窗口: {exc}")
            if isinstance(exc, ImportError):
                self._failed = True

    def _start_warmup_if_due(self) -> None:
        if time.monotonic() - self._last_warmup_at < RETRY_SECONDS:
            return
        self.start_warmup()

    async def _ensure_ready(self) -> Any:
        if self._classifier is not None:
            return self._classifier
        async with self._load_lock:
            if self._classifier is not None:
                return self._classifier
            path = self._model_path()
            valid = self._model_valid(path)
            if not valid:
                custom = self._custom_model_path()
                if custom is not None:
                    raise FileNotFoundError(
                        f"ONNX 模型不完整: {custom}。"
                        "请检查 send_gate_onnx_model_path，或留空让插件自动下载。"
                    )
                if not self._bool("send_gate_onnx_auto_download", True):
                    raise FileNotFoundError(
                        f"ONNX 模型不完整: {path}，且已关闭自动下载。"
                    )
                await asyncio.to_thread(self._download_all, path)
                valid = self._model_valid(path)
            if not valid:
                raise RuntimeError(f"ONNX 模型校验失败: {path}")
            self._classifier = await asyncio.to_thread(
                _load_classifier,
                str(path / "model.onnx"),
                str(path / "tokenizer"),
            )
            self._logger.info("[send_gate] ONNX 节奏模型已加载: %s", path)
            return self._classifier

    def _expected_files(self) -> dict[str, tuple[int, str]]:
        if self._custom_model_path() is not None:
            return {}
        return EXPECTED_FILES.get(self._model_type(), {})

    def _model_valid(self, path: Path | None = None) -> bool:
        root = path or self._model_path()
        if not self._model_complete(root):
            return False
        expected = self._expected_files()
        if not expected:
            return True
        return all(
            self._file_matches(root / relative, size, digest)
            for relative, (size, digest) in expected.items()
        )

    def _download_all(self, target: Path) -> None:
        model_type = self._model_type()
        repo = MODELSCOPE_REPOS[model_type]
        expected = self._expected_files()
        if not expected:
            raise RuntimeError("当前模型类型没有可用的下载校验表")
        timeout = max(
            10.0,
            float(self._cfg("send_gate_onnx_download_timeout_seconds", 120) or 120),
        )
        target.mkdir(parents=True, exist_ok=True)
        self._logger.info(
            "[send_gate] ONNX 模型不完整或校验失败，从 ModelScope 下载 %s -> %s",
            repo,
            target,
        )
        for relative in REQUIRED_FILES:
            destination = target / relative
            size, digest = expected[relative]
            if self._file_matches(destination, size, digest):
                continue
            if self._cancel_requested:
                raise asyncio.CancelledError
            url = MODELSCOPE_FILE_URL.format(repo=repo, path=MODELSCOPE_PATHS[relative])
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, part_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=str(destination.parent),
            )
            os.close(fd)
            part = Path(part_name)
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": "application/octet-stream",
                    },
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    header = response.headers.get("Content-Length")
                    if header and str(header).isdigit() and int(header) != size:
                        raise RuntimeError(
                            f"下载大小与上游清单不一致: {relative} ({header} != {size})"
                        )
                    written = 0
                    with part.open("wb") as handle:
                        while True:
                            if self._cancel_requested:
                                raise asyncio.CancelledError
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > size:
                                raise RuntimeError(f"下载文件超过预期大小: {relative}")
                            handle.write(chunk)
                if written != size:
                    raise RuntimeError(
                        f"下载文件不完整: {relative} ({written} != {size})"
                    )
                if not self._file_matches(part, size, digest):
                    raise RuntimeError(f"下载文件 SHA256 校验失败: {relative}")
                try:
                    os.replace(part, destination)
                except OSError:
                    # 另一个实例可能已经完成同一文件；目标文件有效即可。
                    if not self._file_matches(destination, size, digest):
                        raise
            except asyncio.CancelledError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
                raise RuntimeError(f"从 ModelScope 下载失败: {url} ({exc})") from exc
            finally:
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
        if not self._model_valid(target):
            raise RuntimeError(f"ONNX 模型下载后仍不完整: {target}")
        self._logger.info(
            "[send_gate] ONNX 模型下载完成: %s (model=%s)",
            target,
            model_type,
        )

    def _file_matches(self, path: Path, size: int, digest: str) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
            actual = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    actual.update(chunk)
            return actual.hexdigest() == digest
        except OSError:
            return False

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default if value is None else bool(value)

    def _log_warning_once(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        self._logger.warning("[send_gate] %s", message)

    # ------------------------------------------------------------------
    # 对外评分
    # ------------------------------------------------------------------

    async def score(self, text: str) -> float | None:
        if not self._enabled() or self._failed:
            return None
        if not str(text or "").strip():
            return None
        try:
            if self._classifier is None:
                if not self._model_complete():
                    self._start_warmup_if_due()
                    if not self._warned_download:
                        self._warned_download = True
                        self._logger.info(
                            "[send_gate] ONNX 模型尚未就绪，先使用纯时间窗口；"
                            "后台下载/加载完成后自动启用"
                        )
                    return None
                if not self._model_valid():
                    self._start_warmup_if_due()
                    self._log_warning_once(
                        "ONNX 模型校验失败，后台重新下载；当前降级为纯窗口"
                    )
                    return None
                await self._ensure_ready()
            value, _ = await asyncio.to_thread(
                self._classifier.predict, str(text)[:2000]
            )
            return max(0.0, min(1.0, float(value)))
        except asyncio.CancelledError:
            raise
        except ImportError as exc:
            self._failed = True
            self._log_warning_once(f"ONNX 运行依赖缺失，降级为纯窗口: {exc}")
            return None
        except FileNotFoundError as exc:
            self._start_warmup_if_due()
            self._log_warning_once(f"ONNX 模型不可用: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            self._log_warning_once(f"ONNX 节奏不可用，降级为纯窗口: {exc}")
            return None


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
