"""ruri-v3-30m（ONNX Runtime 経路）の登録・解決・埋め込み正規化テスト。

fastembed では読めないモデル（ModernBERT 系）を `embedders.OrtEmbedder` 経由で
扱うための最小契約を固定する。実モデルの DL が要るテストは依存欠如/オフラインで
skip し、CI（embeddings extra 無し）でも落ちないようにする。
"""

import hashlib
import importlib.util
import os
import sys
import types

import numpy as np
import pytest

from mcp_hub.config import DEFAULT_EMBEDDING_MODEL
from mcp_hub.embedders import (
    OrtEmbedder,
    canonical_ort_model,
    create_embedder,
    ort_model_spec,
)
from mcp_hub.meta_provider import (
    TEXT_FMT_VERSION,
    ToolIndex,
    _embedding_dim,
    _supported_embedding_models,
    model_profile,
    resolve_embedding_model,
)

RURI = "cl-nagoya/ruri-v3-30m"
RURI_REVISION = "234084a8d0e3f8aa847bd9b83347062eb7ce4c4a"


class TestOrtRevisionPin:
    """第三者変換 repo の rev 固定（force-push/再変換で黙って差し替わらない）。"""

    def test_spec_pins_revision(self):
        spec = ort_model_spec(RURI)
        assert spec is not None
        assert spec["revision"] == RURI_REVISION

    def test_alias_shares_same_pinned_spec(self):
        assert ort_model_spec("lc-studio/ruri-v3-30m-onnx") == ort_model_spec(RURI)

    def test_snapshot_download_called_with_revision(self, monkeypatch, tmp_path):
        """create_embedder → OrtEmbedder → snapshot_download まで rev を通す（DL しない）。"""
        calls: dict = {}

        def _fake_snapshot(repo, revision=None, **kwargs):
            calls["repo"] = repo
            calls["revision"] = revision
            return str(tmp_path)

        class _FakeTok:
            def enable_padding(self):
                pass

            def enable_truncation(self, max_length=512):
                pass

        class _FakeInput:
            def __init__(self, name):
                self.name = name
                self.shape = [None, None]

        class _FakeSess:
            def get_inputs(self):
                return [_FakeInput("input_ids"), _FakeInput("attention_mask")]

        monkeypatch.setitem(
            sys.modules,
            "huggingface_hub",
            types.SimpleNamespace(snapshot_download=_fake_snapshot),
        )
        monkeypatch.setitem(
            sys.modules,
            "onnxruntime",
            types.SimpleNamespace(InferenceSession=lambda *a, **k: _FakeSess()),
        )
        monkeypatch.setitem(
            sys.modules,
            "tokenizers",
            types.SimpleNamespace(
                Tokenizer=types.SimpleNamespace(from_file=lambda _p: _FakeTok())
            ),
        )

        create_embedder(RURI)
        assert calls == {
            "repo": "lc-studio/ruri-v3-30m-onnx",
            "revision": RURI_REVISION,
        }


class TestRuriProfile:
    def test_profile_has_japanese_prefixes_and_default_floor(self):
        p = model_profile(RURI)
        assert p["query_prefix"] == "検索クエリ: "
        assert p["passage_prefix"] == "検索文書: "
        assert p["semantic_floor"] == 0.30

    def test_profile_is_case_insensitive(self):
        assert model_profile("CL-NAGOYA/RURI-V3-30M") == model_profile(RURI)

    def test_unknown_model_still_uses_default_profile(self):
        """ruri 以外の未知モデルは従来どおり prefix 無し/floor 0.30。"""
        p = model_profile("acme/whatever")
        assert p["query_prefix"] is None
        assert p["passage_prefix"] is None
        assert p["semantic_floor"] == 0.30


class TestResolveEmbeddingModel:
    def test_case_insensitive_resolution(self):
        assert resolve_embedding_model("CL-NAGOYA/RURI-V3-30M", None) == RURI
        supported = _supported_embedding_models()
        assert supported is not None
        assert resolve_embedding_model("Cl-Nagoya/Ruri-V3-30M", supported) == RURI

    def test_onnx_conversion_repo_name_is_aliased(self):
        """変換リポジトリ名（#001 の実測で使用）でも同じ論理モデルに寄る。"""
        assert resolve_embedding_model("lc-studio/ruri-v3-30m-onnx", None) == RURI
        assert ort_model_spec("lc-studio/ruri-v3-30m-onnx") == ort_model_spec(RURI)

    def test_default_model_is_in_supported_table(self):
        """既定モデルは既知モデル表に載っている（fastembed 有無に関わらず）。"""
        supported = _supported_embedding_models()
        assert supported is not None
        assert DEFAULT_EMBEDDING_MODEL in supported.values()

    def test_unknown_model_falls_back_without_raising(self):
        assert canonical_ort_model("acme/not-a-model") is None
        assert ort_model_spec("acme/not-a-model") is None
        assert (
            resolve_embedding_model("acme/not-a-model", _supported_embedding_models())
            == DEFAULT_EMBEDDING_MODEL
        )


class TestEmbedderStrategy:
    def test_ort_route_selected_for_ruri(self, monkeypatch):
        created: dict = {}

        class _Stub:
            def __init__(self, **kwargs):
                created.update(kwargs)

        monkeypatch.setattr("mcp_hub.embedders.OrtEmbedder", _Stub)
        create_embedder(RURI)
        assert created == {
            "repo": "lc-studio/ruri-v3-30m-onnx",
            "revision": RURI_REVISION,
            "model_file": "model.onnx",
            "pooling": "mean",
        }


