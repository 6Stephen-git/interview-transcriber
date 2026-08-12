const DEFAULT_TRANSCRIPTION_MODELS = ["small", "medium", "large-v3"];
const DEFAULT_SEMANTIC_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "qwen2.5:3b"];
const state = { settings: null, records: [], currentRecord: null };

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || payload.error || `请求失败（${response.status}）`);
  }
  return response.json();
}

function showToast(message, isError = false, duration = 3600) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.toggle("is-success", !isError && duration > 3600);
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("is-visible"), duration);
}

function taskOptions() {
  return {
    language: $("#language-select").value,
    transcription_model: $("#transcription-model").value.trim(),
    semantic_provider: $("#semantic-provider").value,
    semantic_model: $("#semantic-model").value.trim(),
  };
}

function setModelOptions(selector, models) {
  const select = $(selector);
  const selected = select.value;
  select.replaceChildren(...models.map((model) => new Option(model, model)));
  select.value = models.includes(selected) ? selected : models[0];
}

function parseModelList(selector, label) {
  const models = [...new Set($(selector).value
    .split(/\r?\n/)
    .map((model) => model.trim())
    .filter(Boolean))];
  if (!models.length) throw new Error(`${label}至少需要填写一个模型。`);
  return models;
}

function resizeModelList(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function switchView(view) {
  document.querySelectorAll(".view").forEach((element) => element.classList.remove("is-visible"));
  document.querySelectorAll(".nav-item").forEach((element) => element.classList.remove("is-active"));
  $(`#${view}-view`).classList.add("is-visible");
  document.querySelector(`[data-view="${view}"]`)?.classList.add("is-active");
  const labels = {
    queue: ["录音处理", "转录队列"],
    records: ["个人档案", "面经记录"],
    settings: ["本机配置", "本地设置"],
  };
  if (labels[view]) {
    $("#view-eyebrow").textContent = labels[view][0];
    $("#view-title").textContent = labels[view][1];
  }
  if (view === "records") loadRecords();
}

function formatTime(value) {
  if (!value) return "";
  return new Date(`${value.replace(" ", "T")}Z`).toLocaleString("zh-CN", {
    dateStyle: "medium", timeStyle: "short",
  });
}

function stageLabel(stage) {
  return {
    queued: "等待处理",
    extracting: "提取音轨",
    transcribing: "正在转录",
    organizing: "语义整理",
    completed: "已完成",
    failed: "处理失败",
    cancelled: "已停止",
  }[stage] || stage;
}

function isActiveStage(stage) {
  return ["queued", "extracting", "transcribing", "organizing"].includes(stage);
}

function taskActionButtons(task) {
  const buttons = [];
  if (isActiveStage(task.stage)) {
    buttons.push(`<button class="button button-quiet small-button" data-cancel="${task.id}">停止</button>`);
  }
  if (task.stage === "failed" || task.stage === "cancelled") {
    buttons.push(`<button class="button button-quiet small-button" data-retry="${task.id}">重试</button>`);
  }
  if (task.stage === "completed") {
    buttons.push(`<button class="button button-quiet small-button" data-open-task="${task.id}">查看</button>`);
    buttons.push(`<button class="button button-quiet small-button" data-retry="${task.id}">重新整理</button>`);
  }
  if (!isActiveStage(task.stage)) {
    buttons.push(`<button class="button button-danger small-button" data-delete-task="${task.id}">删除</button>`);
  }
  return buttons.join("");
}

function renderTasks(tasks) {
  $("#task-count").textContent = `${tasks.length} 项`;
  const container = $("#task-list");
  if (!tasks.length) {
    container.innerHTML = '<p class="empty-state">还没有任务。选择或拖入一段 OBS 录制文件开始。</p>';
    return;
  }
  container.innerHTML = tasks.map((task) => `
    <article class="task-row">
      <div class="task-main">
        <strong class="file-name">${escapeHtml(task.original_name)}</strong>
        <span class="file-meta">${escapeHtml(task.transcription_model)} · ${escapeHtml(task.semantic_model)} · ${formatTime(task.updated_at)}</span>
        ${task.error_message ? `<span class="file-meta task-error" title="${escapeAttribute(task.error_message)}">${escapeHtml(task.error_message)}</span>` : ""}
      </div>
      <div class="task-actions">
        <span class="stage ${task.stage}">${stageLabel(task.stage)}</span>
        ${taskActionButtons(task)}
      </div>
    </article>
  `).join("");
}

async function loadTasks() {
  const { tasks } = await api("/api/tasks");
  renderTasks(tasks);
}

const taskStages = new Map();
const taskNotified = new Set();
let completionPolling = false;

async function refreshTasksWithNotice() {
  const { tasks } = await api("/api/tasks");
  const newlyCompleted = [];
  tasks.forEach((task) => {
    const prev = taskStages.get(task.id);
    taskStages.set(task.id, task.stage);
    if (task.stage === "completed" && prev !== "completed" && !taskNotified.has(task.id)) {
      taskNotified.add(task.id);
      newlyCompleted.push(task);
    }
  });
  renderTasks(tasks);
  newlyCompleted.forEach((task) => {
    showToast(`任务已完成：${task.original_name || "面经"} 已生成，可在面经记录中查看。`, false, 8000);
  });
}

async function pollTaskCompletions() {
  if (completionPolling) return;
  // 没有任何运行中任务时不请求，避免无意义轮询。
  const hasActiveTasks = [...taskStages.values()].some((stage) =>
    ["queued", "extracting", "transcribing", "organizing"].includes(stage)
  );
  if (!hasActiveTasks) return;
  completionPolling = true;
  try {
    const { tasks } = await api("/api/tasks");
    tasks.forEach((task) => {
      const prev = taskStages.get(task.id);
      taskStages.set(task.id, task.stage);
      if (task.stage === "completed" && prev !== "completed" && !taskNotified.has(task.id)) {
        taskNotified.add(task.id);
        showToast(`任务已完成：${task.original_name || "面经"} 已生成，可在面经记录中查看。`, false, 8000);
      }
    });
    renderTasks(tasks);
  } finally {
    completionPolling = false;
  }
}

function renderScanResults(files) {
  const results = $("#scan-results");
  if (!files.length) {
    results.innerHTML = '<p class="empty-state">此目录中没有可导入的 OBS 录制文件。</p>';
    return;
  }
  results.innerHTML = `
    <div class="scan-row">
      <strong class="file-name">扫描到 ${files.length} 个文件</strong>
      <button class="button button-quiet small-button" id="add-selected">加入已勾选文件</button>
    </div>
    ${files.map((file, index) => `
      <label class="scan-row">
        <span class="task-main">
          <strong class="file-name">${escapeHtml(file.name)}</strong>
          <span class="file-meta">${(file.size / 1024 / 1024).toFixed(1)} MB</span>
        </span>
        <input type="checkbox" data-source="${escapeAttribute(file.path)}" ${index === 0 ? "checked" : ""} />
      </label>
    `).join("")}`;
}

async function scanDirectory() {
  try {
    const { files } = await api("/api/imports/scan");
    renderScanResults(files);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function addSelectedScans() {
  const paths = [...document.querySelectorAll("#scan-results input:checked")].map((input) => input.dataset.source);
  if (!paths.length) return showToast("请先勾选至少一个录制文件。", true);
  try {
    await Promise.all(paths.map((source_path) => api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_path, ...taskOptions() }),
    })));
    showToast(`已将 ${paths.length} 个文件加入队列。`);
    $("#scan-results").innerHTML = "";
    await loadTasks();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const options = taskOptions();
  Object.entries(options).forEach(([key, value]) => form.append(key, value));
  try {
    const { tasks } = await api("/api/imports/upload", { method: "POST", body: form });
    showToast(`已导入 ${tasks.length} 个文件。`);
    await loadTasks();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadRecords() {
  const query = $("#record-search").value.trim();
  try {
    const { records } = await api(`/api/records?q=${encodeURIComponent(query)}`);
    state.records = records;
    const container = $("#record-list");
    if (!records.length) {
      container.innerHTML = '<p class="empty-state">还没有符合内容头的面经记录。</p>';
      return;
    }
    container.innerHTML = records.map((record) => `
      <article class="record-row" data-record="${record.id}" tabindex="0">
        <div class="record-main">
          <strong class="record-title">${escapeHtml(record.content_header || "未填写内容头")}</strong>
          <span class="file-meta">${escapeHtml(record.original_name)} · 更新于 ${formatTime(record.updated_at)}</span>
        </div>
        <div class="record-actions">
          <button class="button button-danger small-button" data-delete-record="${record.id}" type="button">删除</button>
          <span aria-hidden="true">→</span>
        </div>
      </article>
    `).join("");
  } catch (error) {
    showToast(error.message, true);
  }
}

function escapeHtml(text = "") {
  return String(text).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[character]));
}

