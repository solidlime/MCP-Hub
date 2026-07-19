# WebUI パフォーマンス修正 & モーダルリファクタリング 実装計画

> **For agentic workers:** 各 Task は独立して実装可能（依存関係は明示）。Steps は checkbox (`- [ ]`) でトラッキング。実装前に `@oracle` にアーキテクチャレビューを依頼すること。

**Goal:** WebUI でサーバー追加/削除後のレスポンス激遅を解消し、設定モーダルを堅牢にする。フロントエンドのポーリングスタック・全DOM再構築・バックエンドのロック競合の3点を修正。

**Architecture:** フロントエンドは既存の Vanilla JS SPA を維持し、ポーリングを `setInterval` → ガード付き `setTimeout` に変更、レンダリングを全置換から差分更新に切り替え、モーダルの Add/Edit 共用ロジックを整理。バックエンドは `asyncio.Lock` の範囲を最小化し、`GET /admin/api/servers` のデフォルト `include_tools=false` に変更。

**Tech Stack:** Vanilla JS（フロントエンド）, Python/FastAPI（バックエンド）, asyncio

---

## Chunk 1: フロントエンド — ポーリング修正（根本原因）

### Task 1.1: `setInterval` → ガード付き `setTimeout` に変更

**Files:**
- Modify: `src/mcp_hub/static/index.html:2775-2786` (periodicRefresh)
- Modify: `src/mcp_hub/static/index.html:2936-2943` (DOMContentLoaded init)

**背景:** `setInterval(periodicRefresh, 30000)` は前回の fetch 完了を待たずに次を発火。サーバー追加/削除中に `GET /admin/api/servers`（`include_tools=true`）が重なるとリクエストがスタックし、UIが激重になる。

- [ ] **Step 1: `periodicRefresh` をガード付きに変更**

```javascript
// ガード変数を追加 (state セクション、1698行付近)
let periodicRefreshActive = false;

// periodicRefresh をガード付きに修正 (2775-2786行)
async function periodicRefresh() {
    if (periodicRefreshActive) return; // 前回が未完了ならスキップ
    periodicRefreshActive = true;
    try {
        const data = await API.getServers();
        servers = data.servers || [];
        renderServers();
        updateMetrics();
        await updateHealthStatus();
    } catch (_) {
        // Silently ignore during periodic refresh
    } finally {
        periodicRefreshActive = false;
    }
}
```

- [ ] **Step 2: 初期化部分を `setInterval` → `setTimeout` ループに変更**

```javascript
// DOMContentLoaded 内 (2942行)
// Before: setInterval(periodicRefresh, 30000);
// After:
function scheduleNextRefresh() {
    setTimeout(async () => {
        await periodicRefresh();
        scheduleNextRefresh();
    }, 30000);
}
scheduleNextRefresh();
```

- [ ] **Step 3: `periodicRefreshActive` ガード変数を state セクションに追記**

1698行付近の state 変数群:
```javascript
let periodicRefreshActive = false;
```

- [ ] **Step 4: ブラウザ確認テスト**

