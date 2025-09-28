// /view/app.js
// 目的:
//   - ページ遷移なしでフォーム送信（検索ボタン）→ /api/search を呼び出し、
//     /data 配下（dir検索が既定）の一致候補をモーダルに表示。
//   - 候補クリックで元の入力欄へ反映。
// メモ:
//   - 既定で { mode:'dir', scope:'both', limit:200 } を指定。
//   - ZIPを含めたい場合は fetchCandidates のオプションで mode:'both' に切替可。

(() => {
  document.addEventListener('DOMContentLoaded', () => {
    /** @type {HTMLFormElement[]} - ページ内すべての form を対象 */
    const forms = Array.from(document.querySelectorAll('form'));
    if (forms.length === 0) return;

    /** @type {HTMLInputElement|null} - 直近の検索対象入力欄（反映先） */
    let lastActiveTextField = null;

    // モーダルDOMを確保（1回だけ生成して再利用）
    const { backdrop, modal, listEl, statusEl, btnCancel } = ensureModalDom();
    ensureModalStyles();

    // 各フォーム submit をインターセプト
    forms.forEach((form) => {
      form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const textField = form.querySelector('input[type="text"]');
        const q = (textField?.value || '').trim();
        lastActiveTextField = /** @type {HTMLInputElement|null} */(textField);

        openModal(backdrop, listEl, btnCancel);
        setStatus(statusEl, q ? `「${q}」を検索中… (/data 配下)` : 'キーワード未入力です。候補は表示できません。');

        if (!q) {
          const items = buildLocalCandidates('');
          renderList(listEl, items);
          setStatus(statusEl, '（例）東京 / 新宿 / 渋谷 …');
          return;
        }

        try {
          // ★ /data 配下のディレクトリ検索を既定で実行
          const items = await fetchCandidates(q, { mode: 'dir', scope: 'both', limit: 200 });
          renderList(listEl, items);
          setStatus(statusEl, items.length === 0
            ? `「${q}」に一致する結果は見つかりませんでした。(/data)`
            : `${items.length} 件ヒットしました。クリックで確定できます。(/data)`
          );
        } catch (err) {
          console.error(err);
          renderList(listEl, []);
          setStatus(statusEl, '検索中にエラーが発生しました。サーバの起動状態をご確認ください。');
        }
      });
    });

    // 候補クリックで確定（入力欄へ反映 → モーダル閉じ）
    listEl.addEventListener('click', (e) => {
      const li = e.target.closest('li[data-value]');
      if (!li) return;
      const v = li.getAttribute('data-value') || '';
      if (lastActiveTextField) lastActiveTextField.value = v;
      closeModal(backdrop);
      lastActiveTextField?.focus();
    });

    // キーボード操作（Enter/Spaceで選択、Escで閉じる）
    listEl.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('li[data-value]')) {
        e.preventDefault();
        e.target.click();
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !backdrop.hasAttribute('hidden')) {
        closeModal(backdrop);
      }
    });

    // 背景クリック/キャンセルで閉じる
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) closeModal(backdrop);
    });
    btnCancel.addEventListener('click', () => closeModal(backdrop));

    // ===== ユーティリティ群 =====

    function buildLocalCandidates(q) {
      const defaults = ['東京', '新宿', '渋谷', '品川', '上野'];
      const arr = (q ? new Set([q, `${q}駅`, `${q}中央`, `${q}東口`, `${q}西口`]) : new Set(defaults));
      return Array.from(arr).slice(0, 5).map(v => ({ value: v, label: v }));
    }

    /**
     * /api/search を呼び、UI向け候補に整形
     * @param {string} q
     * @param {{mode?:'dir'|'zip'|'both',scope?:'filename'|'content'|'both',limit?:number}} opts
     * @returns {Promise<Array<{value:string,label:string,meta:string,snippet:string}>>}
     */
    async function fetchCandidates(q, opts = {}) {
      const params = new URLSearchParams();
      params.set('q', q);
      if (opts.mode)  params.set('mode', String(opts.mode));
      if (opts.scope) params.set('scope', String(opts.scope));
      if (opts.limit) params.set('limit', String(opts.limit));

      const res = await fetch(`/api/search?${params.toString()}`, { method: 'GET' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!data.ok) return [];

      // data.hits: [{source, zip, path, relpath, reason, snippet, value}]
      return (data.hits || []).map(hit => {
        const label = hit.relpath || hit.path || '';
        const src = hit.source === 'dir' ? 'dir:/data' : `zip:${hit.zip || ''}`;
        const meta = `${src} / ${hit.reason === 'name' ? 'ファイル名一致' : '内容一致'}`;
        const snippet = hit.snippet || '';
        const value = String(hit.value || '').trim() || label;
        return { value, label, meta, snippet };
      });
    }

    function ensureModalDom() {
      let backdrop = document.getElementById('modal-backdrop');
      let modal = document.getElementById('select-modal');
      /** @type {HTMLUListElement} */ let listEl;
      /** @type {HTMLButtonElement} */ let btnCancel;
      /** @type {HTMLDivElement} */ let statusEl;

      if (!backdrop) {
        backdrop = document.createElement('div');
        backdrop.id = 'modal-backdrop';
        backdrop.className = 'modal-backdrop';
        backdrop.setAttribute('hidden', '');
        document.body.appendChild(backdrop);
      }
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'select-modal';
        modal.className = 'modal';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'modal-title');
        modal.tabIndex = -1;

        const h3 = document.createElement('h3');
        h3.id = 'modal-title';
        h3.textContent = '候補を選択してください';

        statusEl = document.createElement('div');
        statusEl.className = 'modal-status';
        statusEl.textContent = '検索待機中…';

        listEl = document.createElement('ul');
        listEl.className = 'modal-list';

        const actions = document.createElement('div');
        actions.className = 'modal-actions';

        btnCancel = document.createElement('button');
        btnCancel.type = 'button';
        btnCancel.id = 'modal-cancel';
        btnCancel.textContent = 'キャンセル';

        actions.appendChild(btnCancel);
        modal.append(h3, statusEl, listEl, actions);
        backdrop.appendChild(modal);
      } else {
        listEl = /** @type {HTMLUListElement} */(modal.querySelector('.modal-list'));
        btnCancel = /** @type {HTMLButtonElement} */(modal.querySelector('#modal-cancel'));
        statusEl = /** @type {HTMLDivElement} */(modal.querySelector('.modal-status'));
      }
      return { backdrop, modal, listEl, statusEl, btnCancel };
    }

    function ensureModalStyles() {
      if (document.getElementById('modal-style')) return;
      const css = `
.modal-backdrop[hidden]{ display:none !important; }
.modal-backdrop{ position: fixed; inset: 0; background: rgba(0,0,0,.4); display:flex; align-items:center; justify-content:center; z-index: 9999; }
.modal{ background:#fff; border:1px solid #ccc; border-radius:8px; width:min(92%, 720px); max-height:76vh; overflow:hidden; display:flex; flex-direction:column; box-shadow:0 10px 30px rgba(0,0,0,.15); }
.modal > h3{ margin:0; padding:14px 18px; font-size:18px; border-bottom:1px solid #eee; }
.modal-status{ padding:8px 16px; font-size:13px; color:#555; border-bottom:1px dashed #eee; }
.modal-list{ margin:0; padding:0; list-style:none; overflow:auto; }
.modal-list li{ padding:12px 16px; cursor:pointer; outline:none; border-bottom:1px solid #f1f1f1; }
.modal-list li:hover, .modal-list li:focus{ background:#f7f7f7; }
.modal-list .label{ display:block; font-size:14px; }
.modal-list .meta{ display:block; font-size:12px; color:#666; }
.modal-list .snippet{ display:block; font-size:12px; color:#444; margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.modal-actions{ display:flex; justify-content:flex-end; gap:8px; padding:10px 14px; border-top:1px solid #eee; }
.modal-actions button{ height:38px; padding:0 14px; font-size:15px; border:1px solid #ccc; border-radius:8px; background:#f3f3f3; cursor:pointer; }
      `.trim();
      const style = document.createElement('style');
      style.id = 'modal-style';
      style.textContent = css;
      document.head.appendChild(style);
    }

    function renderList(listEl, items) {
      if (!Array.isArray(items)) items = [];
      if (items.length === 0) {
        listEl.innerHTML = `<li tabindex="0" role="button" data-value="">
          <span class="label">一致する候補はありません</span>
          <span class="meta">/data の配置やキーワードをご確認ください</span>
        </li>`;
        return;
      }
      listEl.innerHTML = items.map(it => {
        const label = escapeHtml(it.label ?? it.value ?? "");
        const meta = escapeHtml(it.meta ?? "");
        const snippet = escapeHtml(it.snippet ?? "");
        const v = escapeHtml(it.value ?? "");
        return `<li data-value="${v}" tabindex="0" role="button" aria-label="${label} を選択">
          <span class="label">${label}</span>
          ${meta ? `<span class="meta">${meta}</span>` : ""}
          ${snippet ? `<span class="snippet">${snippet}</span>` : ""}
        </li>`;
      }).join('');
    }

    function setStatus(statusEl, msg) { statusEl.textContent = String(msg || ""); }
    function openModal(backdrop, listEl, btnCancel) {
      backdrop.removeAttribute('hidden');
      const first = listEl.querySelector('li[data-value]') || btnCancel;
      first?.focus();
    }
    function closeModal(backdrop) { backdrop.setAttribute('hidden', ''); }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, m => (
        {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[m]
      ));
    }
  });
})();