function escapeAttribute(text = "") {
  return escapeHtml(text);
}

function questionTitle(title, fallbackIndex) {
  const stripped = String(title)
    .replace(/^问题\s*(?:[0-9]+|[一二三四五六七八九十]+)?\s*[：:、.．]?\s*/, "")
    .trim();
  return stripped || `问题 ${fallbackIndex}`;
}

function isQuestionHeading(title) {
  return /^问题\s*(?:[0-9]+|[一二三四五六七八九十]+)?\s*(?:[：:、.．]|$)/.test(title.trim());
}

function previewMarkdown(markdown) {
  let html = escapeHtml(markdown);
  html = html.replace(/&lt;!--[\s\S]*?--&gt;/g, "");
  let questionIndex = 0;
  html = html.replace(/^###\s+(.+)$/gm, (match, title) => {
    if (!isQuestionHeading(title)) return `<h3>${title}</h3>`;
    const id = `question-${questionIndex++}`;
    return `<h3 id="${id}" class="question-anchor">${title}</h3>`;
  });
  html = html.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/&lt;details&gt;/g, "<details>");
  html = html.replace(/&lt;\/details&gt;/g, "</details>");
  html = html.replace(/&lt;summary&gt;/g, "<summary>");
  html = html.replace(/&lt;\/summary&gt;/g, "</summary>");
  // 保护 details 内部内容：其中的换行先换成占位符，
  // 避免后续 \n → <br> 转换在 <details> 里留下游离换行（导致优质回答块渲染异常）。
  html = html.replace(
    /(<details>\s*<summary>[\s\S]*?<\/summary>)([\s\S]*?)(<\/details>)/g,
    (match, head, body, tail) => `${head}${body.replace(/\n/g, "\u0000")}${tail}`
  );
  html = html.replace(/^&lt;h3&gt;(.+?)&lt;\/h3&gt;/gm, (match, title) => {
    if (!isQuestionHeading(title)) return match;
    const id = `question-${questionIndex++}`;
    return `<h3 id="${id}" class="question-anchor">${title}</h3>`;
  });
  html = html.replace(/\n{2,}/g, "</p><p>");
  html = html.replace(/\n/g, "<br>");
  html = html.replace(/\u0000/g, "<br>");
  return `<p>${html}</p>`
    .replace(/<p><h2>/g, "<h2>")
    .replace(/<\/h2><\/p>/g, "</h2>")
    .replace(/<p><h3>/g, "<h3>")
    .replace(/<\/h3><\/p>/g, "</h3>")
    .replace(/<p><details>/g, "<details>")
    .replace(/<\/details><\/p>/g, "</details>");
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function autoResizeDocumentFields() {
  autoResizeTextarea($("#content-header"));
}