スキル `browser-testing-with-devtools` を使い、以下を確認:
- コンソールにエラーがないこと
- 30秒待ってポーリングが正常に動作すること
- サーバー追加・削除操作中に UI がブロックされないこと

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/static/index.html
git commit -m "fix(webui): prevent polling stack-up with guarded setTimeout"
```

---

## Chunk 2: フロントエンド — レンダリング最適化

### Task 2.1: サーバーカードの差分更新関数を追加

**Files:**
- Modify: `src/mcp_hub/static/index.html:1949-2079` (renderServers)
- Add: 差分更新用のヘルパー関数群 (renderServers の後ろ)

**背景:** `renderServers()` が全カードを `innerHTML` で毎回再生成。サーバー数増加に比例して遅延が拡大する。ポーリング更新（ステータス・ツール数変更のみ）では全再構築は不要。

方針:
- サーバー追加/削除時: 引き続き `renderServers()`（構造変更）
- ポーリング更新時: カード単位の差分更新のみ（ステータスバッジ・ツール数）
- 編集・トグル時: 該当カードのみ再生成

- [ ] **Step 1: 単一サーバーカードの HTML 生成関数を抽出**

`renderServers()` からカード HTML 生成部分を独立関数に:

```javascript
function renderServerCard(server) {
    const status = getServerStatus(server);
    const disabled = getServerDisabled(server);
    const tags = getServerTags(server);
    const statusLabel = status === 'connected' ? '接続済' : status === 'connecting' ? '接続中' : status === 'error' ? 'エラー' : '不明';
    const cardClasses = [
        'server-card', 'fade-in',
        disabled ? 'disabled-card' : '',
        status === 'error' ? 'error-card' : ''
    ].filter(Boolean).join(' ');

    return `
    <div class="${cardClasses}" data-server="${server.name}" role="listitem">
        <div class="server-status-badge ${status}" aria-label="ステータス: ${statusLabel}">
            <span class="status-dot" aria-hidden="true"></span>
            <span>${statusLabel}</span>
        </div>
        <div class="server-header" onclick="toggleServer('${server.name}')"
             role="button" tabindex="0" aria-expanded="false"
             aria-label="サーバー ${escapeHtml(server.name)} の詳細を表示">
            <div>
                <div class="server-name">${escapeHtml(server.name)}</div>
                <div class="server-type">
                    ${server.config.url ? '🌐 HTTP' : '⌨️ stdio'}
                </div>
            </div>
        </div>
        <div class="toggle-wrapper">
            <label class="toggle-switch" title="${disabled ? '有効にする' : '無効にする'}">
                <input type="checkbox" ${disabled ? '' : 'checked'}
                       onchange="toggleServerEnabled('${server.name}', this.checked)"
                       aria-label="サーバー ${escapeHtml(server.name)} の有効/無効切替">
                <span class="toggle-track"></span>
                <span class="toggle-thumb"></span>
            </label>
            <span class="toggle-label">${disabled ? '無効' : '有効'}</span>
        </div>
        ${tags.length > 0 ? `
        <div class="server-tags">
            ${tags.map(tag => `
                <span class="tag-chip" data-tag="${escapeHtml(tag)}">
                    ${escapeHtml(tag)}
                    <span class="tag-remove" onclick="removeTag('${server.name}', '${escapeHtml(tag)}')" title="タグを削除" aria-label="タグ ${escapeHtml(tag)} を削除">✕</span>
                </span>
            `).join('')}
            <button class="tag-add-btn" onclick="startAddTag('${server.name}', this)" aria-label="タグを追加" title="タグを追加">＋</button>
        </div>
        ` : `
        <div class="server-tags">
            <button class="tag-add-btn" onclick="startAddTag('${server.name}', this)" aria-label="タグを追加" title="タグを追加">＋</button>
        </div>
        `}
        <div class="server-meta">
            <div class="server-meta-item">
                <span class="meta-icon" aria-hidden="true">🔧</span>
                <span>${server.tools_count || 0} ツール</span>
            </div>
        </div>
        <div class="tools-container" role="region" aria-label="ツール一覧">
            <div class="tools-list">
                ${server.tools && server.tools.length > 0 ? server.tools.map(tool => `
                    <div class="tool-item" role="listitem">
                        <div class="tool-info">
                            <div class="tool-name">${escapeHtml(tool.name)}</div>
                            <div class="tool-description">${escapeHtml(tool.description || '説明なし')}</div>
                        </div>
                        <div class="tool-action">
                            <button class="btn btn-sm btn-secondary"
                                    onclick="openToolModal('${server.name}', '${escapeHtml(tool.name)}')"
                                    aria-label="ツール ${escapeHtml(tool.name)} を実行">
                                実行
                            </button>
                        </div>
                    </div>
                `).join('') : (server.tools === undefined && status === 'connecting') ? '<div style="color: var(--text-muted); padding: 12px; font-size: 0.82rem; display: flex; align-items: center; gap: 6px;"><span class="loading-spinner" style="width:12px;height:12px;"></span> ツールを検出中...</div>' : '<div style="color: var(--text-muted); padding: 12px; font-size: 0.82rem;">ツールがありません</div>'}
            </div>
            <div class="connection-section" id="conn-${server.name}" style="display: none;">
                <div class="conn-label">接続情報</div>
                <div class="connection-url-row">
                    <input type="text" class="connection-url-input" id="conn-url-${server.name}" readonly aria-label="接続URL">
                </div>
                <div id="conn-header-${server.name}" class="connection-header-code" style="display: none;"></div>
            </div>
        </div>
        <div class="server-actions">
            <button class="btn btn-sm btn-secondary" onclick="testServerConnection('${server.name}')"
                    aria-label="サーバー ${escapeHtml(server.name)} の接続テスト">
                <span aria-hidden="true">🔌</span>
                <span>テスト</span>
            </button>
            <button class="btn btn-sm btn-ghost" onclick="showConnectionInfo('${server.name}')"
                    aria-label="接続URLをクリップボードにコピー">
                <span aria-hidden="true">🔗</span>
                <span>接続URL</span>
            </button>
            <button class="btn btn-sm btn-ghost" onclick="openEditModal('${server.name}')"
                    aria-label="サーバー設定を編集">
                <span aria-hidden="true">✏️</span>
                <span>編集</span>
            </button>
            <button class="btn btn-sm btn-danger" onclick="confirmDeleteServer('${server.name}')"
                    aria-label="サーバー ${escapeHtml(server.name)} を削除">
                <span aria-hidden="true">🗑️</span>
                <span>削除</span>
            </button>
        </div>
    </div>`;
}
```

- [ ] **Step 2: `renderServers()` から `renderServerCard()` を呼ぶように変更**

```javascript
function renderServers() {
    const container = document.getElementById('serversList');
    const emptyState = document.getElementById('emptyState');

    if (servers.length === 0) {
        container.innerHTML = '';
        emptyState.style.display = 'block';
        document.getElementById('tagFilterBar').style.display = 'none';
        return;
    }

    emptyState.style.display = 'none';
    container.innerHTML = servers.map(server => renderServerCard(server)).join('');

    renderTagFilterBar();
    applyTagFilter();
}
```

- [ ] **Step 3: 差分更新関数 `updateServerCard()` を追加**

単一サーバーのカードのみを再生成（ステータス・ツール数・disabled 変更時）:

```javascript
function updateServerCard(name) {
    const server = servers.find(s => s.name === name);
    if (!server) return;

    const container = document.getElementById('serversList');
    const existingCard = container.querySelector(`[data-server="${name}"]`);
    if (!existingCard) return;

    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = renderServerCard(server);
    const newCard = tempDiv.firstElementChild;

    existingCard.replaceWith(newCard);
    applyTagFilter();
}
```

- [ ] **Step 4: ポーリング時の全再描画を差分更新に変更**

`periodicRefresh()` 内で、変更があったカードのみ更新:

```javascript
async function periodicRefresh() {
    if (periodicRefreshActive) return;
    periodicRefreshActive = true;
    try {
        const data = await API.getServers();
        const newServers = data.servers || [];

        // 新規/削除があった場合は全再描画
        const oldNames = new Set(servers.map(s => s.name));
        const newNames = new Set(newServers.map(s => s.name));
        const hasStructuralChange =
            oldNames.size !== newNames.size ||
            [...oldNames].some(n => !newNames.has(n)) ||
            [...newNames].some(n => !oldNames.has(n));

        if (hasStructuralChange || servers.length === 0) {
            servers = newServers;
            renderServers();
        } else {
            // 差分更新: ステータス・ツール数が変わったカードのみ
            for (const newServer of newServers) {
                const oldServer = servers.find(s => s.name === newServer.name);
                if (oldServer &&
                    (oldServer.status !== newServer.status ||
                     oldServer.tools_count !== newServer.tools_count)) {
                    // In-place update
                    Object.assign(oldServer, newServer);
                    updateServerCard(newServer.name);
                }
            }
        }
        updateMetrics();
        await updateHealthStatus();
    } catch (_) {
        // Silently ignore during periodic refresh
    } finally {
        periodicRefreshActive = false;
    }
}
```

- [ ] **Step 5: 各操作ハンドラで `renderServers()` の代わりに `updateServerCard()` を使う**

以下の関数で `renderServers()` → `updateServerCard(name)` に変更（単一サーバーのみ変更する場合）:

- `toggleServerEnabled()` (2155行): `renderServers()` → `updateServerCard(name)` + `renderTagFilterBar()`
- `batchAddTags()` (2244行): `renderServers()` → `updateServerCard(name)` + `renderTagFilterBar()`
- `addTag()` (2265行): 同上
- `removeTag()` (2281行): 同上
- `testServerConnection()` (2130行): 同上
- `submitEdit()` の編集成功時 (2604行): 同上

削除・追加時は引き続き `renderServers()` を使用。

- [ ] **Step 6: ブラウザ確認テスト**

スキル `browser-testing-with-devtools` で以下を確認:
- 通常操作でコンソールエラーなし
- サーバーカードが正しくレンダリング
- ポーリング更新でカードがちらつかないこと
- 追加・削除後も一貫した表示

- [ ] **Step 7: コミット**

```bash
git add src/mcp_hub/static/index.html
git commit -m "perf(webui): differential card updates instead of full re-render"
```

---

## Chunk 3: フロントエンド — モーダルリファクタリング

### Task 3.1: Add/Edit モーダルの堅牢化

**Files:**
- Modify: `src/mcp_hub/static/index.html:2334-2682` (edit modal 全体)
- Modify: `src/mcp_hub/static/index.html:2455-2473` (editTagInput handler)

**背景:** Add/Edit 共用モーダルは動作しているが、以下の問題がある:
1. モード切替時に反対側のフィールドを無条件クリア → ユーザーが誤タップでデータ喪失
2. `submitEdit()` が巨大な if/else ブロックで Add/Edit/command/url の4経路を手動分岐
3. 環境変数・ヘッダーのパースが失敗してもエラー表示なし
4. タグ入力のイベントハンドラが DOMContentLoaded で登録されているが、モーダル内の要素が動的に生成されるためタイミング依存

- [ ] **Step 1: モード切替時のデータ喪失防止**

`setEditMode()` (2337行) の修正。フィールドを無条件クリアせず、確認ダイアログを出す:

```javascript
function setEditMode(mode) {
    if (editMode === mode) return;
    
    // 現在のモードに入力がある場合、確認を取る
    const needsConfirm = (editMode === 'command' && document.getElementById('editCommand').value.trim()) ||
                         (editMode === 'url' && document.getElementById('editUrl').value.trim());
    
    if (needsConfirm && !confirm(`入力中の${editMode === 'command' ? 'コマンド' : 'URL'}設定を破棄してモードを切り替えますか？`)) {
        // UIだけ元に戻す
        document.getElementById(editMode === 'command' ? 'modeCommandBtn' : 'modeUrlBtn').classList.add('active');
        return;
    }
    
    editMode = mode;
    const cmdBtn = document.getElementById('modeCommandBtn');
    const urlBtn = document.getElementById('modeUrlBtn');
    const cmdSection = document.getElementById('commandModeSection');
    const urlSection = document.getElementById('urlModeSection');

    if (mode === 'command') {
        cmdBtn.classList.add('active');
        cmdBtn.setAttribute('aria-selected', 'true');
        urlBtn.classList.remove('active');
        urlBtn.setAttribute('aria-selected', 'false');
        cmdSection.style.display = '';
        urlSection.style.display = 'none';
    } else {
        urlBtn.classList.add('active');
        urlBtn.setAttribute('aria-selected', 'true');
        cmdBtn.classList.remove('active');
        cmdBtn.setAttribute('aria-selected', 'false');
        urlSection.style.display = '';
        cmdSection.style.display = 'none';
    }
}
```

- [ ] **Step 2: `submitEdit()` の分岐を整理**

Add パスと Edit パスで payload 構築ロジックを分離し、可読性を向上:

```javascript
function buildServerPayload(isEdit) {
    const command = document.getElementById('editCommand').value.trim();
    const argsStr = document.getElementById('editArgs').value.trim();
    const url = document.getElementById('editUrl').value.trim();
    const disabled = document.getElementById('editDisabled').checked;
    const newArgs = argsStr ? argsStr.split('\n').map(s => s.trim()).filter(Boolean) : [];
    const newEnv = parseKeyValue(document.getElementById('editEnv').value.trim(), '=');
    const newHeaders = parseKeyValue(document.getElementById('editHeaders').value.trim(), ':');

    const payload = {};

    if (editMode === 'command') {
        if (command) payload.command = command;
        payload.args = newArgs;
        if (Object.keys(newEnv).length > 0) payload.env = newEnv;
    } else {
        if (url) payload.url = url;
        if (Object.keys(newHeaders).length > 0) payload.headers = newHeaders;
    }

    if (isEdit) {
        // 編集時: 既存値と差分のみ送信
        const server = servers.find(s => s.name === editingServer);
        if (!server) return payload;
        return diffPayload(payload, server.config, editMode);
    }

    // 追加時: タグとdisabledも含める
    if (editTags.length > 0) payload.tags = editTags;
    if (disabled) payload.disabled = disabled;

    return payload;
}

