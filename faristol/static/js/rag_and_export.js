// --- Funcions RAG Híbrid Local (Docling + BM25 FTS5 + embeddinggemma-2) ---

function alternarPestanyaAntigravity() {
    if (el.tabAntigravityBtn) {
        el.tabAntigravityBtn.style.display = 'flex';
    }
}

function notificarTemaAntigravity() {
    // Més net: el panell RAG Local és natiu i hereta les variables CSS del tema automàticament
}

function recarregarAntigravityFrame() {
    carregarPanellAntigravity(true);
}

function obtenirNomAbastRag() {
    if (!state.activeCollectionId) return 'Tota la Biblioteca';
    if (state.activeCollectionId === 'unfiled') return 'Sense arxivar';
    if (state.activeCollectionId === 'trash') return 'Paperera';
    const col = (state.collections || []).find(c => c.id === state.activeCollectionId);
    return col ? col.name : 'Tota la Biblioteca';
}

function inicialitzarEventsRagLocal() {
    const btnSend = document.getElementById('btn-rag-send');
    const chatInput = document.getElementById('rag-chat-input');
    const btnNewChat = document.getElementById('btn-rag-new-chat');
    const btnIndexScope = document.getElementById('btn-rag-index-scope');
    const btnToggleAll = document.getElementById('btn-rag-toggle-all-sources');
    const btnCollapse = document.getElementById('btn-rag-collapse-sources');
    const btnModeEv = document.getElementById('btn-rag-mode-evidences');
    const btnModeSyn = document.getElementById('btn-rag-mode-synthesis');

    if (btnSend) {
        btnSend.onclick = () => enviarRagPregunta();
    }
    if (chatInput) {
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                enviarRagPregunta();
            }
        });
    }
    if (btnNewChat) {
        btnNewChat.onclick = () => {
            state.ragChatMessages = [];
            renderitzarRagChat();
        };
    }
    if (btnIndexScope) {
        btnIndexScope.onclick = () => iniciarIndexacioRagLocal(false);
    }
    if (btnToggleAll) {
        btnToggleAll.onclick = () => {
            const total = state.ragSources.length;
            const sel = state.ragSelectedFilenames.size;
            if (sel === total && total > 0) {
                state.ragSelectedFilenames.clear();
            } else {
                state.ragSources.forEach(s => state.ragSelectedFilenames.add(s.filename));
            }
            renderitzarFontsRagLocal();
        };
    }
    if (btnCollapse) {
        btnCollapse.onclick = () => {
            state.ragSourcesCollapsed = !state.ragSourcesCollapsed;
            const listEl = document.getElementById('rag-sources-list');
            const iconEl = document.getElementById('icon-rag-collapse-sources');
            if (listEl) listEl.style.display = state.ragSourcesCollapsed ? 'none' : 'flex';
            if (iconEl) iconEl.className = state.ragSourcesCollapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up';
        };
    }
    if (btnModeEv && btnModeSyn) {
        const actualizarEstilModes = () => {
            const isEv = state.ragMode === 'evidences';
            btnModeEv.style.borderColor = isEv ? '#a855f7' : 'var(--border-color)';
            btnModeEv.style.background = isEv ? 'rgba(168, 85, 247, 0.16)' : 'transparent';
            btnModeEv.style.color = isEv ? '#a855f7' : 'var(--text-muted)';
            btnModeEv.style.fontWeight = isEv ? '700' : '600';

            btnModeSyn.style.borderColor = !isEv ? '#a855f7' : 'var(--border-color)';
            btnModeSyn.style.background = !isEv ? 'rgba(168, 85, 247, 0.16)' : 'transparent';
            btnModeSyn.style.color = !isEv ? '#a855f7' : 'var(--text-muted)';
            btnModeSyn.style.fontWeight = !isEv ? '700' : '600';
        };
        btnModeEv.onclick = () => { state.ragMode = 'evidences'; actualizarEstilModes(); };
        btnModeSyn.onclick = () => { state.ragMode = 'synthesis'; actualizarEstilModes(); };
    }
}

async function carregarPanellAntigravity() {
    const titleEl = document.getElementById('rag-active-title');
    if (titleEl) {
        const nom = obtenirNomAbastRag();
        titleEl.textContent = nom;
        titleEl.title = `Abast actiu: ${nom}`;
    }
    await carregarFontsRagLocal();
    await comprovarEstatRagLocal();
    renderitzarRagChat();
}