class _Enc:
    def __init__(self, ids, mask):
        self.ids = ids
        self.attention_mask = mask


class _FakeTok:
    def __init__(self, encs):
        self._encs = encs

    def encode_batch(self, texts):
        return self._encs[: len(texts)]


class _FakeInput:
    def __init__(self, name):
        self.name = name
        self.shape = [None, None]


class _FakeSess:
    def __init__(self, hidden):
        self._hidden = hidden

    def get_inputs(self):
        return [_FakeInput("input_ids"), _FakeInput("attention_mask")]

    def run(self, _outputs, _feed):
        return [self._hidden]


def _fake_embedder(hidden, encs, pooling="mean") -> OrtEmbedder:
    """ONNX セッション/tokenizer を差し替えた OrtEmbedder（モデル DL 無し）。"""
    emb = object.__new__(OrtEmbedder)
    emb.tok = _FakeTok(encs)  # type: ignore[assignment]
    emb.sess = _FakeSess(hidden)  # type: ignore[assignment]
    emb.inputs = {"input_ids", "attention_mask"}  # type: ignore[assignment]
    emb.past_inputs = []  # type: ignore[assignment]
    emb.pooling = pooling  # type: ignore[assignment]
    emb.batch_size = 8  # type: ignore[assignment]
    return emb


class TestOrtEmbedderPooling:
    def test_embed_is_l2_normalized_and_ignores_padding(self):
        hidden = np.array(
            [
                [[2.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]],  # mask [1,1] → [1,0,0,0]
                [
                    [4.0, 0.0, 0.0, 0.0],
                    [9.0, 9.0, 9.0, 9.0],
                ],  # mask [1,0] → padding を無視
                [[3.0, 4.0, 0.0, 0.0], [3.0, 4.0, 0.0, 0.0]],  # norm 5 → [0.6,0.8,0,0]
            ],
            dtype=np.float32,
        )
        encs = [_Enc([1, 2], [1, 1]), _Enc([1, 2], [1, 0]), _Enc([1, 2], [1, 1])]
        out = np.array(
            list(_fake_embedder(hidden, encs).embed(["a", "b", "c"], batch_size=3))
        )

        assert out.shape == (3, 4)
        assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-6)
        assert np.allclose(out[0], [1.0, 0.0, 0.0, 0.0], atol=1e-6)
        assert np.allclose(out[1], [1.0, 0.0, 0.0, 0.0], atol=1e-6)
        assert np.allclose(out[2], [0.6, 0.8, 0.0, 0.0], atol=1e-6)

    def test_embed_returns_one_vector_per_text(self):
        hidden = np.ones((2, 2, 3), dtype=np.float32)
        encs = [_Enc([1, 2], [1, 1]), _Enc([1, 2], [1, 1])]
        out = list(_fake_embedder(hidden, encs).embed(["a", "b"], batch_size=8))
        assert len(out) == 2
        assert all(v.shape == (3,) for v in out)

    def test_unknown_pooling_raises(self):
        hidden = np.ones((1, 2, 2), dtype=np.float32)
        with pytest.raises(ValueError):
            list(
                _fake_embedder(
                    hidden, [_Enc([1, 2], [1, 1])], pooling="mean_pool"
                ).embed(["a"])
            )


class TestCacheKey:
    def test_dim_is_declared_for_ruri(self):
        """dim はモデル登録から取り、384 等をハードコードしない。"""
        assert _embedding_dim(RURI) == 256

    def test_cache_path_keyed_by_model_dim_prefix_and_text_version(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MCP_HUB_EMBED_CACHE_DIR", str(tmp_path))
        idx = ToolIndex(embedding_model=RURI)
        assert idx._embedding_model == RURI
        key = f"{RURI}|256|検索文書: |{TEXT_FMT_VERSION}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        assert idx._cache_path() == os.path.join(str(tmp_path), digest + ".npz")
        assert list(tmp_path.iterdir()) == []  # パス計算はファイルを作らない


def _has_ort_deps() -> bool:
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("onnxruntime", "tokenizers", "huggingface_hub")
    )


@pytest.mark.skipif(
    not _has_ort_deps(), reason="onnxruntime/tokenizers/huggingface_hub 未導入"
)
def test_real_onnx_model_smoke():
    """実モデル（第三者変換）で shape/norm/非退化を確認する。DL 不可なら skip。"""
    spec = ort_model_spec(DEFAULT_EMBEDDING_MODEL)
    assert spec is not None
    try:
        emb = OrtEmbedder(
            repo=spec["repo"],
            revision=spec["revision"],
            model_file=spec["model_file"],
            pooling=spec["pooling"],
        )
        vecs = np.array(
            list(emb.embed(["検索文書: 天気", "検索文書: 照明を点ける"], batch_size=2))
        )
    except Exception as exc:  # ネットワーク無し・未キャッシュは skip（CI 想定）
        pytest.skip(f"ONNX model unavailable: {type(exc).__name__}: {exc}")
    assert vecs.shape == (2, 256)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-3)
    assert not np.allclose(vecs[0], vecs[1])