// キー=値 または キー: 値 形式のパース
function parseKeyValue(text, separator) {
    const result = {};
    if (!text) return result;
    text.split('\n').forEach(line => {
        const idx = line.indexOf(separator);
        if (idx > 0) {
            const key = line.substring(0, idx).trim();
            let val = line.substring(idx + 1).trim();
            if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
                val = val.slice(1, -1);
            }
            result[key] = val;
        }
    });
    return result;
}
```

- [ ] **Step 3: `submitEdit()` 本体をシンプルに**

`buildServerPayload()` を使った簡略化:

```javascript
async function submitEdit() {
    const submitBtn = document.getElementById('editSubmitBtn');
    const errorEl = document.getElementById('editFormError');
    errorEl.style.display = 'none';

    const payload = buildServerPayload(!!editingServer);

    // バリデーション
    if (!editingServer) {
        const name = document.getElementById('editName').value.trim();
        if (!name) { showFormError('サーバー名を入力してください'); return; }
        if (editMode === 'command' && !payload.command) { showFormError('コマンドを入力してください'); return; }
        if (editMode === 'url' && !payload.url) { showFormError('URLを入力してください'); return; }

        // Add path
        try {
            setSubmitLoading(submitBtn, '追加中...');
            await API.addServer({ name, config: payload });
            await loadServers();
            closeEditModal();
            showToast(`「${name}」を追加しました`, 'success');
        } catch (error) {
            handleSubmitError(error, errorEl);
        } finally {
            resetSubmitBtn(submitBtn, '追加');
        }
    } else {
        // Edit path
        if (Object.keys(payload).length === 0) {
            showToast('変更がありません', 'info');
            closeEditModal();
            return;
        }
        try {
            setSubmitLoading(submitBtn, '保存中...');
            await API.patchServer(editingServer, payload);
            const server = servers.find(s => s.name === editingServer);
            if (server) Object.assign(server.config, payload);
            updateServerCard(editingServer);
            renderTagFilterBar();
            closeEditModal();
            showToast(`「${editingServer}」の設定を更新しました`, 'success');
        } catch (error) {
            handleSubmitError(error, errorEl);
        } finally {
            resetSubmitBtn(submitBtn, '保存');
        }
    }
}
```

- [ ] **Step 4: ヘルパー関数を追加**

```javascript
function showFormError(msg) {
    const el = document.getElementById('editFormError');
    el.textContent = msg;
    el.style.display = 'block';
}