async function comprovarEstatRagLocal() {
    const badgeEl = document.getElementById('antigravity-status-badge');
    const bannerEl = document.getElementById('rag-indexing-banner');
    const textEl = document.getElementById('rag-indexing-text');
    const pctEl = document.getElementById('rag-indexing-pct');

    try {
        const res = await fetch('/api/rag/status');
        if (!res.ok) return;
        const data = await res.json();
        const totalChunks = (data.vector_store && data.vector_store.total_chunks) || 0;
        const idxDocs = data.indexed_files_count || 0;
        const idxState = data.indexing || {};

        if (badgeEl) {
            badgeEl.innerHTML = `<i class="fa-solid fa-database" style="font-size: 9px;"></i> <span>${idxDocs} docs · ${totalChunks} ch.</span>`;
            badgeEl.title = `Model: ${data.embedding_model} | Total: ${idxDocs} documents indexats (${totalChunks} fragments)`;
        }

        if (idxState.active) {
            if (bannerEl) bannerEl.style.display = 'block';
            if (textEl) {
                textEl.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin" style="color: #a855f7;"></i> ${escapeHtml(idxState.message || 'Indexant...')}`;
            }
            if (pctEl && idxState.total > 0) {
                pctEl.textContent = `${idxState.done}/${idxState.total}`;
            }
            if (!state.ragIndexingPollTimer) {
                state.ragIndexingPollTimer = setInterval(async () => {
                    await comprovarEstatRagLocal();
                    await carregarFontsRagLocal();
                }, 2000);
            }
        } else {
            if (state.ragIndexingPollTimer) {
                clearInterval(state.ragIndexingPollTimer);
                state.ragIndexingPollTimer = null;
                await carregarFontsRagLocal();
            }
            if (bannerEl) bannerEl.style.display = 'none';
        }
    } catch (err) {
        console.warn('Error comprovant estat RAG Local:', err);
    }
}

async function carregarFontsRagLocal() {
    const colId = (state.activeCollectionId && state.activeCollectionId !== 'trash') ? state.activeCollectionId : '';
    const tag = state.activeTagFilter || '';
    try {
        const params = new URLSearchParams();
        if (colId) params.set('collection_id', colId);
        if (tag) params.set('tag', tag);
        const res = await fetch(`/api/rag/sources?${params.toString()}`);
        if (res.ok) {
            const data = await res.json();
            const prevCount = state.ragSources.length;
            state.ragSources = data.sources || [];
            if (prevCount === 0 || state.ragSelectedFilenames.size === 0) {
                state.ragSelectedFilenames = new Set(state.ragSources.map(s => s.filename));
            } else {
                const validSet = new Set(state.ragSources.map(s => s.filename));
                for (const fn of Array.from(state.ragSelectedFilenames)) {
                    if (!validSet.has(fn)) state.ragSelectedFilenames.delete(fn);
                }
                if (state.ragSelectedFilenames.size === 0 && state.ragSources.length > 0) {
                    state.ragSelectedFilenames = new Set(state.ragSources.map(s => s.filename));
                }
            }
        }
    } catch (err) {
        console.error('Error carregant fonts del RAG Local:', err);
    }
    renderitzarFontsRagLocal();
}

function renderitzarFontsRagLocal() {
    const listEl = document.getElementById('rag-sources-list');
    const countEl = document.getElementById('rag-sources-count');
    const scopeEl = document.getElementById('rag-chat-scope-label');
    const toggleBtn = document.getElementById('btn-rag-toggle-all-sources');
    const inputEl = document.getElementById('rag-chat-input');

    if (!listEl) return;
    listEl.innerHTML = '';

    const total = state.ragSources.length;
    const selCount = state.ragSelectedFilenames.size;
    const idxCount = state.ragSources.filter(s => s.indexed).length;

    if (countEl) {
        countEl.textContent = `Fonts (${selCount}/${total} · ${idxCount} indexades)`;
    }
    if (scopeEl) {
        if (total === 0) scopeEl.textContent = 'Sense fonts';
        else if (selCount === total) scopeEl.textContent = `Totes (${total})`;
        else scopeEl.textContent = `${selCount} de ${total} fonts`;
    }
    if (inputEl) {
        const nomAbast = obtenirNomAbastRag();
        inputEl.placeholder = `Pregunta a ${nomAbast} (${selCount} fonts)... (Enter per enviar)`;
    }
    if (toggleBtn) {
        toggleBtn.textContent = (selCount === total && total > 0) ? 'Cap' : 'Totes';
    }

    if (total === 0) {
        listEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); font-size: 11px; padding: 8px 0;">Cap document en aquesta carpeta.</div>`;
        return;
    }

    state.ragSources.forEach(s => {
        const isSelected = state.ragSelectedFilenames.has(s.filename);
        const item = document.createElement('div');
        item.className = `nlm-source-item${isSelected ? ' selected' : ''}`;

        const isHtml = s.type === 'html';
        const isNote = s.type === 'note';
        const iconClass = isNote ? 'fa-regular fa-note-sticky' : (isHtml ? 'fa-solid fa-globe' : 'fa-solid fa-file-pdf');
        const iconColor = isNote ? '#8b5cf6' : (isHtml ? '#38bdf8' : '#ef4444');
        const statusBadge = s.indexed
            ? `<span title="Indexat al RAG local (${s.chunk_count} fragments)" style="font-size: 9px; padding: 1px 4px; border-radius: 3px; background: rgba(16, 185, 129, 0.14); color: #10b981; font-weight: 600; flex-shrink: 0;">✓ ${s.chunk_count}</span>`
            : `<button class="btn btn-xs rag-index-one-btn" title="Indexar aquest document ara" style="font-size: 9px; padding: 1px 5px; border-radius: 3px; background: rgba(245, 158, 11, 0.16); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.35); cursor: pointer; flex-shrink: 0;">⚡ Indexar</button>`;

        item.innerHTML = `
            <input type="checkbox" class="nlm-source-checkbox" ${isSelected ? 'checked' : ''} style="margin: 0; cursor: pointer; flex-shrink: 0;">
            <i class="${iconClass}" style="color: ${iconColor}; font-size: 11px; flex-shrink: 0;"></i>
            <span class="nlm-source-title" style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px;" title="${escapeHtml(s.title)} (${escapeHtml(s.filename)})">${escapeHtml(s.filename)}</span>
            ${statusBadge}
            <button class="btn btn-xs btn-icon-only nlm-open-pdf-btn" title="${isNote ? 'Obrir nota a l\'editor' : 'Obrir document al visor central'}" style="font-size: 10px; padding: 1px 4px; opacity: 0.75;"><i class="fa-solid ${isNote ? 'fa-pen-to-square' : 'fa-book-open'}"></i></button>
        `;

        const chk = item.querySelector('.nlm-source-checkbox');
        chk.addEventListener('change', (e) => {
            e.stopPropagation();
            if (chk.checked) state.ragSelectedFilenames.add(s.filename);
            else state.ragSelectedFilenames.delete(s.filename);
            renderitzarFontsRagLocal();
        });

        const indexOneBtn = item.querySelector('.rag-index-one-btn');
        if (indexOneBtn) {
            indexOneBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                iniciarIndexacioRagLocal(false, [s.filename]);
            });
        }

        const openBtn = item.querySelector('.nlm-open-pdf-btn');
        if (openBtn) {
            openBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (s.type === 'note') {
                    obrirNota(s.filename, true, true);
                } else {
                    obrirPdf(s.filename, true);
                }
            });
        }

        item.addEventListener('click', (e) => {
            if (e.target.closest('.nlm-open-pdf-btn') || e.target.closest('.rag-index-one-btn') || e.target === chk) return;
            chk.checked = !chk.checked;
            if (chk.checked) state.ragSelectedFilenames.add(s.filename);
            else state.ragSelectedFilenames.delete(s.filename);
            renderitzarFontsRagLocal();
        });

        listEl.appendChild(item);
    });
}