function renderQuestionNav(contentEl) {
  const layout = $("#document-layout");
  const sidebar = $("#question-sidebar");
  const nav = $("#question-nav");
  const headings = [...contentEl.querySelectorAll("h3.question-anchor")];
  const derivedLabels = headings.map((heading, index) => questionTitle(heading.textContent, index + 1));
  // 侧边栏一律以正文标题为准，避免存储的清单过期/错位导致索引指错。
  if (!headings.length) {
    sidebar.hidden = true;
    layout.classList.remove("has-sidebar");
    nav.innerHTML = "";
    return;
  }
  sidebar.hidden = false;
  layout.classList.add("has-sidebar");
  nav.innerHTML = headings.map((heading, index) => {
    const label = derivedLabels[index];
    return `<a href="#${heading.id}" data-target="${heading.id}">${escapeHtml(`${index + 1}. ${label}`)}</a>`;
  }).join("");
}

function refreshRecordView(record) {
  const contentEl = $("#document-content");
  contentEl.innerHTML = previewMarkdown(record.markdown_content);
  normalizeDocumentDom(contentEl);
  renderQuestionNav(contentEl);
}

function inlineMarkdown(node) {
  if (!node) return "";
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  if (node.tagName === "BR") return "\n";

  const content = [...node.childNodes].map(inlineMarkdown).join("");
  return ["STRONG", "B"].includes(node.tagName) ? `**${content}**` : content;
}