function setSubmitLoading(btn, text) {
    btn.disabled = true;
    btn.innerHTML = `<span class="loading-spinner"></span> ${text}`;
}

function resetSubmitBtn(btn, text) {
    btn.disabled = false;
    btn.textContent = text;
}

function handleSubmitError(error, errorEl) {
    if (error.name === 'AbortError') {
        closeEditModal();
        showToast('応答がタイムアウトしました。バックグラウンドで処理を継続します...', 'warning');
        loadServers(); // fire-and-forget
    } else {
        errorEl.textContent = error.message;
        errorEl.style.display = 'block';
    }
}

function diffPayload(newConfig, oldConfig, mode) {
    // 既存configと差分があるフィールドのみを含むpayloadを返す
    const payload = {};
    for (const [key, val] of Object.entries(newConfig)) {
        const oldVal = oldConfig[key];
        if (JSON.stringify(val) !== JSON.stringify(oldVal)) {
            payload[key] = val === '' || (Array.isArray(val) && val.length === 0) ? undefined : val;
        }
    }
    // モード切替時のクリア処理
    if (mode === 'command' && oldConfig.url) payload.url = undefined;
    if (mode === 'url' && oldConfig.command) payload.command = undefined;
    return payload;
}
```

- [ ] **Step 5: ブラウザ確認テスト**

以下を実ブラウザで確認:
- サーバー追加モーダル: 正常に追加できること
- サーバー編集モーダル: 既存値が正しく表示され、保存できること
- モード切替 (command ↔ url): 確認ダイアログが出て、キャンセルで元に戻ること
- タグ追加/削除: モーダル内で正常動作
- エラー表示: 無効な入力で適切なエラーメッセージ

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/static/index.html
git commit -m "refactor(webui): robust add/edit modal with data-loss prevention"
```

