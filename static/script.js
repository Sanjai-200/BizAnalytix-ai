/* BizAnalytix AI - frontend logic (single file, plain functions).
   Handles the chat page (data-page="app"), admin dashboard
   (data-page="admin"), and developer dashboard (data-page="developer"). */

const page = document.body.dataset.page;

// ---------------------------------------------------------------------
// CONFIG-NOT-SET BANNER (shared)
// ---------------------------------------------------------------------

(function checkFirebaseConfigured() {
  const banner = document.getElementById('config-banner');
  if (!banner) return;
  if (typeof firebaseConfig === 'undefined' || !firebaseConfig.apiKey || firebaseConfig.apiKey.indexOf('YOUR_') === 0) {
    banner.classList.add('show');
  }
})();

// ---------------------------------------------------------------------
// AUTH HELPERS (shared)
// ---------------------------------------------------------------------

function getToken() {
  if (typeof fbAuth === 'undefined' || !fbAuth.currentUser) return Promise.resolve(null);
  return fbAuth.currentUser.getIdToken();
}

async function apiFetch(url, options = {}) {
  const token = await getToken();
  if (!token) {
    window.location.href = '/login';
    return Promise.reject(new Error('Not signed in'));
  }
  const headers = Object.assign(
    { 'Authorization': 'Bearer ' + token },
    options.headers || {}
  );
  let res;
  try {
    res = await fetch(url, Object.assign({}, options, { headers }));
  } catch (networkErr) {
    throw new Error('Network error. Check your connection and try again.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || ('Request failed (' + res.status + ')'));
  }
  return data;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str || '';
  return div.innerHTML;
}

// ---------------------------------------------------------------------
// TOAST (shared)
// ---------------------------------------------------------------------

function showToast(message, type = 'default') {
  const wrap = document.getElementById('toast-wrap');
  if (!wrap) { console.log(message); return; }
  const el = document.createElement('div');
  el.className = 'toast' + (type === 'error' ? ' error' : type === 'success' ? ' success' : '');
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------------
// GENERIC CONFIRM MODAL (shared, used on chat page)
// ---------------------------------------------------------------------

function confirmAction(title, body, confirmLabel = 'Delete') {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirm-modal');
    if (!modal) { resolve(window.confirm(body)); return; }
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-body').textContent = body;
    const okBtn = document.getElementById('confirm-ok');
    okBtn.textContent = confirmLabel;
    modal.classList.add('show');

    function cleanup(result) {
      modal.classList.remove('show');
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }

    const cancelBtn = document.getElementById('confirm-cancel');
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
  });
}

// ---------------------------------------------------------------------
// LOGOUT + THEME (shared)
// ---------------------------------------------------------------------

const logoutBtn = document.getElementById('logout-btn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    await fbAuth.signOut();
    window.location.href = '/login';
  });
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
}
applyTheme(localStorage.getItem('theme') || 'light');
const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('theme', next);
    applyTheme(next);
  });
}

function roleBadgeHtml(role) {
  return `<span class="role-badge ${role}">${role}</span>`;
}

// ---------------------------------------------------------------------
// SHARED: AI / CHATBOT CONFIG PANEL (used by admin + developer pages)
// ---------------------------------------------------------------------

