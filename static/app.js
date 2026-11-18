const initialSidebarWidth = Number(localStorage.getItem('sidebar-width')) || (window.innerWidth <= 900 ? 205 : 248);
const state = { lessons: [], currentLesson: null, targetLanguage: localStorage.getItem('target-language') || 'zh', recognitionModel: localStorage.getItem('recognition-model') || 'tiny', showTranslations: true, loopSentence: false, continuous: false, currentSentenceId: null, currentWord: null, currentPhrase: null, wordRequest: 0, lastPhraseSelection: '', dictionaryRequest: 0, dialogLessonId: null, dialogMode: null, sidebarWidth: initialSidebarWidth, playbackSeen: new Set() };
let activeWordSelection = null;
let suppressWordClick = false;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const dictionaryAudioCache = new Map();
const recognitionLabels = { tiny: '快速（tiny）', base: '平衡（base）', small: '精准（small）' }; const langNames = { en: 'English', ja: '日本語', zh: '简体中文', ko: '한국어', fr: 'Français', es: 'Español' };

function escapeHtml(value) { return String(value == null ? '' : value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char])); }
function formatTime(ms) { const seconds = Math.max(0, Math.floor((Number(ms) || 0) / 1000)); return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0'); }
function showToast(message) { $('#toast-message').textContent = message; $('#toast').classList.add('show'); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => $('#toast').classList.remove('show'), 3300); }
async function api(url, options) { const response = await fetch(url, options || {}); let body = {}; try { body = await response.json(); } catch (_) {} if (!response.ok) { const error = new Error(body.detail || body.message || '请求失败（' + response.status + '）'); error.status = response.status; throw error; } return body; }
function statusText(status) { return ({ uploaded: '待识别', processing: '识别中', ready: '已准备', failed: '处理失败' })[status] || '已准备'; }