async function iniciarIndexacioRagLocal(force = false, explicitFiles = null) {
    const filesToIndex = explicitFiles || Array.from(state.ragSelectedFilenames);
    if (!filesToIndex || filesToIndex.length === 0) {
        alert('Selecciona almenys un document per indexar.');
        return;
    }
    try {
        const res = await fetch('/api/rag/index', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filenames: filesToIndex,
                collection_id: state.activeCollectionId,
                force: force,
            }),
        });
        await res.json();
        await comprovarEstatRagLocal();
    } catch (err) {
        console.error('Error iniciant indexació RAG Local:', err);
    }
}

async function enviarRagPregunta(text) {
    const inputEl = document.getElementById('rag-chat-input');
    const query = (text || (inputEl ? inputEl.value : '')).trim();
    if (!query) return;

    if (state.ragSources.length > 0 && state.ragSelectedFilenames.size === 0) {
        alert('Selecciona almenys un document de la llista per consultar el RAG Local.');
        return;
    }

    if (inputEl) inputEl.value = '';

    state.ragChatMessages.push({
        role: 'user',
        text: query,
        mode: state.ragMode,
        sourcesCount: state.ragSelectedFilenames.size,
        totalSources: state.ragSources.length,
    });
    state.ragIsQuerying = true;
    renderitzarRagChat();

    const payload = {
        query: query,
        mode: state.ragMode,
        top_k: 6,
        filenames: Array.from(state.ragSelectedFilenames),
        collection_id: state.activeCollectionId,
        include_notes: true,
    };

    try {
        if (state.ragMode === 'synthesis') {
            const res = await fetch('/api/rag/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                throw new Error(errData.detail || 'Error consultant el RAG Local en streaming');
            }
            if (!res.body) throw new Error('ReadableStream no disponible');

            const assistantMsg = {
                role: 'assistant',
                text: '',
                took_ms: 0,
                mode: 'synthesis',
                citations: {},
                citation_details: {},
                references: [],
                sources: {},
                streaming: true,
            };
            state.ragChatMessages.push(assistantMsg);
            state.ragIsQuerying = false;
            renderitzarRagChat();

            const reader = res.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let sseBuffer = '';
            let lastRender = 0;

            const flushRender = (force = false) => {
                const now = performance.now();
                if (force || now - lastRender > 80) {
                    lastRender = now;
                    renderitzarRagChat();
                }
            };

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });

                let sepIdx;
                while ((sepIdx = sseBuffer.indexOf('\n\n')) !== -1) {
                    const rawEvent = sseBuffer.slice(0, sepIdx);
                    sseBuffer = sseBuffer.slice(sepIdx + 2);

                    let eventType = 'message';
                    const dataLines = [];
                    rawEvent.split('\n').forEach(ln => {
                        if (ln.startsWith('event:')) eventType = ln.slice(6).trim();
                        else if (ln.startsWith('data:')) dataLines.push(ln.slice(5).trim());
                    });
                    if (dataLines.length === 0) continue;

                    let parsed = null;
                    try { parsed = JSON.parse(dataLines.join('\n')); } catch (_) { continue; }

                    if (eventType === 'metadata') {
                        assistantMsg.citations = parsed.citations || {};
                        assistantMsg.citation_details = parsed.citation_details || {};
                        assistantMsg.references = parsed.references || [];
                        assistantMsg.sources = parsed.sources || {};
                        flushRender(true);
                    } else if (eventType === 'token') {
                        assistantMsg.text += (parsed.delta || '');
                        flushRender(false);
                    } else if (eventType === 'done') {
                        if (parsed.answer !== undefined) assistantMsg.text = parsed.answer || 'Sense resposta.';
                        assistantMsg.took_ms = parsed.took_ms || assistantMsg.took_ms;
                        assistantMsg.streaming = false;
                        flushRender(true);
                    }
                }
            }
            assistantMsg.streaming = false;
            if (!assistantMsg.text) assistantMsg.text = 'Sense resposta.';
            renderitzarRagChat();
            return;
        }

        const res = await fetch('/api/rag/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Error consultant el RAG Local');

        state.ragChatMessages.push({
            role: 'assistant',
            text: data.answer || 'Sense resposta.',
            took_ms: data.took_ms || 0,
            mode: data.mode || state.ragMode,
            citations: data.citations || {},
            citation_details: data.citation_details || {},
            references: data.references || [],
            sources: data.sources || {},
        });
    } catch (err) {
        console.error('Error en consulta RAG Local:', err);
        state.ragChatMessages.push({
            role: 'assistant',
            text: `⚠️ **Error en el RAG Local:** ${err.message}`,
        });
    } finally {
        state.ragIsQuerying = false;
        renderitzarRagChat();
    }
}