---

## Chunk 4: バックエンド — ロックスコープ最適化

### Task 4.1: `refresh_server()` のロック範囲を狭める

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py:219-253`

**背景:** `refresh_server()` が `_create_proxy()`（Stdio サブプロセス起動を含む同期的処理）と `_rebuild_mounts()` をロック内で実行している。この間、`list_tools()` や `call_tool()` が全て待機状態になる。

- [ ] **Step 1: `refresh_server()` のリファクタ**

ロックを「状態読み取り/更新」と「副作用（proxy生成/mount/rebuild）」に分離:

```python
async def refresh_server(self, name: str, config: dict) -> None:
    """プロキシの再生成 + 設定更新。disable 時はアンマウントのみ。"""
    # --- Phase 1: 状態更新（ロック保護）---
    needs_remount = False
    is_disabled = config.get("disabled", False)
    async with self._lock:
        self._refreshing.add(name)
        self._server_configs[name] = config
        self._status[name] = "disabled" if is_disabled else "connected"
        old_proxy = self._proxies.pop(name, None)
        self._tool_cache.pop(name, None)
        if old_proxy:
            needs_remount = True

    # --- Phase 2: 副作用（ロック外）---
    try:
        if needs_remount:
            # ロック外で再マウント（他操作をブロックしない）
            async with self._lock:
                await self._rebuild_mounts()

        if not is_disabled:
            # プロキシ生成（サブプロセス起動を含む可能性があるためロック外）
            try:
                proxy = self._create_proxy(name, config)
                async with self._lock:
                    self._proxies[name] = proxy
                    self.mcp.mount(proxy, namespace=name)
                    self._status[name] = "connected"
                logger.info("Refreshed server %s", name)
            except Exception:
                logger.exception("Failed to refresh server %s", name)
                async with self._lock:
                    self._status[name] = "error"

        # Callbacks outside lock — they may perform IO (rebuild_index calls list_tools)
        for cb in self._on_change_callbacks:
            await cb()
    finally:
        async with self._lock:
            self._refreshing.discard(name)