function renderConfigForm(container, config) {
  container.innerHTML = `
    <div class="form-row">
      <div>
        <label for="cfg-model">Gemini model</label>
        <input type="text" id="cfg-model" value="${escapeHtml(config.model)}">
      </div>
      <div>
        <label for="cfg-temp">Response creativity (temperature) <span class="range-value" id="cfg-temp-val">${config.temperature}</span></label>
        <input type="range" id="cfg-temp" min="0" max="1" step="0.05" value="${config.temperature}">
      </div>
    </div>
    <label for="cfg-welcome">Welcome message (shown on a new chat)</label>
    <input type="text" id="cfg-welcome" value="${escapeHtml(config.welcome_message)}" maxlength="200">

    <label for="cfg-prompt">Chatbot system prompt (defines its persona and topic boundaries)</label>
    <textarea id="cfg-prompt" rows="8">${escapeHtml(config.system_prompt)}</textarea>

    <div class="error-msg" id="cfg-error"></div>
    <button class="btn-primary" id="cfg-save" style="width:auto;padding:9px 18px;">Save configuration</button>
  `;

  const tempInput = container.querySelector('#cfg-temp');
  tempInput.addEventListener('input', () => {
    container.querySelector('#cfg-temp-val').textContent = tempInput.value;
  });

  container.querySelector('#cfg-save').addEventListener('click', async () => {
    const btn = container.querySelector('#cfg-save');
    const errorEl = container.querySelector('#cfg-error');
    errorEl.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      await apiFetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: container.querySelector('#cfg-model').value,
          temperature: parseFloat(tempInput.value),
          welcome_message: container.querySelector('#cfg-welcome').value,
          system_prompt: container.querySelector('#cfg-prompt').value
        })
      });
      showToast('Configuration saved.', 'success');
    } catch (e) {
      errorEl.textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save configuration';
    }
  });
}

async function loadConfigPanel() {
  const container = document.getElementById('config-form-container');
  if (!container) return;
  try {
    const config = await apiFetch('/api/config');
    renderConfigForm(container, config);
  } catch (e) {
    container.innerHTML = `<p class="error-msg">${escapeHtml(e.message)}</p>`;
  }
}

// =======================================================================
// CHAT PAGE
// =======================================================================