function generarMarkdownPurRag(msgIndex, includeHeader = false) {
    const msg = state.ragChatMessages[msgIndex];
    if (!msg || !msg.text) return '';

    let userQuery = '';
    if (msgIndex > 0 && state.ragChatMessages[msgIndex - 1].role === 'user') {
        userQuery = state.ragChatMessages[msgIndex - 1].text.trim();
    }

    const scopeTitle = obtenirNomAbastRag();
    const textFormatted = convertirCitacionsAPandoc(msg.text, msg.citations, msg.sources, false, msg.citation_details);
    const evidenciesMd = formatarEvidenciesMarkdown(msg.references);

    let md = '';
    if (includeHeader) {
        const headerTitle = userQuery ? `### RAG Local: ${userQuery}` : `### RAG Local (${scopeTitle})`;
        md += `${headerTitle}\n\n`;
    }
    md += `${textFormatted}\n`;
    if (evidenciesMd) {
        md += `\n#### Evidències i cites del PDF:\n\n${evidenciesMd}\n`;
    }
    return md;
}

async function crearNovaNotaDesDeRag(msgIndex) {
    const msg = state.ragChatMessages[msgIndex];
    if (!msg || !msg.text) return;

    let userQuery = '';
    if (msgIndex > 0 && state.ragChatMessages[msgIndex - 1].role === 'user') {
        userQuery = state.ragChatMessages[msgIndex - 1].text.trim();
    }

    const scopeTitle = obtenirNomAbastRag();
    const defaultTitle = userQuery ? `RAG Local: ${userQuery}` : `Síntesi RAG (${scopeTitle})`;
    const titol = prompt("Títol de la nova nota de síntesi:", defaultTitle);
    if (!titol || !titol.trim()) return;

    const titolNet = titol.trim();
    const textFormatted = convertirCitacionsAPandoc(msg.text, msg.citations, msg.sources, false, msg.citation_details);
    const evidenciesMd = formatarEvidenciesMarkdown(msg.references);

    const avui = new Date().toISOString().slice(0, 10);
    let contingut = `---
title: "${titolNet.replace(/"/g, '\\"')}"
date: ${avui}
tags: ["sintesi", "rag-local"]
---

# ${titolNet}

${userQuery ? `> **Pregunta:** ${userQuery}\n\n` : ''}${textFormatted}
`;

    if (evidenciesMd) {
        contingut += `\n## Evidències i cites del PDF\n\n${evidenciesMd}\n`;
    }

    try {
        const res = await fetch('/api/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                titol: titolNet,
                tags: "sintesi rag-local",
                contingut: contingut
            })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Error creant la nota");

        const filename = data.filename;
        if (state.activeCollectionId && state.activeCollectionId !== 'unfiled' && state.activeCollectionId !== 'trash') {
            await afegirItemAColleccio(state.activeCollectionId, 'note', filename);
        }

        const notesRes = await fetch('/api/notes');
        if (notesRes.ok) {
            const notesData = await notesRes.json();
            state.notes = notesData.notes || [];
            if (el.notesCount) el.notesCount.textContent = state.notes.length;
            renderitzarLlistaNotes();
        }

        await obrirNota(filename);
        canviarPestanya('notes');
        actualitzarEstatGuardat('saved', `Nota '${titolNet}' creada`);
    } catch (err) {
        console.error("Error creant nota des de RAG Local:", err);
        alert(`Error en crear la nota: ${err.message}`);
    }
}