```

- [ ] **Step 2: `unregister_server()` のロック範囲も見直し**

現在の実装はロック内で `_rebuild_mounts()` を実行している。再マウント中は他操作がブロックされる。短時間で完了するが、念のため確認:

```python
async def unregister_server(self, name: str) -> bool:
    """サーバー削除 + アンマウント。"""
    existed = await self.registry.remove_server(name)
    if not existed:
        return False

    async with self._lock:
        self._proxies.pop(name, None)
        self._server_configs.pop(name, None)
        self._status.pop(name, None)
        self._tool_counts.pop(name, None)
        self._tool_cache.pop(name, None)
        await self._rebuild_mounts()  # ロック内だが高速（単純なリスト操作）

    # Callbacks outside lock
    for cb in self._on_change_callbacks:
        await cb()
    return True
```

（`unregister_server` は既に `_rebuild_mounts` の後にコールバックをロック外で発火している。ロック内の `_rebuild_mounts` は provider リストの再構築のみで軽量なため現状維持で良い。）

- [ ] **Step 3: テスト実行**

```bash
cd /home/rausraus/code/MCP-Hub
python -m pytest tests/ -x -q --tb=short
```

全テストが通過することを確認。

- [ ] **Step 4: コミット**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "perf(backend): narrow lock scope in refresh_server to prevent blocking"
```

