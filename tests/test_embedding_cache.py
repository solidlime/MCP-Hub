"""A1/A2/A3: 埋め込みディスクキャッシュ・同一コーパス short-circuit・embedding_status.

conftest が MCP_HUB_EMBEDDING=0 で fastembed を切るため、各テストは
ToolIndex を組んだ後に _use_embeddings と fake embedder を直接注入する
（test_tool_index.py の _FixedEmbedder / test_fix6.py の _Boom と同じ流儀）。
"""

import hashlib

import numpy as np

from mcp_hub.meta_provider import ToolIndex


def _docs(n: int) -> list[dict]:
    return [
        {
            "server": "srv",
            "name": f"tool_{i}",
            "description": f"tool number {i}",
            "tags": ["dev"],
            "inputSchema": {"type": "object", "properties": {}},
        }
        for i in range(n)
    ]


class _CountingEmbedder:
    """embed() の呼び出し回数を数え、定数ベクトルを返す。"""

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts, batch_size=8):
        self.calls += 1
        self.texts.extend(texts)
        return [np.zeros(self.dim, dtype=np.float32) for _ in texts]


def _index(monkeypatch, tmp_path, embedder=None, use: bool = True) -> ToolIndex:
    """tmpdir をキャッシュ置き場にし、埋め込みを有効化した ToolIndex を返す。"""
    monkeypatch.setenv("MCP_HUB_EMBED_CACHE_DIR", str(tmp_path))
    idx = ToolIndex()
    idx._use_embeddings = use
    idx._embeddings_requested = use
    if embedder is not None:
        idx._embedder = embedder  # type: ignore[assignment]
    return idx


def _matrix(idx: ToolIndex) -> np.ndarray:
    emb = idx._embeddings
    assert emb is not None
    return emb


async def test_rebuild_twice_embeds_once(monkeypatch, tmp_path):
    """A2: 同一コーパスで 2 回 rebuild しても embed は 1 回しか呼ばれない。"""
    emb = _CountingEmbedder()
    idx = _index(monkeypatch, tmp_path, emb)
    docs = _docs(5)

    await idx.rebuild(docs)
    assert emb.calls == 1

    await idx.rebuild(docs)  # 同一コーパス → short-circuit
    assert emb.calls == 1
    assert _matrix(idx).shape == (5, 4)


async def test_cache_hit_across_instances_skips_embed(monkeypatch, tmp_path):
    """A1: 別インスタンスでもディスクキャッシュが効けば embed は呼ばれない。"""
    emb1 = _CountingEmbedder()
    idx1 = _index(monkeypatch, tmp_path, emb1)
    docs = _docs(4)
    await idx1.rebuild(docs)
    assert emb1.calls == 1

    emb2 = _CountingEmbedder()
    idx2 = _index(monkeypatch, tmp_path, emb2)
    await idx2.rebuild(docs)
    assert emb2.calls == 0
    assert _matrix(idx2).shape == (4, 4)
    np.testing.assert_allclose(_matrix(idx2), _matrix(idx1))


async def test_broken_cache_file_recomputes_without_raising(monkeypatch, tmp_path):
    """壊れた npz でも例外を出さず、キャッシュ無しで再計算する。"""
    idx = _index(monkeypatch, tmp_path, _CountingEmbedder())
    await idx.rebuild(_docs(2))

    # 正しい npz を破損バイト列で置き換える
    with open(idx._cache_path(), "wb") as fh:
        fh.write(b"not a real npz \x00\x01\x02")

    emb2 = _CountingEmbedder()
    idx._embedder = emb2  # type: ignore[assignment]
    await idx.rebuild(_docs(3))  # 例外を上げてはならない

    assert emb2.calls >= 1
    assert _matrix(idx).shape == (3, 4)


async def test_embedding_status_reports_error_and_inactive(monkeypatch, tmp_path):
    """A3: 失敗時は error:*、実効無効時は inactive:* を返す。"""

    class _Boom:
        def embed(self, texts, batch_size=8):
            raise RuntimeError("onnx down")

    idx = _index(monkeypatch, tmp_path, _Boom())
    await idx.rebuild(_docs(2))  # embed 失敗 → 恒久降格

    assert idx.embedding_status.startswith("error:")
    assert "onnx down" in idx.embedding_status
    assert idx.use_embeddings is False

    fresh = _index(monkeypatch, tmp_path, None, use=False)
    assert fresh.embedding_status.startswith("inactive:")


async def test_partial_change_does_not_clobber_cache(monkeypatch, tmp_path):
    """回帰: 1 文書変更の rebuild 後もキャッシュが全件分残っていること。

    旧実装は _save_cache(fresh) で新規分だけを npz に書き、それまでのエントリを
    消していた（部分 rebuild のたびにキャッシュ自壊）。
    """
    emb = _CountingEmbedder()
    idx = _index(monkeypatch, tmp_path, emb)
    docs = _docs(4)
    await idx.rebuild(docs)
    assert emb.calls == 1

    docs[1]["description"] = "changed description"  # 1 文書だけ変更
    await idx.rebuild(docs)
    assert emb.calls == 2

    # 新インスタンス・同コーパス: 全件キャッシュ済みなので embed は 0 回
    emb2 = _CountingEmbedder()
    idx2 = _index(monkeypatch, tmp_path, emb2)
    await idx2.rebuild(docs)
    assert emb2.calls == 0
    assert _matrix(idx2).shape == (4, 4)

    # npz のエントリ数が現コーパス全件（4）分あること
    with np.load(idx2._cache_path()) as data:
        assert sorted(data.files) == sorted(
            [
                hashlib.sha1(  # noqa: S324
                    f"srv/{d['name']} [{', '.join(d.get('tags', []))}]: {d['description']}".encode()
                ).hexdigest()[:16]
                for d in docs
            ]
        )


async def test_embeddings_cleared_before_reembedding(monkeypatch, tmp_path):
    """行ずれ防止: embed が走る時点で旧コーパスの行列が残っていないこと。

    rebuild は _documents を先に差し替えるため、embed 窓で _embeddings が残ると
    search() の semantic 経路が新 doc リストと行ずれして誤ヒットする。
    タイミング依存にせず、_embed_docs_blocking 呼び出し時に None を確認する。
    """
    idx = _index(monkeypatch, tmp_path, _CountingEmbedder())
    await idx.rebuild(_docs(3))
    assert _matrix(idx).shape == (3, 4)

    observed: dict = {}

    def checked(doc_texts):
        observed["embeddings_at_embed"] = idx._embeddings
        return np.zeros((len(doc_texts), 4), dtype=np.float32)

    idx._embed_docs_blocking = checked  # type: ignore[assignment,method-assign]
    docs = _docs(3)
    docs.append(
        {
            "server": "srv",
            "name": "tool_new",
            "description": "brand new tool",
            "tags": [],
            "inputSchema": {},
        }
    )
    await idx.rebuild(docs)

    assert observed["embeddings_at_embed"] is None
    assert _matrix(idx).shape == (4, 4)