function renderitzarRagChat() {
    const histEl = document.getElementById('rag-chat-history');
    if (!histEl) return;
    histEl.innerHTML = '';

    if (state.ragChatMessages.length === 0 && !state.ragIsQuerying) {
        const scopeNom = obtenirNomAbastRag();
        histEl.innerHTML = `
            <div id="rag-empty-state" style="text-align: center; color: var(--text-muted); font-size: 12px; margin-top: 26px; padding: 0 10px;">
                <i class="fa-solid fa-bolt-lightning" style="font-size: 28px; color: #a855f7; opacity: 0.7; margin-bottom: 10px;"></i>
                <p style="margin: 0 0 6px 0; font-weight: 600; color: var(--text-main);">RAG Híbrid Local (${escapeHtml(scopeNom)})</p>
                <p style="font-size: 11px; margin: 0; line-height: 1.4;">
                    Motor <b>Docling + BM25 FTS5 + embeddinggemma-2</b> en NumPy float32.<br>
                    Fes clic a qualsevol cita <code>[@citekey, p. X]</code> per obrir el PDF a la pàgina exacta.
                </p>
            </div>
        `;
        return;
    }

    state.ragChatMessages.forEach((msg, idx) => {
        if (msg.role === 'user') {
            const div = document.createElement('div');
            div.className = 'nlm-msg-user';
            div.style.background = 'rgba(168, 85, 247, 0.16)';
            div.style.borderColor = 'rgba(168, 85, 247, 0.35)';
            div.textContent = msg.text;
            const badge = document.createElement('div');
            badge.style.cssText = 'font-size: 9px; opacity: 0.85; margin-top: 4px; font-style: italic; text-align: right;';
            badge.textContent = `${msg.mode === 'synthesis' ? '🧠 Síntesi IA' : '⚡ Evidències'} · ${msg.sourcesCount}/${msg.totalSources} fonts`;
            div.appendChild(badge);
            histEl.appendChild(div);
        } else {
            const div = document.createElement('div');
            div.className = 'nlm-msg-assistant';

            if (msg.took_ms || msg.streaming) {
                const metaTop = document.createElement('div');
                metaTop.style.cssText = 'display: flex; align-items: center; justify-content: space-between; font-size: 9.5px; color: var(--text-muted); margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px dashed var(--border-subtle);';
                const rightBadge = msg.streaming
                    ? `<span style="font-family: monospace; color: #a855f7; font-weight: 600;"><i class="fa-solid fa-circle-notch fa-spin"></i> generant...</span>`
                    : `<span style="font-family: monospace; color: #a855f7; font-weight: 600;">${msg.took_ms} ms</span>`;
                metaTop.innerHTML = `<span><i class="fa-solid fa-bolt-lightning" style="color: #a855f7;"></i> ${msg.mode === 'synthesis' ? 'Híbrid + Ollama' : 'Híbrid BM25 + Vector'}</span>${rightBadge}`;
                div.appendChild(metaTop);
            }

            const bodyDiv = document.createElement('div');
            bodyDiv.innerHTML = msg.text
                ? formatMarkdownNlm(msg.text, msg.citations, msg.sources, msg.citation_details)
                : '<span style="color: var(--text-muted); font-size: 11px; font-style: italic;">Sintetitzant resposta amb Ollama...</span>';
            div.appendChild(bodyDiv);

            if (msg.references && msg.references.length > 0) {
                const refsBlock = document.createElement('div');
                refsBlock.className = 'nlm-references-section';
                refsBlock.style.cssText = 'margin-top: 10px; padding: 8px 10px; background: var(--bg-subtle, rgba(255,255,255,0.03)); border-radius: 6px; border: 1px solid var(--border-subtle, rgba(255,255,255,0.08)); font-size: 11px;';

                const refsHeader = document.createElement('div');
                refsHeader.style.cssText = 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; font-weight: 600; color: var(--text-muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;';
                refsHeader.innerHTML = `<span><i class="fa-solid fa-quote-left" style="color: #a855f7; margin-right: 4px;"></i> Fragments verificats al PDF (${msg.references.length})</span>`;
                refsBlock.appendChild(refsHeader);

                const refsList = document.createElement('div');
                refsList.style.cssText = 'display: flex; flex-direction: column; gap: 6px;';

                msg.references.forEach((ref) => {
                    const refItem = document.createElement('div');
                    refItem.className = 'nlm-reference-item';
                    refItem.style.cssText = 'display: flex; flex-direction: column; gap: 3px; padding: 6px 8px; background: var(--bg-card, rgba(255,255,255,0.02)); border: 1px solid var(--border-subtle, rgba(255,255,255,0.05)); border-radius: 5px; cursor: pointer; transition: background 0.15s, border-color 0.15s;';
                    refItem.onmouseenter = () => { refItem.style.borderColor = '#a855f7'; refItem.style.background = 'rgba(168, 85, 247, 0.08)'; };
                    refItem.onmouseleave = () => { refItem.style.borderColor = 'var(--border-subtle, rgba(255,255,255,0.05))'; refItem.style.background = 'var(--bg-card, rgba(255,255,255,0.02))'; };

                    const pnum = ref.page || 1;
                    const isNoteRef = ref.source_kind === 'note' || (ref.pdf && /\.(md|org)$/i.test(ref.pdf));
                    const noteTarget = ref.note_filename || (isNoteRef ? ref.source_id : '');
                    const pdfName = ref.pdf || ref.source_id || 'Document';
                    const citekey = ref.citekey || 'ref';
                    const cleanCite = ref.cited_text ? ref.cited_text.replace(/\s+/g, ' ').trim() : '';
                    const shortCite = cleanCite.length > 180 ? cleanCite.slice(0, 180) + '...' : cleanCite;
                    const ctxLabel = ref.context ? `<div style="font-size: 9.5px; color: #a855f7; font-weight: 600;">📂 ${escapeHtml(ref.context)}</div>` : '';
                    const refIcon = isNoteRef
                        ? '<i class="fa-regular fa-note-sticky" style="color: #8b5cf6; margin-right: 4px;"></i>'
                        : '<i class="fa-solid fa-file-pdf" style="color: #ef4444; margin-right: 4px;"></i>';
                    const badgeLabel = isNoteRef ? `[@${escapeHtml(citekey)}]` : `[@${escapeHtml(citekey)}, p. ${pnum}]`;

                    refItem.innerHTML = `
                        <div style="display: flex; align-items: center; justify-content: space-between; font-size: 11px;">
                            <span style="font-family: monospace; font-weight: 600; color: #a855f7;">
                                ${refIcon}${badgeLabel}
                            </span>
                            <span style="font-size: 10px; color: var(--text-muted); display: inline-flex; align-items: center; gap: 4px;">
                                <span style="max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(isNoteRef ? (noteTarget || pdfName) : pdfName)}</span>
                                <i class="fa-solid fa-arrow-up-right-from-square" style="color: #a855f7; font-size: 9px;" title="${isNoteRef ? 'Obrir nota a l\'editor' : `Saltar a la pàgina ${pnum}`}"></i>
                            </span>
                        </div>
                        ${ctxLabel}
                        ${cleanCite ? `<div style="font-size: 10.5px; color: var(--text-muted); font-style: italic; line-height: 1.35; padding-left: 6px; border-left: 2px solid #a855f7;">«${escapeHtml(shortCite)}»</div>` : ''}
                    `;
                    refItem.onclick = () => {
                        if (isNoteRef && noteTarget) {
                            obrirNota(noteTarget, true, true);
                        } else {
                            saltarACitacioNlm(pdfName, pnum, cleanCite);
                        }
                    };
                    refsList.appendChild(refItem);
                });

                refsBlock.appendChild(refsList);
                div.appendChild(refsBlock);
            }

            // Accions: Crear nota, Inserir a la nota, Copiar
            const actionsDiv = document.createElement('div');
            actionsDiv.className = 'nlm-msg-actions';

            const btnNewNote = document.createElement('button');
            btnNewNote.className = 'nlm-action-btn';
            btnNewNote.innerHTML = '<i class="fa-solid fa-file-circle-plus" style="color: #4ade80;"></i> Crear nota';
            btnNewNote.title = 'Crear una nova nota Markdown a la biblioteca amb aquesta resposta i cites [@citekey, p. X]';
            btnNewNote.onclick = () => crearNovaNotaDesDeRag(idx);
            actionsDiv.appendChild(btnNewNote);

            const btnInsert = document.createElement('button');
            btnInsert.className = 'nlm-action-btn';
            btnInsert.innerHTML = '<i class="fa-solid fa-file-import" style="color: #a855f7;"></i> Inserir a la nota';
            btnInsert.title = 'Inserir aquesta resposta i cites [@citekey, p. X] a la nota oberta';
            btnInsert.onclick = () => {
                const snippet = generarMarkdownPurRag(idx, true);
                inserirTextAlCursor(`\n\n${snippet}\n`);
                actualitzarEstatGuardat('unsaved', 'Resposta RAG inserida');
            };
            actionsDiv.appendChild(btnInsert);

            const btnCopy = document.createElement('button');
            btnCopy.className = 'nlm-action-btn';
            btnCopy.innerHTML = '<i class="fa-solid fa-copy"></i> Copiar';
            btnCopy.onclick = async () => {
                try {
                    const textMd = generarMarkdownPurRag(idx, false);
                    await navigator.clipboard.writeText(textMd);
                    btnCopy.innerHTML = '<i class="fa-solid fa-check" style="color: #4ade80;"></i> Copiat!';
                    setTimeout(() => { btnCopy.innerHTML = '<i class="fa-solid fa-copy"></i> Copiar'; }, 1500);
                } catch (e) {
                    console.error('Error copiant resposta RAG:', e);
                }
            };
            actionsDiv.appendChild(btnCopy);

            div.appendChild(actionsDiv);
            histEl.appendChild(div);
        }
    });

    if (!histEl._hasCitationClick) {
        histEl._hasCitationClick = true;
        histEl.addEventListener('click', (e) => {
            const pageLink = e.target.closest('.nlm-page-link');
            if (pageLink && (pageLink.dataset.pdf || pageLink.dataset.note)) {
                e.preventDefault();
                e.stopPropagation();
                if (pageLink.dataset.kind === 'note' || (pageLink.dataset.note && (!pageLink.dataset.pdf || /\.(md|org)$/i.test(pageLink.dataset.pdf)))) {
                    obrirNota(pageLink.dataset.note, true, true);
                } else {
                    saltarACitacioNlm(pageLink.dataset.pdf, parseInt(pageLink.dataset.page, 10) || 1, pageLink.dataset.cited || '');
                }
                return;
            }
            const badge = e.target.closest('.nlm-citation-badge.pandoc');
            if (badge && (badge.dataset.pdf || badge.dataset.note)) {
                e.preventDefault();
                e.stopPropagation();
                if (badge.dataset.kind === 'note' && badge.dataset.note) {
                    obrirNota(badge.dataset.note, true, true);
                } else {
                    saltarACitacioNlm(badge.dataset.pdf, parseInt(badge.dataset.page, 10) || 1, badge.dataset.cited || '');
                }
            }
        });
    }

    if (state.ragIsQuerying) {
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'nlm-msg-assistant';
        loadingDiv.innerHTML = `
            <div style="display: flex; align-items: center; gap: 8px; color: #a855f7; font-size: 11px;">
                <i class="fa-solid fa-bolt fa-beat-fade"></i>
                <span>${state.ragMode === 'synthesis' ? 'Recuperant i sintetitzant amb Ollama...' : 'Cercant al RAG Híbrid Local (BM25 + embeddinggemma-2)...'}</span>
            </div>
        `;
        histEl.appendChild(loadingDiv);
    }

    histEl.scrollTop = histEl.scrollHeight;
}

// --- Auto-Resum IA i Exportació Quarto / Pandoc ---
async function resumirNotaActualAmbRag() {
    if (!state.activeNoteFilename) {
        alert('Obre una nota abans de generar el resum automàtic.');
        return;
    }
    if (!window.vditorInstance || !window.vditorReady) {
        alert("L'editor encara s'està carregant.");
        return;
    }

    const btn = document.getElementById('btn-summarize-note');
    const prevHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> Resumint...';
    }
    actualitzarEstatGuardat('saving', 'Generant resum IA amb Ollama...');

    try {
        const res = await fetch(`/api/notes/${encodeURIComponent(state.activeNoteFilename)}/summarize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Error generant el resum');

        const summaryMd = (data.markdown || '').trim();
        if (!summaryMd) throw new Error('Ollama ha retornat un resum buit.');

        const currentVal = window.vditorInstance.getValue() || '';
        let newVal = '';
        // Si la nota ja té frontmatter YAML (--- ... ---), inserir just després del frontmatter o reemplaçar secció buida
        const fmMatch = currentVal.match(/^(---\r?\n[\s\S]*?\r?\n---\r?\n*)/);
        if (fmMatch) {
            const fmPart = fmMatch[1];
            const rest = currentVal.slice(fmPart.length).trim();
            newVal = rest ? `${fmPart}${summaryMd}\n\n${rest}\n` : `${fmPart}${summaryMd}\n`;
        } else {
            newVal = currentVal.trim() ? `${summaryMd}\n\n${currentVal.trim()}\n` : `${summaryMd}\n`;
        }

        window.vditorInstance.setValue(newVal);
        state.isDirty = true;
        actualitzarEstatGuardat('unsaved', 'Resum IA inserit');
        if (typeof desarNotaActual === 'function') {
            await desarNotaActual();
        }
    } catch (err) {
        console.error('Error en Auto-Resum IA:', err);
        actualitzarEstatGuardat('error', 'Error en resum IA');
        alert(`No s'ha pogut generar el resum: ${err.message}`);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = prevHtml;
        }
    }
}