function blockMarkdown(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent.trim();
  if (node.nodeType !== Node.ELEMENT_NODE) return "";

  if (node.tagName === "H2") return `## ${inlineMarkdown(node).trim()}`;
  if (node.tagName === "H3") return `### ${inlineMarkdown(node).trim()}`;
  if (node.tagName === "DETAILS") {
    const summary = node.querySelector(":scope > summary");
    const answer = [...node.childNodes]
      .filter((child) => child !== summary)
      .map(blockMarkdown)
      .filter(Boolean)
      .join("\n\n");
    return `<details>\n<summary>${inlineMarkdown(summary).trim()}</summary>${answer ? `\n${answer}` : ""}\n</details>`;
  }
  return inlineMarkdown(node).trim();
}

function documentToMarkdown() {
  const contentEl = $("#document-content");
  normalizeDocumentDom(contentEl);
  return [...contentEl.childNodes]
    .map(blockMarkdown)
    .filter(Boolean)
    .join("\n\n")
    .trim();
}

function normalizeDocumentDom(contentEl) {
  // 清理编辑与渲染产生的结构噪声：空段落、details 顶部游离 <br>、段落内孤立换行。
  contentEl.querySelectorAll("details").forEach((details) => {
    while (details.firstChild && details.firstChild.nodeName === "BR") {
      details.removeChild(details.firstChild);
    }
  });
  contentEl.querySelectorAll("p").forEach((p) => {
    if (!p.textContent.trim() && !p.querySelector("img, br")) {
      p.remove();
      return;
    }
    if (p.childNodes.length === 1 && p.firstChild.nodeName === "BR") {
      p.replaceWith(document.createElement("br"));
      return;
    }
  });
  contentEl.querySelectorAll("details > br").forEach((br) => br.remove());
  // 清理内容容器下孤立存在的 <br>（不在段落内、不是换行排版需要）
  [...contentEl.childNodes].forEach((node) => {
    if (node.nodeName === "BR") node.remove();
  });
  // 清理文档末尾的孤立 <br>（内容结束后的残留换行）
  while (contentEl.lastChild && contentEl.lastChild.nodeType === 3 && !contentEl.lastChild.textContent.trim()) {
    contentEl.removeChild(contentEl.lastChild);
  }
}