function renderSideLessons() {
  $('#lesson-count').textContent = state.lessons.length; $('#heading-count').textContent = state.lessons.length;
  $('#lesson-list').innerHTML = state.lessons.map((lesson) => '<div class="side-lesson-wrap" data-lesson-row="' + lesson.id + '"><button class="side-lesson ' + (state.currentLesson && state.currentLesson.lesson.id === lesson.id ? 'active' : '') + '" data-side-id="' + lesson.id + '"><span class="side-lesson-dot"></span><span class="side-lesson-title">' + escapeHtml(lesson.title) + '</span></button><button class="lesson-menu-toggle" aria-label="' + escapeHtml(lesson.title) + ' 的更多操作" aria-haspopup="menu" aria-expanded="false" data-menu-toggle="' + lesson.id + '">⋯</button><div class="lesson-menu" role="menu" data-menu="' + lesson.id + '"><button role="menuitem" data-lesson-action="rename" data-lesson-id="' + lesson.id + '">重命名</button><button role="menuitem" class="delete-action" data-lesson-action="delete" data-lesson-id="' + lesson.id + '">删除材料</button></div></div>').join('');
  $$('.side-lesson').forEach((button) => button.addEventListener('click', () => openLesson(button.dataset.sideId)));
  $$('.lesson-menu-toggle').forEach((button) => button.addEventListener('click', (event) => { event.stopPropagation(); toggleLessonMenu(button.dataset.menuToggle); }));
  $$('.lesson-menu [data-lesson-action]').forEach((button) => button.addEventListener('click', (event) => { event.stopPropagation(); closeLessonMenus(); openLessonDialog(button.dataset.lessonId, button.dataset.lessonAction); }));
}
function closeLessonMenus() { $$('.lesson-menu').forEach((menu) => menu.classList.remove('open')); $$('.side-lesson-wrap').forEach((row) => row.classList.remove('menu-open')); $$('.lesson-menu-toggle').forEach((button) => button.setAttribute('aria-expanded', 'false')); }
function toggleLessonMenu(id) { const menu = document.querySelector('.lesson-menu[data-menu="' + id + '"]'); const toggle = document.querySelector('.lesson-menu-toggle[data-menu-toggle="' + id + '"]'); const row = document.querySelector('.side-lesson-wrap[data-lesson-row="' + id + '"]'); const shouldOpen = menu && !menu.classList.contains('open'); closeLessonMenus(); if (shouldOpen) { menu.classList.add('open'); row.classList.add('menu-open'); toggle.setAttribute('aria-expanded', 'true'); } }
function openLessonDialog(id, mode) { const lesson = state.lessons.find((item) => item.id === id); if (!lesson) return; state.dialogLessonId = id; state.dialogMode = mode; const deleting = mode === 'delete'; $('#lesson-dialog-title').textContent = deleting ? '删除这份材料？' : '重命名材料'; $('#lesson-dialog-eyebrow').textContent = deleting ? 'REMOVE MATERIAL' : 'MATERIAL SETTINGS'; $('#lesson-dialog-copy').textContent = deleting ? '“' + lesson.title + '”及其音频、听力稿、翻译和词汇记录将从本机删除，无法恢复。' : '为这份听力材料取一个容易辨认的名字。'; $('#rename-field-label').hidden = deleting; $('#rename-input').value = lesson.title; $('#lesson-dialog-confirm').textContent = deleting ? '确认删除' : '保存名称'; $('#lesson-dialog-confirm').classList.toggle('danger-button', deleting); $('#lesson-dialog-backdrop').hidden = false; if (!deleting) { $('#rename-input').focus(); $('#rename-input').select(); } }
function closeLessonDialog() { $('#lesson-dialog-backdrop').hidden = true; state.dialogLessonId = null; state.dialogMode = null; }
async function confirmLessonAction() { const id = state.dialogLessonId; const mode = state.dialogMode; if (!id || !mode) return; try { if (mode === 'rename') { const title = $('#rename-input').value.trim(); if (!title) return showToast('材料名称不能为空'); await api('/api/lessons/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: title }) }); if (state.currentLesson && state.currentLesson.lesson.id === id) state.currentLesson.lesson.title = title; closeLessonDialog(); await loadLessons(); if (state.currentLesson && state.currentLesson.lesson.id === id) $('#lesson-title').textContent = title; showToast('材料已重命名'); } else { await api('/api/lessons/' + id, { method: 'DELETE' }); const wasOpen = state.currentLesson && state.currentLesson.lesson.id === id; if (wasOpen) { state.currentLesson = null; $('#audio-player').pause(); $('#audio-player').removeAttribute('src'); showView('library'); } closeLessonDialog(); await loadLessons(); showToast('材料及相关本地记录已删除'); } } catch (error) { showToast(error.message); } }
function renderLibrary() {
  renderSideLessons(); $('#empty-library').hidden = state.lessons.length > 0;
  $('#library-grid').innerHTML = state.lessons.map((lesson) => { const language = (lesson.source_language || 'en').toLowerCase(); const count = lesson.sentence_count || 0; return '<article class="library-card" data-lesson-id="' + lesson.id + '"><div class="card-top"><span class="card-language ' + language + '">' + (language === 'ja' ? '日本語' : 'ENGLISH') + '</span><span class="card-status"><i class="status-dot"></i>' + statusText(lesson.status) + '</span></div><h3 class="card-title">' + escapeHtml(lesson.title) + '</h3><div class="card-meta">' + (count ? count + ' 句话' : '尚未生成听力稿') + (lesson.audio_name ? ' · ' + escapeHtml(lesson.audio_name) : '') + '</div><div class="card-footer"><div class="card-progress">' + (count ? '准备开始学习' : '点击查看处理状态') + '<div class="mini-progress"><span></span></div></div><span class="card-arrow">↗</span></div></article>'; }).join('');
  $$('.library-card').forEach((card) => card.addEventListener('click', () => openLesson(card.dataset.lessonId)));
}
async function loadLessons() { try { state.lessons = await api('/api/lessons'); renderLibrary(); } catch (error) { showToast(error.message); } }
function showView(name) { if (name !== 'workspace') { state.currentLesson = null; resetLookupCards(); window.getSelection()?.removeAllRanges(); renderSideLessons(); } $$('.view').forEach((view) => view.classList.toggle('active', view.id === name + '-view')); $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.nav === (name === 'vocabulary-detail' ? 'vocabulary' : name))); $('#breadcrumb-current').textContent = name === 'vocabulary' || name === 'vocabulary-detail' ? '我的词汇' : name === 'workspace' ? '听力工作台' : '全部材料'; }
function closeLessonTitleMenu() { const menu = $('#lesson-title-menu'); if (!menu) return; menu.hidden = true; $('#lesson-more').setAttribute('aria-expanded', 'false'); }
function toggleLessonTitleMenu() { const menu = $('#lesson-title-menu'); if (!menu) return; menu.hidden = !menu.hidden; $('#lesson-more').setAttribute('aria-expanded', menu.hidden ? 'false' : 'true'); }
function sentenceTranslation(sentenceId, language) { if (!state.currentLesson) return null; const sentence = state.currentLesson.sentences.find((item) => item.id === sentenceId); const text = sentence && (sentence.edited_text || sentence.original_text); return state.currentLesson.translations.find((item) => item.sentence_id === sentenceId && item.target_language === language && item.source_text === text); }
function tokenize(text) { const japanese = /[\u3040-\u30ff\u3400-\u9fff]/u.test(text); if (japanese && window.Intl && Intl.Segmenter) return [...new Intl.Segmenter('ja', { granularity: 'word' }).segment(text)].map((part) => part.segment); return japanese ? (text.match(/[A-Za-z0-9]+|[\u3040-\u30ff\u3400-\u9fff]+|[^\s]/gu) || [text]) : text.split(/(\s+)/u).filter(Boolean); }
function wordsHtml(text, sentenceId) { return tokenize(text).map((part) => /^\s+$/u.test(part) ? part : '<span class="word" tabindex="0" data-sentence-id="' + sentenceId + '" data-word="' + escapeHtml(part) + '">' + escapeHtml(part) + '</span>').join(''); }

function renderSentences() {
  const data = state.currentLesson; if (!data) return; const sentences = data.sentences || [];
  $('#transcript-count').textContent = sentences.length; $('#sentence-total').textContent = sentences.length + ' 句话'; $('#progress-total').textContent = sentences.length;
  $('#no-sentences').hidden = sentences.length > 0 && data.lesson.status === 'ready';
  $('#sentence-list').innerHTML = sentences.map((sentence) => { const text = sentence.edited_text || sentence.original_text; const translation = sentenceTranslation(sentence.id, state.targetLanguage); return '<article class="sentence-item" data-sentence-id="' + sentence.id + '" data-start="' + sentence.start_ms + '" data-end="' + sentence.end_ms + '"><div class="sentence-index">' + String(sentence.sequence).padStart(2, '0') + '</div><div class="sentence-body"><div class="sentence-text">' + wordsHtml(text, sentence.id) + '</div><div class="sentence-translation" ' + (state.showTranslations && translation ? '' : 'hidden') + '>' + (translation ? escapeHtml(translation.translated_text) : '') + '</div></div><button class="sentence-play" type="button" title="播放这一句" aria-label="播放第 ' + sentence.sequence + ' 句">▶</button><div class="sentence-time">' + formatTime(sentence.start_ms) + '</div><div class="sentence-actions"><button class="sentence-action translate-action">' + (translation ? '重新翻译' : '翻译这句') + '</button><button class="sentence-action edit-action">编辑</button><button class="sentence-action split-action">拆分</button><button class="sentence-action merge-action">与下一句合并</button></div></article>'; }).join('');
  $$('.sentence-item').forEach(bindSentence);
}
function bindSentence(item) {
  const id = item.dataset.sentenceId;
  item.querySelector('.sentence-play').addEventListener('click', (event) => { event.stopPropagation(); playSentence(id); });
  item.querySelectorAll('.word').forEach((word) => { word.addEventListener('click', (event) => { event.stopPropagation(); if (suppressWordClick) { suppressWordClick = false; return; } const selection = window.getSelection(); if (selection && !selection.isCollapsed && selection.toString().trim()) return; clearWordHighlights(); word.classList.add('phrase-selected'); lookupWord(id, word.dataset.word); }); word.addEventListener('keydown', (event) => { if (event.key === 'Enter') { clearWordHighlights(); word.classList.add('phrase-selected'); lookupWord(id, word.dataset.word); } }); });
  item.querySelector('.translate-action').addEventListener('click', (event) => { event.stopPropagation(); translateSentence(id); });
  item.querySelector('.edit-action').addEventListener('click', (event) => { event.stopPropagation(); editSentence(item, id); });
  item.querySelector('.split-action').addEventListener('click', (event) => { event.stopPropagation(); beginSplitSentence(item, id); });
  item.querySelector('.merge-action').addEventListener('click', (event) => { event.stopPropagation(); mergeNext(id); });
}
function updatePlayerSource() { const player = $('#audio-player'); const lesson = state.currentLesson && state.currentLesson.lesson; player.pause(); player.removeAttribute('src'); player.dataset.lessonId = lesson ? lesson.id : ''; player.currentTime = 0; $('#seek-bar').value = '0'; $('#current-time').textContent = '00:00'; $('#duration-time').textContent = '00:00'; $('#play-main').innerHTML = '<span class="play-triangle">▶</span>'; $('#player-card').dataset.lessonId = lesson ? lesson.id : ''; $('#player-title').textContent = lesson ? (lesson.audio_name || lesson.title || '听力音频') : '听力音频'; if (lesson && lesson.audio_path) { player.src = '/api/lessons/' + lesson.id + '/audio'; $('#player-state').textContent = '准备播放'; } else { $('#player-state').textContent = lesson ? '此示例没有音频' : '准备播放'; } player.load(); }
function resetLookupCards() { state.wordRequest++; state.currentWord = null; state.currentPhrase = null; state.lastPhraseSelection = ''; state.dictionaryRequest++; $('#word-empty').hidden = false; $('#word-result').hidden = true; $('#word-save-button').hidden = false; $('#word-save-button').classList.remove('saved'); $('#word-save-button').setAttribute('aria-pressed', 'false'); $('#word-saved-status').textContent = '点击星标收藏'; $('#dictionary-card').hidden = false; $('#dictionary-empty').hidden = false; $('#dictionary-empty').textContent = '选中单词后，这里显示词典释义、音标和发音。'; $('#dictionary-result').hidden = true; }
function renderProcessingState(lesson) { const processing = lesson.status === 'processing'; const waiting = lesson.status === 'uploaded'; const failed = lesson.status === 'failed'; const progress = Math.max(0, Math.min(100, Number(lesson.progress) || 0)); $('#processing-title').textContent = processing ? '正在生成你的听力稿…' : failed ? '识别没有完成' : waiting ? '等待开始识别' : '听力稿已准备好'; $('#processing-copy').textContent = lesson.progress_message || (processing ? '识别会在后台继续运行。' : waiting ? '安装本地识别模型后，可以重新开始。' : ''); $('#processing-spinner').hidden = !processing; $('#processing-progress').hidden = lesson.status === 'ready'; $('#processing-progress-bar').style.width = progress + '%'; $('#processing-percent').textContent = progress + '%'; $('#retry-transcription').hidden = !(waiting || failed); $('#retry-transcription').textContent = failed ? '重新识别' : '开始识别'; const banner = $('#processing-banner'); banner.hidden = !processing; if (processing) { banner.classList.add('active'); $('#processing-banner-title').textContent = '听力稿正在重新解析'; $('#processing-banner-copy').textContent = (lesson.progress_message || '识别任务在后台进行中') + ' · 当前模式：' + (recognitionLabels[state.recognitionModel] || state.recognitionModel); $('#processing-banner-bar').style.width = progress + '%'; $('#processing-banner-percent').textContent = progress + '%'; } }
async function startTranscription(id) { try { await api('/api/lessons/' + id + '/transcribe', { method: 'POST' }); showToast('已开始识别，进度会在这里更新'); await openLesson(id); } catch (error) { showToast(error.message); if (state.currentLesson && state.currentLesson.lesson.id === id) renderProcessingState(state.currentLesson.lesson); } }
async function reparseLesson(id) { try { await api('/api/lessons/' + id + '/reparse', { method: 'POST' }); showToast('已按当前识别模式重新解析'); await openLesson(id); } catch (error) { showToast(error.message); } }
async function openLesson(id) {
  try { state.currentLesson = await api('/api/lessons/' + id); resetLookupCards(); state.playbackSeen = new Set(); state.currentSentenceId = null; const lesson = state.currentLesson.lesson; $('#lesson-title').textContent = lesson.title; const tag = $('#lesson-language-tag'); tag.textContent = lesson.source_language === 'ja' ? 'JA' : 'EN'; tag.classList.toggle('ja', lesson.source_language === 'ja'); $('#lesson-duration').textContent = lesson.audio_name ? '本地音频' : '示例听力'; $('#lesson-status').textContent = statusText(lesson.status); renderProcessingState(lesson); updatePlayerSource(); renderSentences(); updateProgress(); renderSideLessons(); showView('workspace'); if (lesson.status === 'processing') pollLesson(id); }
  catch (error) { showToast(error.message); }
}
async function pollLesson(id) { clearTimeout(pollLesson.timer); pollLesson.timer = setTimeout(async () => { try { const data = await api('/api/lessons/' + id); if (!state.currentLesson || state.currentLesson.lesson.id !== id) return; state.currentLesson = data; $('#lesson-status').textContent = statusText(data.lesson.status); renderProcessingState(data.lesson); if (data.lesson.status === 'processing') pollLesson(id); else { await loadLessons(); renderSentences(); if (data.lesson.status === 'ready') showToast('听力稿已经准备好了'); else if (data.lesson.status === 'failed') showToast(data.lesson.progress_message || '转写未完成'); } } catch (error) { showToast(error.message); } }, 1200); }
function playSentence(id, continuous) { const item = document.querySelector('.sentence-item[data-sentence-id="' + id + '"]'); if (!item) return; const player = $('#audio-player'); if (!player.src) { item.classList.add('active'); showToast('这份示例材料没有可播放的音频'); return; } state.continuous = !!continuous; state.currentSentenceId = id; $$('.sentence-item').forEach((node) => node.classList.remove('active')); item.classList.add('active'); state.playbackSeen.add(id); updateProgress(); player.currentTime = Number(item.dataset.start) / 1000; player.play().catch(() => {}); $('#player-state').textContent = '第 ' + item.querySelector('.sentence-index').textContent + ' 句'; }
async function translateSentence(id) { const button = document.querySelector('.sentence-item[data-sentence-id="' + id + '"] .translate-action'); if (button) button.textContent = '翻译中…'; try { const result = await api('/api/sentences/' + id + '/translate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_language: state.targetLanguage, force: true }) }); const index = state.currentLesson.translations.findIndex((item) => item.sentence_id === result.sentence_id && item.target_language === result.target_language && item.source_text === result.source_text); if (index >= 0) state.currentLesson.translations[index] = result; else state.currentLesson.translations.push(result); renderSentences(); showToast('已翻译为' + langNames[state.targetLanguage]); } catch (error) { if (button) button.textContent = '翻译这句'; showToast(error.message); } }
function speakDictionaryWord(button) {
  const word = button?.dataset.word || $('#dictionary-word')?.textContent || $('#vocab-detail-word')?.textContent || '';
  if (!word || !('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) {
    showToast('当前浏览器不支持发音');
    return;
  }
  const utterance = new SpeechSynthesisUtterance(word);
  const locale = button?.dataset.locale || (button?.dataset.language === 'ja' ? 'ja-JP' : 'en-US');
  utterance.lang = locale;
  const voices = window.speechSynthesis.getVoices();
  const wantedLanguage = locale.toLowerCase();
  const preferredVoice = voices.find((voice) => voice.lang.toLowerCase() === wantedLanguage);
  if (preferredVoice) utterance.voice = preferredVoice;
  utterance.rate = 0.86;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
}
function getDictionaryAudio(raw) {
  const normalized = raw.startsWith('//') ? 'https:' + raw : raw.replace(/^http:/i, 'https:');
  const url = new URL(normalized, window.location.href);
  if (!['https:', 'http:'].includes(url.protocol)) throw new Error('invalid protocol');
  if (!dictionaryAudioCache.has(url.href)) {
    const audio = new Audio();
    audio.preload = 'auto';
    audio.src = url.href;
    audio.load();
    dictionaryAudioCache.set(url.href, audio);
  }
  return dictionaryAudioCache.get(url.href);
}
function warmDictionaryAudio(items) {
  ['英式', '美式'].forEach((region) => {
    const item = items.find((entry) => entry.region === region && entry.audio);
    if (item) { try { getDictionaryAudio(item.audio); } catch (_) {} }
  });
}
function playDictionaryAudio(button) {
  const raw = String(button?.dataset.audio || '').trim();
  if (!raw) return speakDictionaryWord(button);
  try {
    const audio = getDictionaryAudio(raw);
    audio.pause();
    try { audio.currentTime = 0; } catch (_) {}
    let finished = false;
    let timeoutId;
    const cleanup = () => { clearTimeout(timeoutId); audio.removeEventListener('playing', onPlaying); audio.removeEventListener('error', fallback); };
    const onPlaying = () => { if (finished) return; finished = true; cleanup(); };
    const fallback = () => {
      if (finished) return;
      finished = true; cleanup(); audio.pause();
      try { audio.currentTime = 0; } catch (_) {}
      speakDictionaryWord(button);
    };
    audio.addEventListener('playing', onPlaying, { once: true });
    audio.addEventListener('error', fallback, { once: true });
    timeoutId = setTimeout(fallback, 1800);
    const playback = audio.play();
    if (playback && typeof playback.catch === 'function') playback.catch(fallback);
  } catch (_) { speakDictionaryWord(button); }
}
function bindDictionaryAudio(root) { (root || document).querySelectorAll('.dictionary-audio').forEach((button) => button.addEventListener('click', () => playDictionaryAudio(button))); }
function escapeRegExp(value) { return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function highlightPhrase(text, phrase, className) {
  const source = String(text || ''); const needle = String(phrase || '').trim();
  if (!source || !needle) return escapeHtml(source);
  const startsWithWord = /^[\p{L}\p{N}_]/u.test(needle);
  const endsWithWord = /[\p{L}\p{N}_]$/u.test(needle);
  const before = startsWithWord ? String.raw`(?<![\p{L}\p{N}_'-])` : '';
  const after = endsWithWord ? String.raw`(?![\p{L}\p{N}_'-])` : '';
  const regex = new RegExp(before + escapeRegExp(needle) + after, 'giu'); let output = ''; let cursor = 0; let match;
  while ((match = regex.exec(source)) !== null) { output += escapeHtml(source.slice(cursor, match.index)) + '<mark class="' + (className || 'vocab-highlight') + '">' + escapeHtml(match[0]) + '</mark>'; cursor = match.index + match[0].length; if (!match[0].length) regex.lastIndex++; }
  return output + escapeHtml(source.slice(cursor));
}
function highlightWordForms(text, word, className) {
  const forms = [String(word || '').trim()];
  const lower = forms[0].toLocaleLowerCase();
  if (lower.endsWith('ies') && forms[0].length > 4) forms.push(forms[0].slice(0, -3) + 'y');
  else if (lower.endsWith('s') && forms[0].length > 3) forms.push(forms[0].slice(0, -1));
  const source = String(text || '');
  const form = forms.find((candidate) => candidate && new RegExp('(?<![\p{L}\p{N}_\'-])' + escapeRegExp(candidate) + '(?![\p{L}\p{N}_\'-])', 'iu').test(source));
  return highlightPhrase(source, form || forms[0], className);
}
function highlightTranslation(text, phrase) {
  const candidates = String(phrase || '').split(/[；;,，/|]+/u).map((item) => item.trim()).filter(Boolean);
  const source = String(text || '');
  const candidate = candidates.find((item) => source.toLocaleLowerCase().includes(item.toLocaleLowerCase())) || candidates[0] || '';
  return highlightPhrase(source, candidate, 'vocab-highlight-translation');
}
const partOfSpeechLabels = { noun: 'n.', verb: 'v.', adjective: 'adj.', adverb: 'adv.', preposition: 'prep.', pronoun: 'pron.', conjunction: 'conj.', interjection: 'interj.', determiner: 'det.', article: 'art.', 'auxiliary verb': 'aux. v.', 'modal verb': 'modal v.', 'phrasal verb': 'phr. v.', numeral: 'num.', particle: 'part.' };
const definitionProviderLabels = { local_model: '本地模型生成', baidu_api: '百度翻译 API 生成', libretranslate: 'LibreTranslate API 生成', local_glossary: '本地词汇表释义', source_text: '原文', unavailable: '翻译暂不可用', unknown: '翻译来源未知' };
function definitionItemsHtml(data, useBrief = false) {
  const compact = useBrief && Array.isArray(data.brief_items) && data.brief_items.length > 0;
  const items = compact ? data.brief_items : (data.definition_items || (data.definitions || []).map((item) => ({ part_of_speech: item.part_of_speech, text: item.text, translation: '' })));
  if (!items.length) return '<span class="definition-unavailable">暂无释义</span>';
  return items.map((item) => {
    const pos = partOfSpeechLabels[item.part_of_speech] || item.part_of_speech || '';
    const translation = compact ? '' : (item.translation && item.translation !== item.text ? '<span class="definition-translation">' + escapeHtml(item.translation) + '</span>' : (data.target_language !== 'en' && !item.translation ? '<span class="definition-translation unavailable">暂未获取译文</span>' : ''));
    const provenance = compact && item.provider ? '<small class="definition-source">' + escapeHtml(definitionProviderLabels[item.provider] || '翻译来源未知') + '</small>' : '';
    return '<div class="definition-item"><span class="definition-pos">' + escapeHtml(pos) + '</span><span class="definition-text">' + escapeHtml(item.text || '') + '</span>' + translation + provenance + '</div>';
  }).join('');
}
function dictionaryPronunciationItemHtml(word, sourceLanguage, item, audio, locale) {
  const region = item.region || '通用';
  return '<span class="dictionary-pronunciation"><span>' + escapeHtml(region) + ' ' + escapeHtml(item.text || '暂无音标') + '</span><button class="dictionary-audio" type="button"' + (audio ? ' data-audio="' + escapeHtml(audio) + '"' : '') + ' data-word="' + escapeHtml(word) + '" data-language="' + escapeHtml(sourceLanguage || 'en') + '" data-locale="' + locale + '" aria-label="播放' + escapeHtml(region) + '发音">🔊</button></span>';
}
function dictionaryPronunciationsHtml(word, sourceLanguage, items, getAudio) {
  const phonetics = Array.isArray(items) ? items : [];
  let html = phonetics.map((item) => {
    const locale = item.region === '英式' ? 'en-GB' : item.region === '澳式' ? 'en-AU' : 'en-US';
    return dictionaryPronunciationItemHtml(word, sourceLanguage, item, getAudio(item), locale);
  }).join('');
  if (sourceLanguage === 'en' && !phonetics.some((item) => item.region === '英式')) {
    html += dictionaryPronunciationItemHtml(word, sourceLanguage, { region: '英式（系统语音）', text: '系统发音' }, '', 'en-GB');
  }
  return html;
}
function renderDictionary(data) {
  $('#dictionary-result').hidden = false; $('#dictionary-empty').hidden = true;
  $('#dictionary-word').textContent = data.word;
  $('#dictionary-meaning').innerHTML = definitionItemsHtml(data, data.target_language === 'zh');
  const phonetics = data.phonetics || [];
  $('#dictionary-pronunciations').innerHTML = dictionaryPronunciationsHtml(data.word, data.source_language, phonetics, (item) => item.audio || '');
  warmDictionaryAudio(phonetics);
  bindDictionaryAudio($('#dictionary-result'));
}
async function lookupWord(sentenceId, word) { const requestId = ++state.wordRequest; state.phraseRequest++; state.currentPhrase = null; state.currentWord = null; state.lastPhraseSelection = ''; $('#word-save-button').hidden = false; $('#dictionary-card').hidden = false; $('#word-empty').hidden = true; $('#word-result').hidden = false; $('#looked-word').textContent = word; $('#word-language').textContent = '查询中…'; $('#word-meaning').textContent = '正在获取单词释义'; $('#word-save-button').classList.remove('saved'); $('#word-save-button').setAttribute('aria-pressed', 'false'); $('#word-saved-status').textContent = '点击星标收藏'; const sentence = state.currentLesson.sentences.find((item) => item.id === sentenceId); $('#word-context').textContent = sentence ? (sentence.edited_text || sentence.original_text) : ''; try { const result = await api('/api/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sentence_id: sentenceId, word: word, target_language: state.targetLanguage, save: false }) }); if (requestId !== state.wordRequest) return; if (result.dictionary) renderDictionary(result.dictionary); else { $('#dictionary-result').hidden = true; $('#dictionary-empty').hidden = false; $('#dictionary-empty').textContent = '暂无已保存的词典释义'; } const meaning = result.meaning || '暂无当前句释义'; state.currentWord = { sentenceId: sentenceId, word: word, meaning: meaning, targetLanguage: state.targetLanguage, vocabularyId: result.id || null, saved: !!result.saved }; $('#word-language').textContent = langNames[state.targetLanguage] || state.targetLanguage; $('#word-meaning').textContent = meaning; if (state.currentWord.saved) setWordSavedState(true); } catch (error) { if (requestId !== state.wordRequest) return; $('#word-language').textContent = '暂不可用'; $('#word-meaning').textContent = error.message; } }
async function lookupPhrase(sentenceId, phrase) {
  const requestId = ++state.wordRequest;
  state.phraseRequest++;
  state.currentWord = null;
  state.currentPhrase = { sentenceId: sentenceId, phrase: phrase };
  state.dictionaryRequest++;
  $('#dictionary-card').hidden = true;
  $('#word-empty').hidden = true;
  $('#word-result').hidden = false;
  $('#looked-word').textContent = phrase;
  $('#word-language').textContent = '短语 · 查询中…';
  $('#word-meaning').textContent = '正在结合句意查询短语';
  const sentence = state.currentLesson?.sentences.find((item) => item.id === sentenceId);
  $('#word-context').textContent = sentence ? (sentence.edited_text || sentence.original_text) : '';
  $('#word-save-button').hidden = true;
  $('#word-saved-status').textContent = '短语释义';
  try {
    const result = await api('/api/phrase-lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sentence_id: sentenceId, phrase: phrase, target_language: state.targetLanguage }) });
    if (requestId !== state.wordRequest) return;
    state.currentPhrase.meaning = result.meaning;
    $('#word-language').textContent = '短语 · ' + (langNames[state.targetLanguage] || state.targetLanguage);
    $('#word-meaning').textContent = result.meaning;
    $('#word-context').textContent = result.context_text || '';
    $('#word-saved-status').textContent = result.cached ? '已从本地缓存读取' : '结合句意生成';
  } catch (error) {
    if (requestId !== state.wordRequest) return;
    state.lastPhraseSelection = '';
    $('#word-language').textContent = '暂不可用';
    $('#word-meaning').textContent = error.message;
  }
}
function selectionElement(node) { return node && (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement); }
function readSelectedPhrase() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  const startSentence = selectionElement(range.startContainer)?.closest('.sentence-item');
  const endSentence = selectionElement(range.endContainer)?.closest('.sentence-item');
  if (!startSentence || startSentence !== endSentence) return null;
  const sentenceText = startSentence.querySelector('.sentence-text');
  if (!sentenceText?.contains(range.startContainer) || !sentenceText.contains(range.endContainer)) return null;
  let phrase = selection.toString().replace(/\s+/gu, ' ').trim();
  phrase = phrase.replace(/^[\p{P}\p{S}]+|[\p{P}\p{S}]+$/gu, '').trim();
  const tokens = phrase.match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*(?:-[\p{L}\p{N}]+)*/gu) || [];
  if (tokens.length < 2 || tokens.length > 10 || phrase.length > 120) return null;
  return { sentenceId: startSentence.dataset.sentenceId, phrase: phrase };
}
function handleTextSelection() {
  window.setTimeout(() => {
    const selected = readSelectedPhrase();
    if (!selected) return;
    const key = selected.sentenceId + '|' + selected.phrase.toLocaleLowerCase();
    if (key === state.lastPhraseSelection) return;
    state.lastPhraseSelection = key;
    lookupPhrase(selected.sentenceId, selected.phrase);
  }, 0);
}
function clearWordHighlights() { $$('.sentence-text .word.phrase-selected').forEach((word) => word.classList.remove('phrase-selected')); }
function highlightWordRange(sentenceText, startIndex, endIndex) {
  const words = [...sentenceText.querySelectorAll('.word')];
  const start = Math.max(0, Math.min(startIndex, endIndex));
  const end = Math.min(words.length - 1, Math.max(startIndex, endIndex));
  words.forEach((word, index) => word.classList.toggle('phrase-selected', index >= start && index <= end));
  return words.slice(start, end + 1);
}
function wordAtPoint(sentenceText, x, y) {
  const direct = selectionElement(document.elementFromPoint(x, y))?.closest('.word');
  if (direct && sentenceText.contains(direct)) return direct;
  const bounds = sentenceText.getBoundingClientRect();
  if (x < bounds.left || x > bounds.right || y < bounds.top - 8 || y > bounds.bottom + 8) return null;
  let nearest = null;
  let nearestDistance = Infinity;
  sentenceText.querySelectorAll('.word').forEach((word) => {
    [...word.getClientRects()].forEach((rect) => {
      const dx = x < rect.left ? rect.left - x : x > rect.right ? x - rect.right : 0;
      const dy = y < rect.top ? rect.top - y : y > rect.bottom ? y - rect.bottom : 0;
      const distance = dx * dx + dy * dy;
      if (distance < nearestDistance) { nearest = word; nearestDistance = distance; }
    });
  });
  return nearestDistance <= 28 * 28 ? nearest : null;
}
function beginWordSelection(event) {
  if (event.button !== 0) return;
  const target = selectionElement(event.target);
  const sentenceText = target?.closest('.sentence-text');
  const word = sentenceText && wordAtPoint(sentenceText, event.clientX, event.clientY);
  if (!sentenceText || !word) { activeWordSelection = null; document.body.classList.remove('dragging-word-selection'); clearWordHighlights(); return; }
  const sentenceItem = sentenceText.closest('.sentence-item');
  const words = [...sentenceText.querySelectorAll('.word')];
  const startIndex = words.indexOf(word);
  if (!sentenceItem || startIndex < 0) return;
  window.getSelection()?.removeAllRanges();
  clearWordHighlights();
  document.body.classList.add('dragging-word-selection');
  word.classList.add('phrase-selected');
  activeWordSelection = { pointerId: event.pointerId, sentenceItem, sentenceText, words, startIndex, endIndex: startIndex, startX: event.clientX, startY: event.clientY, moved: false };
}
function updateWordSelection(event) {
  const drag = activeWordSelection;
  if (!drag || event.pointerId !== drag.pointerId) return;
  const word = wordAtPoint(drag.sentenceText, event.clientX, event.clientY);
  if (!word) return;
  const endIndex = drag.words.indexOf(word);
  if (endIndex < 0) return;
  if (endIndex !== drag.startIndex || Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) >= 4) drag.moved = true;
  drag.endIndex = endIndex;
  highlightWordRange(drag.sentenceText, drag.startIndex, drag.endIndex);
}
function finishWordSelection(event) {
  const drag = activeWordSelection;
  if (!drag || event.pointerId !== drag.pointerId) return;
  activeWordSelection = null;
  document.body.classList.remove('dragging-word-selection');
  if (!drag.moved) return;
  const selectedWords = highlightWordRange(drag.sentenceText, drag.startIndex, drag.endIndex);
  if (!selectedWords.length) return;
  suppressWordClick = true;
  window.setTimeout(() => { suppressWordClick = false; }, 0);
  const sentenceId = drag.sentenceItem.dataset.sentenceId;
  if (selectedWords.length === 1) { lookupWord(sentenceId, selectedWords[0].dataset.word); return; }
  const phrase = selectedWords.map((word) => word.dataset.word).join(' ').replace(/^[.,!?;:]+|[.,!?;:]+$/gu, '').trim();
  const tokens = phrase.match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*(?:-[\p{L}\p{N}]+)*/gu) || [];
  if (tokens.length > 10 || phrase.length > 120) { showToast('一次最多选择 10 个词语'); return; }
  if (tokens.length >= 2) lookupPhrase(sentenceId, phrase);
}
function cancelWordSelection(event) {
  if (activeWordSelection?.pointerId !== event.pointerId) return;
  activeWordSelection = null;
  document.body.classList.remove('dragging-word-selection');
}
function setWordSavedState(saved) { const button = $('#word-save-button'); button.classList.toggle('saved', saved); button.setAttribute('aria-pressed', saved ? 'true' : 'false'); button.title = saved ? '取消收藏' : '收藏到我的词汇'; $('#word-saved-status').textContent = saved ? '已收藏到我的词汇' : '点击星标收藏'; }
async function toggleWordSaved() { const word = state.currentWord; if (!word) { showToast('请先点击一个单词'); return; } try { if (word.saved && word.vocabularyId) { await api('/api/vocabulary/' + word.vocabularyId, { method: 'DELETE' }); word.saved = false; setWordSavedState(false); showToast('已从我的词汇移除'); } else { const result = await api('/api/vocabulary', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sentence_id: word.sentenceId, word: word.word, target_language: word.targetLanguage, meaning: word.meaning, save: true }) }); word.saved = true; word.vocabularyId = result.id; setWordSavedState(true); showToast('已收藏，正在后台生成释义和例句'); } } catch (error) { showToast(error.message); } }
function updateProgress() { const total = state.currentLesson ? state.currentLesson.sentences.length : 0; const seen = state.playbackSeen.size; $('#progress-number').textContent = seen; $('#progress-bar').style.width = (total ? Math.min(100, seen / total * 100) : 0) + '%'; }
function editSentence(item, id) { if (item.querySelector('.sentence-edit')) return; const sentence = state.currentLesson.sentences.find((entry) => entry.id === id); const body = item.querySelector('.sentence-body'); const text = sentence.edited_text || sentence.original_text; body.innerHTML = '<textarea class="sentence-edit" aria-label="编辑句子">' + escapeHtml(text) + '</textarea><div class="edit-times"><label>开始（秒）<input class="edit-start" type="number" min="0" step="0.1" value="' + (sentence.start_ms / 1000).toFixed(1) + '"></label><label>结束（秒）<input class="edit-end" type="number" min="0" step="0.1" value="' + (sentence.end_ms / 1000).toFixed(1) + '"></label></div><div class="sentence-actions" style="opacity:1;height:24px;margin-top:5px"><button class="sentence-action save-edit">保存</button><button class="sentence-action cancel-edit">取消</button></div>'; body.querySelector('.sentence-edit').focus(); body.querySelector('.save-edit').addEventListener('click', async () => { const updatedText = body.querySelector('.sentence-edit').value.trim(); const start = Math.round(Number(body.querySelector('.edit-start').value) * 1000); const end = Math.round(Number(body.querySelector('.edit-end').value) * 1000); if (!updatedText) return showToast('句子不能为空'); try { const result = await api('/api/sentences/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: updatedText, start_ms: start, end_ms: end }) }); sentence.edited_text = result.edited_text; sentence.start_ms = result.start_ms; sentence.end_ms = result.end_ms; renderSentences(); showToast('句子已保存'); } catch (error) { showToast(error.message); } }); body.querySelector('.cancel-edit').addEventListener('click', renderSentences); }
function beginSplitSentence(item, id) {
  const sentence = state.currentLesson.sentences.find((entry) => entry.id === id);
  if (!sentence) return;
  const text = sentence.edited_text || sentence.original_text;
  const body = item.querySelector('.sentence-body');
  body.innerHTML = '<div class="split-picker"><textarea class="split-text" rows="3" spellcheck="false" aria-label="选择拆分位置">' + escapeHtml(text) + '</textarea><div class="split-picker-hint">把光标放到要拆分的位置，再点击确认。</div><div class="split-picker-actions"><button type="button" class="sentence-action split-confirm">确认拆分</button><button type="button" class="sentence-action split-cancel">取消</button></div></div>';
  const editor = body.querySelector('.split-text');
  editor.focus();
  editor.setSelectionRange(Math.max(1, Math.floor(text.length / 2)), Math.max(1, Math.floor(text.length / 2)));
  body.querySelector('.split-confirm').addEventListener('click', async () => {
    const offset = editor.selectionStart;
    if (offset <= 0 || offset >= editor.value.length) return showToast('请把光标放在句子中间');
    await submitSplitSentence(id, offset);
  });
  body.querySelector('.split-cancel').addEventListener('click', renderSentences);
}
async function submitSplitSentence(id, offset) { try { await api('/api/sentences/' + id + '/split', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offset: offset }) }); await openLesson(state.currentLesson.lesson.id); showToast('已按所选位置拆分为两句'); } catch (error) { showToast(error.message); } }
async function mergeNext(id) { try { await api('/api/sentences/' + id + '/merge-next', { method: 'POST' }); await openLesson(state.currentLesson.lesson.id); showToast('已与下一句合并'); } catch (error) { showToast(error.message); } }
async function uploadFile(file) { if (!file) return; const headers = { 'Content-Type': file.type || 'application/octet-stream', 'x-file-name': encodeURIComponent(file.name), 'x-title': encodeURIComponent(file.name.replace(/\.[^.]+$/, '')), 'x-source-language': $('#source-language').value }; try { showToast('正在上传音频…'); const lesson = await api('/api/lessons', { method: 'POST', headers: headers, body: file }); await loadLessons(); await openLesson(lesson.id); await startTranscription(lesson.id); } catch (error) { showToast(error.message); } }
async function createDemo() { try { const demo = await api('/api/lessons/demo', { method: 'POST' }); await loadLessons(); await openLesson(demo.id); showToast('已打开示例课程'); } catch (error) { showToast(error.message); } }
async function openVocabularyDetail(id) {
  try {
    const item = await api('/api/vocabulary/' + id); state.currentVocabulary = item; showView('vocabulary-detail');
    $('#vocab-detail-word').textContent = item.word;
    $('#vocab-detail-lesson').textContent = item.lesson_title ? '来源：' + item.lesson_title : '';
    const status = item.generation_status || 'pending';
    if (status === 'processing' || status === 'pending') {
      $('#vocab-detail-meaning').textContent = '正在生成简明释义…';
      $('#vocab-detail-examples').innerHTML = '<p class="vocab-example-empty">后台正在生成学习例句，请稍候。</p>';
      $('#vocab-detail-pronunciation').innerHTML = '';
      clearTimeout(openVocabularyDetail.timer);
      openVocabularyDetail.timer = setTimeout(() => openVocabularyDetail(id), 1200);
      return;
    }
    if (status === 'failed') {
      $('#vocab-detail-meaning').textContent = item.generation_error || '生成失败';
      $('#vocab-detail-examples').innerHTML = '<p class="vocab-example-empty">生成失败，请稍后重试。</p>';
      return;
    }
    const meaningItems = Array.isArray(item.brief_items) && item.brief_items.length
      ? item.brief_items
      : (item.meaning ? [{ part_of_speech: item.part_of_speech || '', text: item.meaning, provider: item.generation_source === 'language-model' ? 'local_model' : 'unknown' }] : []);
    $('#vocab-detail-meaning').innerHTML = definitionItemsHtml({ brief_items: meaningItems, target_language: item.target_language }, true);
    const phonetics = item.phonetics || [];
    $('#vocab-detail-pronunciation').innerHTML = dictionaryPronunciationsHtml(
      item.word, item.source_language, phonetics, (p) => p.audio_path ? '/api/vocabulary/' + encodeURIComponent(item.id) + '/audio?region=' + encodeURIComponent(p.region || '') : '',
    ) || '<span class="definition-unavailable">暂无音标</span>';
    const examples = (item.examples || []).map((example, index) => '<p class="vocab-example"><b>' + (index + 1) + '</b><span class="vocab-example-text">' + highlightWordForms(example.example_text, item.word, 'vocab-highlight') + '</span>' + (example.translated_text ? '<span class="vocab-example-translation">' + escapeHtml(example.translated_text) + '</span>' : '<span class="vocab-example-translation unavailable">暂未获取译文</span>') + '</p>').join('');
    $('#vocab-detail-examples').innerHTML = examples || '<p class="vocab-example-empty">暂未找到例句。</p>';
    bindDictionaryAudio($('#vocab-detail-pronunciation'));
  } catch (error) { showToast(error.message); }
}
async function deleteVocabularyItem(id) { try { await api('/api/vocabulary/' + id, { method: 'DELETE' }); state.currentVocabulary = null; showToast('已删除收藏'); await renderVocabulary(); showView('vocabulary'); } catch (error) { showToast(error.message); } }
async function renderVocabulary() { const list = $('#vocab-list'); try { const items = await api('/api/vocabulary'); if (!items.length) { list.innerHTML = ''; $('#empty-vocabulary').style.display = 'block'; return; } $('#empty-vocabulary').style.display = 'none'; list.innerHTML = items.map((item) => { const meaning = ['pending', 'processing'].includes(item.generation_status) ? '正在生成释义和例句…' : item.meaning; return '<article class="vocab-item" data-vocab-id="' + escapeHtml(item.id) + '"><button class="vocab-open" type="button"><strong class="vocab-word">' + escapeHtml(item.word) + '</strong><span class="vocab-meaning">' + escapeHtml(meaning) + '<small class="vocab-context">' + escapeHtml(item.lesson_title) + '</small></span><span class="vocab-lang">' + escapeHtml(langNames[item.target_language] || item.target_language) + '</span><span class="vocab-open-hint">查看详情 →</span></button><button class="vocab-delete" type="button">删除</button></article>'; }).join(''); $$('.vocab-item').forEach((card) => { const id = card.dataset.vocabId; card.querySelector('.vocab-open').addEventListener('click', () => openVocabularyDetail(id)); card.querySelector('.vocab-delete').addEventListener('click', () => deleteVocabularyItem(id)); }); } catch (error) { showToast(error.message); } }
async function requestModelSwitch(model, behavior) {
  try {
    const result = await api('/api/recognition/model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, behavior }) });
    state.recognitionModel = model; localStorage.setItem('recognition-model', model); $('#recognition-model').value = model;
    if (result.pending_model) { showToast('当前任务结束后自动切换识别模式'); pollRecognitionState(); } else showToast(result.message || '识别模式已切换');
    return true;
  } catch (error) {
    if (error.status === 409 && behavior === 'immediate' && error.message.includes('有转写任务正在运行')) {
      $('#model-switch-copy').textContent = error.message; $('#model-switch-backdrop').hidden = false;
      $('#model-switch-backdrop').dataset.model = model; return false;
    }
    showToast(error.message); $('#recognition-model').value = state.recognitionModel; return false;
  }
}
async function pollRecognitionState() {
  clearTimeout(pollRecognitionState.timer);
  try {
    const data = await api('/api/recognition');
    if (data.switch_error) { showToast(data.switch_error); state.recognitionModel = data.selected_model; $('#recognition-model').value = data.selected_model; return; }
    if (data.pending_model || data.switching) pollRecognitionState.timer = setTimeout(pollRecognitionState, 1500);
    else if (!data.active_jobs.length) { state.recognitionModel = data.selected_model; localStorage.setItem('recognition-model', data.selected_model); $('#recognition-model').value = data.selected_model; }
  } catch (_) {}
}
async function initializeRecognitionSelector() {
  try {
    const data = await api('/api/recognition');
    data.models.forEach((item) => { const option = $('#recognition-model').querySelector('option[value="' + item.id + '"]'); option.disabled = !item.available; option.textContent = item.label + (item.available ? '' : '（未下载）'); });
    state.recognitionModel = data.pending_model || data.selected_model; $('#recognition-model').value = state.recognitionModel;
    if (data.pending_model || data.switching) pollRecognitionState();
  } catch (error) { showToast(error.message); }
}

function setupEvents() {
  setupSidebarResize();
  $('#target-language').value = state.targetLanguage; $('#target-language').addEventListener('change', (event) => { state.targetLanguage = event.target.value; localStorage.setItem('target-language', state.targetLanguage); if (state.currentLesson) renderSentences(); if (state.currentPhrase) lookupPhrase(state.currentPhrase.sentenceId, state.currentPhrase.phrase); else if (state.currentWord) lookupWord(state.currentWord.sentenceId, state.currentWord.word); showToast('翻译目标：' + langNames[state.targetLanguage]); });
  $('#recognition-model').value = state.recognitionModel; $('#recognition-model').addEventListener('change', (event) => { const model = event.target.value; requestModelSwitch(model, 'immediate'); });
  initializeRecognitionSelector();
  $('#choose-file').addEventListener('click', () => $('#audio-file').click()); $('#empty-upload').addEventListener('click', () => $('#audio-file').click()); $('#new-upload-side').addEventListener('click', () => { showView('library'); $('#audio-file').click(); }); $('#audio-file').addEventListener('change', (event) => { uploadFile(event.target.files[0]); event.target.value = ''; });
  const drop = $('#dropzone'); ['dragenter', 'dragover'].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add('dragover'); })); ['dragleave', 'drop'].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove('dragover'); })); drop.addEventListener('drop', (event) => uploadFile(event.dataTransfer.files[0]));
  $('#try-demo').addEventListener('click', createDemo); $('#refresh-lessons').addEventListener('click', loadLessons); $('#back-library').addEventListener('click', () => { showView('library'); renderLibrary(); }); $('#retry-transcription').addEventListener('click', () => { if (state.currentLesson) startTranscription(state.currentLesson.lesson.id); });
  $$('.nav-item').forEach((item) => item.addEventListener('click', () => { showView(item.dataset.nav); if (item.dataset.nav === 'vocabulary') renderVocabulary(); })); $('#back-vocabulary').addEventListener('click', () => { showView('vocabulary'); renderVocabulary(); }); $('#vocab-detail-delete').addEventListener('click', () => { if (state.currentVocabulary) deleteVocabularyItem(state.currentVocabulary.id); });
  $('#lesson-more').addEventListener('click', (event) => { event.stopPropagation(); toggleLessonTitleMenu(); }); $$('#lesson-title-menu [data-title-action]').forEach((button) => button.addEventListener('click', async (event) => { event.stopPropagation(); closeLessonTitleMenu(); const lesson = state.currentLesson && state.currentLesson.lesson; if (lesson && button.dataset.titleAction === 'reparse') await reparseLesson(lesson.id); else if (lesson) openLessonDialog(lesson.id, button.dataset.titleAction); }));
  $('#workspace-reparse').addEventListener('click', () => { if (state.currentLesson) reparseLesson(state.currentLesson.lesson.id); }); $('#toggle-translations').addEventListener('click', (event) => { state.showTranslations = !state.showTranslations; event.currentTarget.classList.toggle('active', state.showTranslations); renderSentences(); }); $('#play-all').addEventListener('click', () => { const first = state.currentLesson && state.currentLesson.sentences[0]; if (first) playSentence(first.id, true); });
  $('#speed-button').addEventListener('click', (event) => { event.stopPropagation(); const menu = $('#speed-menu'); const open = !menu.classList.contains('open'); menu.classList.toggle('open', open); event.currentTarget.setAttribute('aria-expanded', open ? 'true' : 'false'); }); $$('#speed-menu [data-speed]').forEach((option) => option.addEventListener('click', (event) => { const speed = Number(event.currentTarget.dataset.speed); $('#audio-player').playbackRate = speed; $('#speed-button').textContent = speed + '×'; $('#speed-menu').classList.remove('open'); $('#speed-button').setAttribute('aria-expanded', 'false'); })); $('#loop-button').addEventListener('click', (event) => { state.loopSentence = !state.loopSentence; event.currentTarget.classList.toggle('active', state.loopSentence); showToast(state.loopSentence ? '已开启单句循环' : '已关闭单句循环'); });
  const player = $('#audio-player'); player.addEventListener('loadedmetadata', () => { $('#duration-time').textContent = formatTime(player.duration * 1000); }); player.addEventListener('timeupdate', () => { $('#current-time').textContent = formatTime(player.currentTime * 1000); if (player.duration) $('#seek-bar').value = Math.round(player.currentTime / player.duration * 1000); const active = $$('.sentence-item').find((node) => player.currentTime * 1000 >= Number(node.dataset.start) && player.currentTime * 1000 < Number(node.dataset.end)); if (active) { $$('.sentence-item').forEach((node) => node.classList.toggle('active', node === active)); state.playbackSeen.add(active.dataset.sentenceId); updateProgress(); if (state.loopSentence && player.currentTime * 1000 >= Number(active.dataset.end) - 100) { player.currentTime = Number(active.dataset.start) / 1000; } } }); player.addEventListener('ended', () => { if (state.loopSentence) { const active = $('.sentence-item.active'); if (active) { player.currentTime = Number(active.dataset.start) / 1000; player.play(); } } }); $('#seek-bar').addEventListener('input', (event) => { if (player.duration) player.currentTime = Number(event.target.value) / 1000 * player.duration; }); $('#play-main').addEventListener('click', () => { if (!player.src) return showToast('这份示例材料没有可播放的音频'); if (player.paused) player.play(); else player.pause(); }); player.addEventListener('play', () => { $('#play-main').innerHTML = '<span class="play-triangle">Ⅱ</span>'; }); player.addEventListener('pause', () => { $('#play-main').innerHTML = '<span class="play-triangle">▶</span>'; });
  $('#help-button').addEventListener('click', () => { $('#modal-backdrop').hidden = false; }); $('#modal-close').addEventListener('click', () => { $('#modal-backdrop').hidden = true; }); $('#modal-done').addEventListener('click', () => { $('#modal-backdrop').hidden = true; }); $('#modal-backdrop').addEventListener('click', (event) => { if (event.target === $('#modal-backdrop')) $('#modal-backdrop').hidden = true; });
  document.addEventListener('click', (event) => { if (!event.target.closest('.side-lesson-wrap')) closeLessonMenus(); if (!event.target.closest('.title-line')) closeLessonTitleMenu(); });
  document.addEventListener('click', (event) => { if (!event.target.closest('.speed-control')) { $('#speed-menu').classList.remove('open'); $('#speed-button').setAttribute('aria-expanded', 'false'); } });
  $('#lesson-dialog-confirm').addEventListener('click', confirmLessonAction); $('#lesson-dialog-cancel').addEventListener('click', closeLessonDialog); $('#lesson-dialog-close').addEventListener('click', closeLessonDialog); $('#lesson-dialog-backdrop').addEventListener('click', (event) => { if (event.target === $('#lesson-dialog-backdrop')) closeLessonDialog(); });
  $$('#model-switch-backdrop [data-model-behavior]').forEach((button) => button.addEventListener('click', async () => { const backdrop = $('#model-switch-backdrop'); const behavior = button.dataset.modelBehavior; if (behavior === 'immediate') return showToast('当前有转写任务，不能立即切换'); backdrop.hidden = true; await requestModelSwitch(backdrop.dataset.model, behavior); })); $('#model-switch-close').addEventListener('click', () => { $('#model-switch-backdrop').hidden = true; $('#recognition-model').value = state.recognitionModel; }); $('#model-switch-backdrop').addEventListener('click', (event) => { if (event.target === $('#model-switch-backdrop')) { $('#model-switch-backdrop').hidden = true; $('#recognition-model').value = state.recognitionModel; } });
  $('#rename-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') confirmLessonAction(); });
  $('#word-save-button').addEventListener('click', toggleWordSaved);
  document.addEventListener('pointerdown', beginWordSelection, true);
  document.addEventListener('pointermove', updateWordSelection, true);
  document.addEventListener('pointerup', finishWordSelection, true);
  document.addEventListener('pointercancel', cancelWordSelection, true);
  document.addEventListener('keyup', (event) => { if (event.shiftKey || ['Shift', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) handleTextSelection(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') { closeLessonMenus(); if (!$('#lesson-dialog-backdrop').hidden) closeLessonDialog(); } });
}
function setSidebarWidth(width, persist) { const max = Math.min(360, window.innerWidth - 320); state.sidebarWidth = Math.max(180, Math.min(max, width)); document.documentElement.style.setProperty('--sidebar-width', state.sidebarWidth + 'px'); $('#sidebar-resize').setAttribute('aria-valuenow', String(Math.round(state.sidebarWidth))); if (persist) localStorage.setItem('sidebar-width', String(Math.round(state.sidebarWidth))); }
function setupSidebarResize() { const handle = $('#sidebar-resize'); setSidebarWidth(state.sidebarWidth, false); let startX = 0; let startWidth = state.sidebarWidth; handle.addEventListener('pointerdown', (event) => { if (window.innerWidth <= 640) return; event.preventDefault(); startX = event.clientX; startWidth = state.sidebarWidth; document.body.classList.add('resizing-sidebar'); handle.setPointerCapture(event.pointerId); }); handle.addEventListener('pointermove', (event) => { if (!handle.hasPointerCapture(event.pointerId)) return; setSidebarWidth(startWidth + event.clientX - startX, false); }); const finish = (event) => { if (handle.hasPointerCapture(event.pointerId)) { handle.releasePointerCapture(event.pointerId); document.body.classList.remove('resizing-sidebar'); localStorage.setItem('sidebar-width', String(Math.round(state.sidebarWidth))); } }; handle.addEventListener('pointerup', finish); handle.addEventListener('pointercancel', finish); handle.addEventListener('keydown', (event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setSidebarWidth(state.sidebarWidth + (event.key === 'ArrowRight' ? 12 : -12), true); } }); window.addEventListener('resize', () => { if (!localStorage.getItem('sidebar-width') && window.innerWidth > 640) setSidebarWidth(window.innerWidth <= 900 ? 205 : 248, false); }); }
setupEvents();
$('#audio-player').addEventListener('timeupdate', () => {
  const player = $('#audio-player'); const item = state.currentSentenceId && document.querySelector('.sentence-item[data-sentence-id="' + state.currentSentenceId + '"]');
  if (!item || player.paused || player.currentTime * 1000 < Number(item.dataset.end)) return;
  if (state.loopSentence) { player.currentTime = Number(item.dataset.start) / 1000; return; }
  if (state.continuous) { const next = item.nextElementSibling; if (next) { state.currentSentenceId = next.dataset.sentenceId; player.currentTime = Number(next.dataset.start) / 1000; } else { state.continuous = false; state.currentSentenceId = null; player.pause(); } return; }
  state.currentSentenceId = null; player.pause();
});
loadLessons();