function alternarMenuExportacio(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const menu = document.getElementById('export-dropdown-menu');
    if (!menu) return;
    const isVisible = menu.style.display === 'block';
    menu.style.display = isVisible ? 'none' : 'block';
}

document.addEventListener('click', (e) => {
    const wrap = document.getElementById('export-dropdown-wrap');
    const menu = document.getElementById('export-dropdown-menu');
    if (menu && wrap && !wrap.contains(e.target)) {
        menu.style.display = 'none';
    }
});

async function exportarDocumentActual(format = 'pdf') {
    const menu = document.getElementById('export-dropdown-menu');
    if (menu) menu.style.display = 'none';

    if (!window.vditorInstance || !window.vditorReady) {
        alert("Obre una nota a l'editor abans d'exportar.");
        return;
    }

    const content = window.vditorInstance.getValue() || '';
    if (!content.trim()) {
        alert('La nota actual és buida.');
        return;
    }

    const activeNote = state.notes.find(n => n.filename === state.activeNoteFilename);
    const title = (activeNote && activeNote.title) || (state.activeNoteFilename ? state.activeNoteFilename.replace(/\.(md|qmd|org)$/i, '') : 'Document Faristol');

    actualitzarEstatGuardat('saving', `Exportant a ${format.toUpperCase()}...`);
    try {
        const res = await fetch('/api/export/document', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: state.activeNoteFilename || 'document.md',
                title: title,
                content: content,
                format: format,
            }),
        });
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Error exportant (${res.status})`);
        }

        const blob = await res.blob();
        let downloadName = `${(state.activeNoteFilename || 'document').replace(/\.(md|qmd|org)$/i, '')}.${format}`;
        const disp = res.headers.get('Content-Disposition') || '';
        const matchName = disp.match(/filename\*?=([^;]+)/i);
        if (matchName) {
            downloadName = matchName[1].replace(/^UTF-8''/i, '').replace(/["']/g, '').trim();
            try { downloadName = decodeURIComponent(downloadName); } catch (_) {}
        }

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = downloadName;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);

        actualitzarEstatGuardat('saved', `Exportat (${format.toUpperCase()})`);
    } catch (err) {
        console.error('Error exportant document:', err);
        actualitzarEstatGuardat('error', 'Error en exportar');
        alert(`Error exportant a ${format.toUpperCase()}: ${err.message}`);
    }
}

// --- Gestió del Tema (Clar per defecte / Fosc) ---
function inicialitzarTema() {
    const savedTheme = localStorage.getItem('faristol_theme');
    // Per defecte clar ('light'), llevat que l'usuari haja guardat 'dark'
    const isDark = (savedTheme === 'dark');
    aplicarTema(isDark ? 'dark' : 'light');
}

function alternarTema() {
    const isCurrentlyDark = document.documentElement.classList.contains('dark-theme') || document.body.classList.contains('dark-theme');
    aplicarTema(isCurrentlyDark ? 'light' : 'dark');
}

function aplicarTema(tema) {
    const isDark = (tema === 'dark');
    document.documentElement.classList.toggle('dark-theme', isDark);
    document.body.classList.toggle('dark-theme', isDark);
    localStorage.setItem('faristol_theme', isDark ? 'dark' : 'light');

    // Actualitzar icones i títols
    const iconHeader = document.getElementById('theme-toggle-icon');
    if (iconHeader) {
        iconHeader.className = isDark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
        if (iconHeader.parentElement) {
            iconHeader.parentElement.title = isDark ? 'Canviar a tema clar (Alt+T)' : 'Canviar a tema fosc (Alt+T)';
        }
    }
    const iconLib = document.getElementById('theme-toggle-icon-lib');
    if (iconLib) {
        iconLib.className = isDark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
        if (iconLib.parentElement) {
            iconLib.parentElement.title = isDark ? 'Canviar a tema clar (Alt+T)' : 'Canviar a tema fosc (Alt+T)';
        }
    }

    // Sincronitzar amb Vditor si està llest
    if (window.vditorInstance && window.vditorReady) {
        try {
            if (isDark) {
                window.vditorInstance.setTheme('dark', 'dark', 'github-dark');
            } else {
                window.vditorInstance.setTheme('classic', 'light', 'github');
            }
        } catch (err) {
            console.warn('Avís en canviar tema de Vditor:', err);
        }
    }

    // Sincronitzar amb el subagent Antigravity si està incrustat en l'iframe
    notificarTemaAntigravity(isDark ? 'dark' : 'light');
}

function tancarModals() {
    document.querySelectorAll('.modal-backdrop').forEach(m => m.style.display = 'none');
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function urlSegura(u) {
    if (!u || typeof u !== 'string') return '';
    const net = u.trim();
    if (/^https?:\/\//i.test(net)) {
        return escapeHtml(net);
    }
    return '';
}

// --- Gestió de la Paperera de la Biblioteca ---
async function demanarMoureNotaPaperera(filename) {
    if (!confirm(`Vols moure "${filename}" i els seus documents associats a la Paperera de la biblioteca?`)) {
        return;
    }
    try {
        const res = await fetch(`/api/notes/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        if (res.ok) {
            if (state.activeNoteFilename === filename) {
                netejarEditor();
                netejarVisorPdf();
            }
            await carregarDadesBiblioteca(true);
            actualitzarNomColleccioActiva();
            renderitzarColleccions();
            renderitzarLlistaNotes();
            renderitzarLlistaPDFs();
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Error movent a la paperera: ${err.detail || 'Error desconegut'}`);
        }
    } catch (e) {
        alert(`Error de xarxa: ${e.message}`);
    }
}

async function demanarMourePdfPaperera(filename) {
    if (!confirm(`Vols moure el document "${filename}" a la Paperera?`)) {
        return;
    }
    try {
        const res = await fetch(`/api/trash/move-doc/${encodeURIComponent(filename)}`, { method: 'POST' });
        if (res.ok) {
            if (state.activePdfFilename === filename) {
                netejarVisorPdf();
            }
            await carregarDadesBiblioteca(true);
            actualitzarNomColleccioActiva();
            renderitzarColleccions();
            renderitzarLlistaNotes();
            renderitzarLlistaPDFs();
        } else {
            alert("No s'ha pogut moure el document a la paperera.");
        }
    } catch (e) {
        alert(`Error de xarxa: ${e.message}`);
    }
}

async function restaurarElementPaperera(filename) {
    try {
        const res = await fetch(`/api/trash/restore/${encodeURIComponent(filename)}`, { method: 'POST' });
        if (res.ok) {
            await carregarDadesBiblioteca(true);
            renderitzarColleccions();
            renderitzarLlistaNotes();
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Error restaurant: ${err.detail || 'Error desconegut'}`);
        }
    } catch (e) {
        alert(`Error de xarxa: ${e.message}`);
    }
}

async function eliminarDefinitiuPaperera(filename) {
    if (!confirm(`ATENCIÓ: Esborrar definitivament "${filename}"?\nAquesta acció NO es pot desfer.`)) {
        return;
    }
    try {
        const res = await fetch(`/api/trash/permanent/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        if (res.ok) {
            await carregarDadesBiblioteca(true);
            renderitzarColleccions();
            renderitzarLlistaNotes();
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Error eliminant: ${err.detail || 'Error desconegut'}`);
        }
    } catch (e) {
        alert(`Error de xarxa: ${e.message}`);
    }
}

async function demanarBuidarPaperera() {
    if (!confirm("Vols eliminar definitivament TOTS els elements de la Paperera?\nAquesta acció NO es pot desfer.")) {
        return;
    }
    try {
        const res = await fetch('/api/trash/empty', { method: 'POST' });
        if (res.ok) {
            await carregarDadesBiblioteca(true);
            renderitzarColleccions();
            renderitzarLlistaNotes();
        } else {
            alert("Error buidant la paperera.");
        }
    } catch (e) {
        alert(`Error de xarxa: ${e.message}`);
    }
}