async function openRecord(recordId) {
  try {
    const record = await api(`/api/records/${recordId}`);
    state.currentRecord = record;
    $("#document-source").textContent = record.original_name;
    $("#content-header").value = record.content_header;
    refreshRecordView(record);
    autoResizeDocumentFields();
    document.querySelectorAll(".view").forEach((element) => element.classList.remove("is-visible"));
    $("#record-view").classList.add("is-visible");
    $("#view-eyebrow").textContent = "可编辑档案";
    $("#view-title").textContent = "面经文档";
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveRecord(event) {
  event.preventDefault();
  if (!state.currentRecord) return;
  const markdownContent = documentToMarkdown();
  if (!markdownContent) return showToast("面经正文不能为空。", true);
  try {
    const record = await api(`/api/records/${state.currentRecord.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content_header: $("#content-header").value,
        markdown_content: markdownContent,
      }),
    });
    state.currentRecord = record;
    refreshRecordView(record);
    autoResizeDocumentFields();
    showToast("文档已保存。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function openTaskRecord(taskId) {
  await loadRecords();
  const record = state.records.find((item) => item.task_id === taskId);
  if (record) openRecord(record.id);
  else showToast("文档尚未准备完成，请稍后刷新。", true);
}

async function cancelTask(taskId) {
  try {
    await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    showToast("已请求停止，当前片段结束后会退出。");
    await loadTasks();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function deleteTask(taskId) {
  if (!window.confirm("确定删除此队列任务？已生成的面经文档会保留。")) return;
  try {
    await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    showToast("任务已删除。");
    await Promise.all([loadTasks(), loadRecords()]);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function deleteRecord(recordId, { stayOnList = true } = {}) {
  if (!window.confirm("确定删除这篇面经？此操作不可恢复。")) return;
  try {
    await api(`/api/records/${recordId}`, { method: "DELETE" });
    showToast("面经已删除。");
    state.currentRecord = null;
    if (stayOnList) {
      await Promise.all([loadRecords(), loadTasks()]);
    } else {
      switchView("records");
      await loadTasks();
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

function applySettings(settings) {
  state.settings = settings;
  const transcriptionModels = settings.transcription_models || DEFAULT_TRANSCRIPTION_MODELS;
  const semanticModels = settings.semantic_models || DEFAULT_SEMANTIC_MODELS;
  $("#recording-directory").value = settings.recording_directory || "";
  $("#local-concurrency").value = settings.local_concurrency;
  $("#api-concurrency").value = settings.api_concurrency;
  $("#transcription-models").value = transcriptionModels.join("\n");
  $("#semantic-models").value = semanticModels.join("\n");
  resizeModelList($("#transcription-models"));
  resizeModelList($("#semantic-models"));
  setModelOptions("#transcription-model", transcriptionModels);
  setModelOptions("#semantic-model", semanticModels);
  $("#ollama-url").value = settings.ollama_url;
  $("#openai-base-url").value = settings.openai_base_url;
  $("#openai-api-key").value = settings.openai_api_key || "";
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const payload = {
      recording_directory: $("#recording-directory").value.trim(),
      local_concurrency: Number($("#local-concurrency").value),
      api_concurrency: Number($("#api-concurrency").value),
      transcription_models: parseModelList("#transcription-models", "可选转录模型"),
      semantic_models: parseModelList("#semantic-models", "可选整理模型"),
      ollama_url: $("#ollama-url").value.trim(),
      openai_base_url: $("#openai-base-url").value.trim(),
      openai_api_key: $("#openai-api-key").value.trim(),
    };
    applySettings(await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }));
    showToast("本地设置已保存。");
  } catch (error) {
    showToast(error.message, true);
  }
}

function bindEvents() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  $("#refresh-button").addEventListener("click", () => { refreshTasksWithNotice(); loadRecords(); });
  $("#scan-button").addEventListener("click", scanDirectory);
  $("#scan-results").addEventListener("click", (event) => {
    if (event.target.id === "add-selected") addSelectedScans();
  });
  $("#file-input").addEventListener("change", (event) => uploadFiles(event.target.files));
  const dropZone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault(); dropZone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault(); dropZone.classList.remove("is-dragging");
  }));
  dropZone.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));
  $("#task-list").addEventListener("click", async (event) => {
    const { retry, openTask, cancel, deleteTask: deleteTaskId } = event.target.dataset;
    if (cancel) return cancelTask(cancel);
    if (deleteTaskId) return deleteTask(deleteTaskId);
    if (retry) {
      try {
        await api(`/api/tasks/${retry}/retry`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ options: taskOptions() }),
        });
        showToast("任务已重新加入队列。"); loadTasks();
      } catch (error) { showToast(error.message, true); }
    }
    if (openTask) openTaskRecord(openTask);
  });
  $("#record-search").addEventListener("input", debounce(loadRecords, 250));
  ["#transcription-models", "#semantic-models"].forEach((selector) => {
    $(selector).addEventListener("input", (event) => resizeModelList(event.currentTarget));
  });
  $("#record-list").addEventListener("click", (event) => {
    const deleteId = event.target.dataset.deleteRecord;
    if (deleteId) {
      event.preventDefault();
      event.stopPropagation();
      return deleteRecord(deleteId);
    }
    const row = event.target.closest("[data-record]");
    if (row) openRecord(row.dataset.record);
  });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#back-to-records").addEventListener("click", () => switchView("records"));
  $("#record-form").addEventListener("submit", saveRecord);
  $("#content-header").addEventListener("input", (event) => autoResizeTextarea(event.currentTarget));
  $("#document-content").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    document.execCommand("insertLineBreak");
  });
  $("#delete-record-button").addEventListener("click", () => {
    if (state.currentRecord) deleteRecord(state.currentRecord.id, { stayOnList: false });
  });
  $("#question-nav").addEventListener("click", (event) => {
    const link = event.target.closest("[data-target]");
    if (!link) return;
    event.preventDefault();
    const target = document.getElementById(link.dataset.target);
    if (!target) return;
    const margin = parseFloat(getComputedStyle(target).scrollMarginTop) || 0;
    const idealScroll = target.getBoundingClientRect().top + window.scrollY - margin;
    const maxScroll = document.documentElement.scrollHeight - document.documentElement.clientHeight;
    // 若目标无法滚动到顶部（例如页面底部附近的最后几题），改为就近对齐，
    // 避免跳转后标题停在视口中部、看起来像跳错了位置。
    const block = idealScroll <= maxScroll ? "start" : "nearest";
    target.scrollIntoView({ behavior: "smooth", block });
    $("#question-nav").querySelectorAll("a").forEach((item) => item.classList.remove("is-active"));
    link.classList.add("is-active");
  });
  $("#question-sidebar-top").addEventListener("click", () => {
    $("#document-content").scrollIntoView({ behavior: "smooth", block: "start" });
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  $("#export-outline-button").addEventListener("click", () => {
    const headings = [...document.querySelectorAll("#document-content h3.question-anchor")];
    const lines = headings.map((heading, index) => `${index + 1}. ${questionTitle(heading.textContent, index + 1)}`);
    if (!lines.length) return showToast("当前文档中没有问题标题。", true);
    const source = $("#document-source").textContent.trim() || "面经";
    const date = new Date().toISOString().slice(0, 10);
    const text = [`# ${source} 问题清单`, "", ...lines.map((line) => `- ${line}`)].join("\n");
    downloadMarkdownFile(`${safeBaseName(source)}-问题清单-${date}.md`, text);
    showToast("问题清单已导出为 Markdown。");
  });
  $("#download-record-button").addEventListener("click", () => {
    if (!state.currentRecord) return;
    const markdown = documentToMarkdown();
    if (!markdown) return showToast("面经正文不能为空。", true);
    const header = $("#content-header").value.trim();
    const source = $("#document-source").textContent.trim() || "面经";
    const date = new Date().toISOString().slice(0, 10);
    const text = header ? [`> ${header}`, "", markdown].join("\n") : markdown;
    downloadMarkdownFile(`${safeBaseName(source)}-面经-${date}.md`, text);
    showToast("面经已下载。");
  });
}

function safeBaseName(name) {
  return name.replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]/g, "_").trim() || "面经";
}

function downloadMarkdownFile(filename, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function debounce(callback, delay) {
  let timer;
  return (...args) => { window.clearTimeout(timer); timer = window.setTimeout(() => callback(...args), delay); };
}

async function initialize() {
  bindEvents();
  try {
    await api("/api/health");
    $("#health-label").textContent = "本地服务已连接";
    const settings = await api("/api/settings");
    applySettings(settings);
    await Promise.all([loadTasks(), loadRecords()]);
    // 不再固定轮询队列；仅在存在运行中任务时轮询检测完成，完成后弹窗提示。
    window.setInterval(pollTaskCompletions, 3000);
  } catch (error) {
    $("#health-label").textContent = "本地服务不可用";
    showToast(error.message, true);
  }
}

initialize();