---

## Chunk 5: バックエンド — API 最適化

### Task 5.1: `GET /admin/api/servers` のデフォルト `include_tools=false`

**Files:**
- Modify: `src/mcp_hub/admin_router.py:125-158` (get_servers endpoint)

**背景:** 現在 `include_tools` のデフォルトが `True` のため、ポーリング時に全サーバーのツール一覧を毎回取得している。フロントエンドのポーリング更新にはツールデータは不要（ツール数だけで十分）。

- [ ] **Step 1: デフォルト値を変更**

```python
@router.get("/servers", response_model=ServersResponse)
async def get_servers(
    include_tools: bool = Query(False, alias="include_tools", description="Include tool definitions"),
):
```

- [ ] **Step 2: フロントエンドの `loadServers()` を修正**

初期ロード時はツールを含めて取得:

```javascript
// API.getServers にパラメータを追加
async getServers(includeTools = false) {
    const url = includeTools ? '/admin/api/servers?include_tools=true' : '/admin/api/servers';
    const response = await fetch(url);
    if (!response.ok) throw new Error('サーバー一覧の取得に失敗しました');
    return response.json();
},
```

`loadServers()` (2099行) でツール込み取得:
```javascript
async function loadServers() {
    try {
        const data = await API.getServers(true); // include_tools=true for initial load
        servers = data.servers || [];
        renderServers();
        updateMetrics();
        // ...
    }
}
```

- [ ] **Step 3: ポーリング用はツールなしで取得**

`periodicRefresh()` では既に `include_tools` なし（デフォルト `false`）で十分。

`API.getServers()` の呼び出しを確認。`periodicRefresh()` 内で `await API.getServers()` と呼んでいる → デフォルト `false` で OK。

- [ ] **Step 4: テスト実行**

```bash
python -m pytest tests/test_admin_api.py -x -q --tb=short
```

`include_tools` パラメータのデフォルト値変更に関連するテストが通過することを確認。

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/admin_router.py src/mcp_hub/static/index.html
git commit -m "perf(api): default include_tools=false, frontend lazy-loads tools"
```

---

## 依存関係

```
Chunk 1 (polling) ──独立──→ 並列実行可
Chunk 2 (rendering) ──Chunk 1 に依存（polling修正後のコードをベースにする）──→ Chunk 1 の後に実行
Chunk 3 (modal) ──独立──→ 並列実行可（別ファイル領域のため）
Chunk 4 (backend lock) ──独立──→ 並列実行可
Chunk 5 (api optimize) ──独立──→ 並列実行可
```

**推奨実行順:**
1. Chunk 1 + Chunk 3 + Chunk 4 + Chunk 5 を並列（4 @fixer）
2. Chunk 2 を Chunk 1 完了後に実行（1 @fixer）
3. 全テスト + ブラウザ確認 + Docker確認

---

## 検証チェックリスト

- [ ] `python -m pytest tests/ -x -q` 全テスト通過
- [ ] ブラウザ確認: コンソールエラーなし
- [ ] ブラウザ確認: サーバー追加 → 即座にUI反映（遅延なし）
- [ ] ブラウザ確認: サーバー削除 → 即座にUI反映
- [ ] ブラウザ確認: 連続追加/削除操作でUIがブロックされない
- [ ] ブラウザ確認: モーダルの Add/Edit 両モード正常動作
- [ ] ブラウザ確認: モード切替時のデータ喪失防止確認
- [ ] ブラウザ確認: 30秒ポーリングでカードのちらつきなし
- [ ] Docker確認: `docker compose up --build` + HEALTHCHECK通過
- [ ] Docker確認: `curl localhost:8080/admin/api/health` → `{"status":"ok"}`