if (page === 'app') {
  let currentChatId = null;
  let currentDatasetId = null; // null = default: answer from ALL of the user's own datasets combined
  let currentDatasetFilename = null;
  let hasAnyOwnDataset = false;
  let currentDatasetOwnerUid = null; // set when using a shared/foreign dataset
  let shareTargetDatasetId = null;

  fbAuth.onAuthStateChanged(async (user) => {
    if (!user) {
      window.location.href = '/login';
      return;
    }
    try {
      const info = await apiFetch('/api/auth/verify', { method: 'POST' });
      document.getElementById('user-email').textContent = info.email;
      const roleChip = document.getElementById('role-chip');
      roleChip.textContent = info.role;
      roleChip.className = 'role-badge ' + info.role;
      document.getElementById('welcome-message').textContent = info.welcome_message;

      if (info.role === 'developer' || info.role === 'admin') {
        document.getElementById('developer-link').style.display = 'inline';
        document.getElementById('shared-section-title').style.display = 'flex';
        loadSharedDatasets();
      }
      if (info.role === 'admin') {
        document.getElementById('admin-link').style.display = 'inline';
      }

      loadChats();
      loadDatasets();
    } catch (e) {
      showToast('Could not load your account: ' + e.message, 'error');
    }
  });

  document.getElementById('new-chat-btn').addEventListener('click', () => {
    currentChatId = null;
    currentDatasetId = null; // back to the default: answer from all my datasets
    currentDatasetOwnerUid = null;
    renderActiveDatasetPill();
    document.getElementById('chat-area').innerHTML =
      '<div class="empty-state"><h3>New chat</h3><p>Ask a business or data question below.</p></div>';
    highlightActiveChat();
    highlightActiveDataset();
  });

  async function loadChats() {
    try {
      const data = await apiFetch('/api/chats');
      const list = document.getElementById('chat-list');
      list.innerHTML = '';
      if (data.chats.length === 0) {
        list.innerHTML = '<div class="sidebar-empty">No chats yet</div>';
      }
      data.chats.forEach((chat) => {
        const item = document.createElement('div');
        item.className = 'side-item';
        item.dataset.chatId = chat.chat_id;
        item.innerHTML = `<span class="name-text">${escapeHtml(chat.title || 'Chat')}</span>`;
        item.addEventListener('click', () => openChat(chat.chat_id, chat.dataset_id));
        list.appendChild(item);
      });
      highlightActiveChat();
    } catch (e) {
      showToast('Could not load chat history: ' + e.message, 'error');
    }
  }

  async function openChat(chatId, datasetId) {
    currentChatId = chatId;
    currentDatasetId = datasetId || null;
    currentDatasetOwnerUid = null; // reopening a saved chat always resumes with the owner's own dataset
    renderActiveDatasetPill();
    highlightActiveChat();
    highlightActiveDataset();
    try {
      const data = await apiFetch('/api/chats/' + chatId);
      const area = document.getElementById('chat-area');
      area.innerHTML = '';
      data.messages.forEach((m) => appendMessage(m.role, m.content, m.timestamp));
      area.scrollTop = area.scrollHeight;
    } catch (e) {
      showToast('Could not load this chat: ' + e.message, 'error');
    }
  }

  function highlightActiveChat() {
    document.querySelectorAll('#chat-list .side-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.chatId === currentChatId);
    });
  }

  async function loadDatasets() {
    try {
      const data = await apiFetch('/api/datasets');
      const list = document.getElementById('dataset-list');
      list.innerHTML = '';
      hasAnyOwnDataset = data.datasets.length > 0;
      if (data.datasets.length === 0) {
        list.innerHTML = '<div class="sidebar-empty">No datasets yet</div>';
      }
      renderActiveDatasetPill(); // refresh the "answering from all my datasets" pill now that we know the count
      data.datasets.forEach((ds) => {
        const item = document.createElement('div');
        item.className = 'side-item';
        item.dataset.datasetId = ds.dataset_id;
        item.innerHTML =
          `<span class="name-text" title="${escapeHtml(ds.filename)}">${escapeHtml(ds.filename)}</span>` +
          `<span class="row-actions">
             <button class="icon-btn share-btn" title="Share with a developer/admin">&#8599;</button>
             <button class="icon-btn" title="Delete dataset">&times;</button>
           </span>`;
        item.querySelector('.name-text').addEventListener('click', () => {
          currentDatasetId = ds.dataset_id;
          currentDatasetOwnerUid = null;
          currentDatasetFilename = ds.filename;
          renderActiveDatasetPill();
          highlightActiveDataset();
        });
        item.querySelector('.share-btn').addEventListener('click', (e) => {
          e.stopPropagation();
          openShareModal(ds.dataset_id);
        });
        item.querySelectorAll('.icon-btn')[1].addEventListener('click', async (e) => {
          e.stopPropagation();
          const ok = await confirmAction('Delete dataset?', `"${ds.filename}" will be permanently deleted. This cannot be undone.`);
          if (!ok) return;
          try {
            await apiFetch('/api/datasets/' + ds.dataset_id, { method: 'DELETE' });
            if (currentDatasetId === ds.dataset_id) {
              currentDatasetId = null;
              renderActiveDatasetPill();
            }
            loadDatasets();
            showToast('Dataset deleted.', 'success');
          } catch (err) {
            showToast(err.message, 'error');
          }
        });
        list.appendChild(item);
      });
      highlightActiveDataset();
    } catch (e) {
      showToast('Could not load datasets: ' + e.message, 'error');
    }
  }

  async function loadSharedDatasets() {
    try {
      const data = await apiFetch('/api/shared-datasets');
      const list = document.getElementById('shared-dataset-list');
      list.innerHTML = '';
      if (data.datasets.length === 0) {
        list.innerHTML = '<div class="sidebar-empty">None yet</div>';
        return;
      }
      data.datasets.forEach((ds) => {
        const item = document.createElement('div');
        item.className = 'side-item';
        item.dataset.datasetId = ds.dataset_id;
        item.innerHTML =
          `<span class="name-text" title="${escapeHtml(ds.filename)}">${escapeHtml(ds.filename)}
             <span class="owner-tag">(${escapeHtml(ds.owner_email || 'unknown')})</span></span>`;
        item.addEventListener('click', () => {
          currentDatasetId = ds.dataset_id;
          currentDatasetOwnerUid = ds.owner_uid;
          currentDatasetFilename = ds.filename;
          renderActiveDatasetPill();
          highlightActiveDataset();
        });
        list.appendChild(item);
      });
      highlightActiveDataset();
    } catch (e) {
      showToast('Could not load shared datasets: ' + e.message, 'error');
    }
  }

  function highlightActiveDataset() {
    document.querySelectorAll('#dataset-list .side-item, #shared-dataset-list .side-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.datasetId === currentDatasetId);
    });
  }

  function renderActiveDatasetPill() {
    const pill = document.getElementById('active-dataset-pill');

    if (currentDatasetId) {
      // A specific dataset was clicked - narrow the question to just that one.
      const name = currentDatasetFilename || 'selected dataset';
      const label = currentDatasetOwnerUid
        ? `Using shared dataset: ${escapeHtml(name)}`
        : `Using only: ${escapeHtml(name)}`;
      pill.innerHTML = `<span class="dataset-pill">${label} <button class="icon-btn" id="clear-dataset" title="Answer from all my datasets instead">&times;</button></span>`;
      document.getElementById('clear-dataset').addEventListener('click', () => {
        currentDatasetId = null;
        currentDatasetOwnerUid = null;
        currentDatasetFilename = null;
        renderActiveDatasetPill();
        highlightActiveDataset();
      });
      return;
    }

    // Default mode: no single dataset picked, so every question is
    // automatically answered using ALL of this user's uploaded datasets.
    if (hasAnyOwnDataset) {
      pill.innerHTML = `<span class="dataset-pill">Answering from all my uploaded datasets</span>`;
    } else {
      pill.innerHTML = '';
    }
  }

  // ---- Share modal ----
  function openShareModal(datasetId) {
    shareTargetDatasetId = datasetId;
    document.getElementById('share-email').value = '';
    document.getElementById('share-error').textContent = '';
    document.getElementById('share-modal').classList.add('show');
  }
  document.getElementById('share-cancel').addEventListener('click', () => {
    document.getElementById('share-modal').classList.remove('show');
  });
  document.getElementById('share-confirm').addEventListener('click', async () => {
    const email = document.getElementById('share-email').value.trim();
    const errorEl = document.getElementById('share-error');
    if (!email) { errorEl.textContent = 'Enter an email address.'; return; }
    try {
      await apiFetch(`/api/datasets/${shareTargetDatasetId}/share`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      document.getElementById('share-modal').classList.remove('show');
      showToast('Dataset shared successfully.', 'success');
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });

  // ---- Upload ----
  const fileInput = document.getElementById('file-input');
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    showToast('Uploading and processing "' + file.name + '"...');
    try {
      const token = await getToken();
      if (!token) { window.location.href = '/login'; return; }
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: formData
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Upload failed.');
      showToast(`Uploaded "${data.filename}" - ${data.record_count} rows ready.`, 'success');
      // Don't narrow to just this one file - the default "answer from all
      // my datasets" mode already includes it automatically.
      currentDatasetId = null;
      currentDatasetOwnerUid = null;
      currentDatasetFilename = null;
      renderActiveDatasetPill();
      loadDatasets();
    } catch (e) {
      showToast(e.message, 'error');
    } finally {
      fileInput.value = '';
    }
  });

  // ---- Composer ----
  const messageInput = document.getElementById('message-input');
  const sendBtn = document.getElementById('send-btn');

  messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 140) + 'px';
  });
  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  sendBtn.addEventListener('click', sendMessage);

  async function sendMessage() {
    const text = messageInput.value.trim();
    if (!text) return;

    const area = document.getElementById('chat-area');
    if (area.querySelector('.empty-state')) area.innerHTML = '';

    appendMessage('user', text, new Date().toISOString());
    messageInput.value = '';
    messageInput.style.height = 'auto';

    const thinkingEl = appendMessage('assistant', '', null, true);

    sendBtn.disabled = true;
    try {
      const body = {
        message: text,
        chat_id: currentChatId,
        dataset_id: currentDatasetId,
        dataset_owner_uid: currentDatasetOwnerUid
      };
      const data = await apiFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      currentChatId = data.chat_id;
      thinkingEl.remove();
      let note = null;
      if (!currentDatasetId && data.datasets_used) {
        note = data.datasets_used < data.datasets_total
          ? `Answered using the ${data.datasets_used} most recent of your ${data.datasets_total} datasets`
          : `Answered using all ${data.datasets_used} of your datasets`;
      }
      appendMessage('assistant', data.answer, new Date().toISOString(), false, false, note);
      loadChats();
    } catch (e) {
      thinkingEl.remove();
      appendMessage('assistant', 'Something went wrong: ' + e.message, new Date().toISOString(), false, true);
    } finally {
      sendBtn.disabled = false;
    }
  }

  function appendMessage(role, content, timestamp, isThinking = false, isError = false, note = null) {
    const area = document.getElementById('chat-area');
    const el = document.createElement('div');
    el.className = 'msg ' + role + (isError ? ' error-msg-bubble' : '');
    if (isThinking) {
      el.innerHTML = '<span class="loading-dots">Thinking</span>';
    } else {
      el.textContent = content;
      if (note) {
        const noteEl = document.createElement('span');
        noteEl.className = 'msg-time';
        noteEl.style.display = 'block';
        noteEl.style.marginTop = '2px';
        noteEl.textContent = note;
        el.appendChild(noteEl);
      }
      if (timestamp) {
        const time = document.createElement('span');
        time.className = 'msg-time';
        time.textContent = new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        el.appendChild(time);
      }
    }
    area.appendChild(el);
    area.scrollTop = area.scrollHeight;
    return el;
  }
}

