"""埋め込みエンジンの選択（fastembed / ONNX Runtime 直叩き）。

fastembed の `PoolingType` は MEAN/CLS しか無く、ModernBERT 系（ruri-v3）は
登録リストにも無いため `TextEmbedding` で読めない。そうしたモデルは
onnxruntime + tokenizers + huggingface_hub で直接読む（OrtEmbedder）。

依存の import は OrtEmbedder / create_embedder の実行時まで遅延する。依存が
無い環境でも meta_provider の import と BM25 経路は壊れない（既存の degrade）。

`embeddings` extra には fastembed / onnxruntime / tokenizers / huggingface_hub を
明示宣言している（fastembed の推移依存に頼ると、既定モデルが ORT 経路に
なった今、fastembed の依存変更で静かに壊れる）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

logger = logging.getLogger(__name__)

# 論理モデル名（カノニカル） → ONNX 変換リポジトリの仕様。
# 変換は第三者製（lc-studio）。実測は #001 の /tmp/emb_eval/eval2.py と同一
# アーティファクト: rev 234084a8d0e3f8aa847bd9b83347062eb7ce4c4a /
# model.onnx 147,008,055 bytes / sha256 ffdb6a8f48e2bc26181838f2a408d52ef88853a7e44915ac09b1c311544e3a08
ORT_MODELS: dict[str, dict[str, Any]] = {
    "cl-nagoya/ruri-v3-30m": {
        "repo": "lc-studio/ruri-v3-30m-onnx",
        # 第三者変換 repo のため rev 固定。force-push/再変換で同名のまま別
        # アーティファクトに差し替わるのを防ぐ。更新は明示的なコミットで行う。
        "revision": "234084a8d0e3f8aa847bd9b83347062eb7ce4c4a",
        "model_file": "model.onnx",
        "pooling": "mean",
        "dim": 256,
    },
}

# 変換リポジトリ名で指定されても同じ論理モデルに寄せる（#001 の実測は repo 名で
# 回していたため、その名前が設定ファイルに残っていても動くように）。
ORT_MODEL_ALIASES: dict[str, str] = {
    "lc-studio/ruri-v3-30m-onnx": "cl-nagoya/ruri-v3-30m",
}

# {lowercase: カノニカル名}。fastembed の登録リストに重ねて使う。
ORT_SUPPORTED_MODELS: dict[str, str] = {
    **{alias.lower(): canonical for alias, canonical in ORT_MODEL_ALIASES.items()},
    **{name.lower(): name for name in ORT_MODELS},
}


def canonical_ort_model(model: str) -> str | None:
    """モデル名（大小文字非依存）が ORT 経路の対象ならカノニカル名を返す。"""
    return ORT_SUPPORTED_MODELS.get(model.lower())


def ort_model_spec(model: str) -> dict[str, Any] | None:
    """ORT 経路の仕様（repo/model_file/pooling/dim）。対象外は None。"""
    canonical = canonical_ort_model(model)
    return ORT_MODELS[canonical] if canonical is not None else None


class OrtEmbedder:
    """任意の HF ONNX エンコーダを ONNX Runtime 直叩きで埋め込む。

    fastembed の PoolingType は MEAN/CLS しか無く、last-token プーリングや
    ModernBERT 系を扱えないため自前実装する。インタフェースは fastembed の
    TextEmbedding と互換: `embed(texts, batch_size=...)` が L2 正規化済み
    np.float32 ベクトルを 1 件ずつ yield する。
    """

    def __init__(
        self,
        repo: str,
        revision: str,
        model_file: str = "model.onnx",
        pooling: str = "mean",
        max_length: int = 512,
        batch_size: int = 8,
    ):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        self.dir = snapshot_download(
            repo,
            revision=revision,
            allow_patterns=[
                model_file,
                "*.json",
                "*.txt",
                "*.model",
                "tokenizer*",
                "onnx/*.json",
                "onnx/*.model",
            ],
        )
        self.tok = Tokenizer.from_file(os.path.join(self.dir, "tokenizer.json"))
        self.tok.enable_padding()
        self.tok.enable_truncation(max_length=max_length)
        self.sess = ort.InferenceSession(
            os.path.join(self.dir, model_file), providers=["CPUExecutionProvider"]
        )
        self.inputs = {i.name for i in self.sess.get_inputs()}
        # onnx-community 系（transformers.js エクスポート）は KV cache 入力を要求する。
        self.past_inputs = [
            i for i in self.sess.get_inputs() if i.name.startswith("past_key_values")
        ]
        self.pooling = pooling
        self.batch_size = batch_size

    def embed(self, texts: list[str], batch_size: int | None = None):
        import numpy as np

        bs = batch_size or self.batch_size
        for i in range(0, len(texts), bs):
            encs = self.tok.encode_batch(texts[i : i + bs])
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed: dict[str, Any] = {}
            if "input_ids" in self.inputs:
                feed["input_ids"] = ids
            if "attention_mask" in self.inputs:
                feed["attention_mask"] = mask
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            if "position_ids" in self.inputs:
                feed["position_ids"] = (
                    np.arange(ids.shape[1])[None, :]
                    .repeat(ids.shape[0], 0)
                    .astype(np.int64)
                )
            for pi in self.past_inputs:
                # 空の past を渡して「キャッシュ無し＝全文を一度に処理」させる。
                feed[pi.name] = np.zeros(
                    (ids.shape[0], pi.shape[1], 0, pi.shape[3]), dtype=np.float32
                )
            out = self.sess.run(None, feed)
            hs = cast(
                np.ndarray,
                next(o for o in out if getattr(o, "ndim", 0) == 3),
            )
            if self.pooling == "mean":
                m = mask[..., None].astype(np.float32)
                emb = (hs * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            elif self.pooling == "cls":
                emb = hs[:, 0]
            elif self.pooling == "last":
                idx = mask.sum(1) - 1
                emb = hs[np.arange(hs.shape[0]), idx]
            else:
                raise ValueError(self.pooling)
            emb = emb.astype(np.float32)
            norm = np.linalg.norm(emb, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            emb = emb / norm
            yield from emb


def create_embedder(model: str) -> Any:
    """モデル名から埋め込みエンジンを選ぶ（Strategy）。

    ORT 経路対象（ruri 等の ModernBERT 系）は OrtEmbedder、それ以外は fastembed
    の TextEmbedding。どちらも `embed(texts, batch_size=...)` で L2 正規化済み
    ベクトルを返す点で互換。
    """
    spec = ort_model_spec(model)
    if spec is not None:
        # revision は spec から明示的に落とす（キー欠落を unpinned DL にしない）。
        return OrtEmbedder(
            repo=spec["repo"],
            revision=spec["revision"],
            model_file=spec["model_file"],
            pooling=spec["pooling"],
        )
    from fastembed import TextEmbedding  # 遅延 import: ORT 経路では不要

    return TextEmbedding(model)