// =======================================================================
// ADMIN PAGE
// =======================================================================

if (page === 'admin') {
  fbAuth.onAuthStateChanged(async (user) => {
    if (!user) { window.location.href = '/login'; return; }
    try {
      const info = await apiFetch('/api/auth/verify', { method: 'POST' });
      if (info.role !== 'admin') {
        document.getElementById('admin-denied').style.display = 'block';
        return;
      }
      document.getElementById('admin-content').style.display = 'block';
      loadOverview();
      loadUsers();
      loadConfigPanel();
      loadAllDatasets();
    } catch (e) {
      document.getElementById('admin-denied').style.display = 'block';
    }
  });

  async function loadOverview() {
    try {
      const data = await apiFetch('/api/admin/overview');
      document.getElementById('stat-users').textContent = data.total_users;
      document.getElementById('stat-datasets').textContent = data.total_datasets;
      document.getElementById('stat-records').textContent = data.total_records;
    } catch (e) {
      showToast('Could not load overview stats: ' + e.message, 'error');
    }
  }

  async function loadUsers() {
    try {
      const data = await apiFetch('/api/admin/users');
      const tbody = document.getElementById('users-tbody');
      tbody.innerHTML = '';
      data.users.forEach((u) => {
        const tr = document.createElement('tr');
        const joined = u.created_at ? new Date(u.created_at).toLocaleDateString() : '-';
        tr.innerHTML =
          `<td>${escapeHtml(u.email)}</td>` +
          `<td>${roleBadgeHtml(u.role)}</td>` +
          `<td><span class="status-badge ${u.status || 'active'}">${u.status || 'active'}</span></td>` +
          `<td>${joined}</td>` +
          `<td>
            <select class="role-select" data-uid="${u.uid}" data-field="role">
              <option value="user" ${u.role === 'user' ? 'selected' : ''}>user</option>
              <option value="developer" ${u.role === 'developer' ? 'selected' : ''}>developer</option>
              <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>admin</option>
            </select>
            <select class="role-select" data-uid="${u.uid}" data-field="status" style="margin-left:6px;">
              <option value="active" ${(u.status || 'active') === 'active' ? 'selected' : ''}>active</option>
              <option value="disabled" ${u.status === 'disabled' ? 'selected' : ''}>disabled</option>
            </select>
          </td>`;
        tbody.appendChild(tr);
      });

      tbody.querySelectorAll('select[data-field="role"]').forEach((sel) => {
        sel.addEventListener('change', () => updateUser(sel.dataset.uid, 'role', sel.value));
      });
      tbody.querySelectorAll('select[data-field="status"]').forEach((sel) => {
        sel.addEventListener('change', () => updateUser(sel.dataset.uid, 'status', sel.value));
      });
    } catch (e) {
      showToast('Could not load users: ' + e.message, 'error');
    }
  }

  async function updateUser(uid, field, value) {
    try {
      await apiFetch(`/api/admin/users/${uid}/${field}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value })
      });
      showToast('Updated successfully.', 'success');
      loadOverview();
    } catch (e) {
      showToast(e.message, 'error');
      loadUsers();
    }
  }

  async function loadAllDatasets() {
    try {
      const data = await apiFetch('/api/shared-datasets'); // admin role returns every dataset
      const tbody = document.getElementById('all-datasets-tbody');
      tbody.innerHTML = '';
      if (data.datasets.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:var(--ink-soft);">No datasets uploaded yet.</td></tr>';
        return;
      }
      data.datasets.forEach((ds) => {
        const uploaded = ds.uploaded_at ? new Date(ds.uploaded_at).toLocaleDateString() : '-';
        const tr = document.createElement('tr');
        tr.innerHTML =
          `<td>${escapeHtml(ds.filename)}</td>` +
          `<td>${escapeHtml(ds.owner_email || 'unknown')}</td>` +
          `<td class="num">${ds.record_count}</td>` +
          `<td>${uploaded}</td>`;
        tbody.appendChild(tr);
      });
    } catch (e) {
      showToast('Could not load datasets: ' + e.message, 'error');
    }
  }
}

// =======================================================================
// DEVELOPER PAGE
// =======================================================================

if (page === 'developer') {
  fbAuth.onAuthStateChanged(async (user) => {
    if (!user) { window.location.href = '/login'; return; }
    try {
      const info = await apiFetch('/api/auth/verify', { method: 'POST' });
      if (info.role !== 'developer' && info.role !== 'admin') {
        document.getElementById('dev-denied').style.display = 'block';
        return;
      }
      document.getElementById('dev-content').style.display = 'block';
      loadOverview();
      loadConfigPanel();
      loadSharedDatasets();
    } catch (e) {
      document.getElementById('dev-denied').style.display = 'block';
    }
  });

  async function loadOverview() {
    try {
      const data = await apiFetch('/api/admin/overview');
      document.getElementById('stat-datasets').textContent = data.total_datasets;
      document.getElementById('stat-records').textContent = data.total_records;
    } catch (e) {
      showToast('Could not load overview stats: ' + e.message, 'error');
    }
  }

  async function loadSharedDatasets() {
    try {
      const data = await apiFetch('/api/shared-datasets');
      const tbody = document.getElementById('shared-datasets-tbody');
      tbody.innerHTML = '';
      if (data.datasets.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="color:var(--ink-soft);">No datasets have been shared with you yet.</td></tr>';
        return;
      }
      data.datasets.forEach((ds) => {
        const uploaded = ds.uploaded_at ? new Date(ds.uploaded_at).toLocaleDateString() : '-';
        const tr = document.createElement('tr');
        tr.innerHTML =
          `<td>${escapeHtml(ds.filename)}</td>` +
          `<td>${escapeHtml(ds.owner_email || 'unknown')}</td>` +
          `<td class="num">${ds.record_count}</td>` +
          `<td>${uploaded}</td>` +
          `<td><a href="/">Use in chat &rarr;</a></td>`;
        tbody.appendChild(tr);
      });
    } catch (e) {
      showToast('Could not load shared datasets: ' + e.message, 'error');
    }
  }
}
