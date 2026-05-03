const themeStorageKey = "task-journal:theme";
const initialTheme = localStorage.getItem(themeStorageKey) === "dark" ? "dark" : "light";

document.documentElement.dataset.theme = initialTheme;

const state = {
  projects: [],
  linkTypes: [],
  statuses: [],
  tasks: [],
  task: null,
  links: [],
  currentProjectId: null,
  filter: "current",
  search: "",
  notice: null,
  theme: initialTheme,
};

const statusNames = {
  all: "Все",
  current: "Сейчас",
  next: "Следующие",
  waiting: "Ожидают",
  done: "Готово",
  archive: "Архив",
};

const app = document.getElementById("app");

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function shortDate(value) {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || "Ошибка запроса");
  }
  return payload;
}

function notify(message, kind = "ok") {
  state.notice = { message, kind };
  render();
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => {
    state.notice = null;
    render();
  }, 2600);
}

function currentProject() {
  return state.projects.find((project) => project.id === state.currentProjectId) || null;
}

function statusLabel(key) {
  return statusNames[key] || key;
}

function projectStorageKey(suffix) {
  return `task-journal:${suffix}:${state.currentProjectId}`;
}

function themeLabel() {
  return state.theme === "dark" ? "Светлая тема" : "Темная тема";
}

function themeButtonText() {
  return state.theme === "dark" ? "Светлая" : "Темная";
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem(themeStorageKey, state.theme);
  applyTheme();
  render();
}

async function bootstrap() {
  const payload = await api("/api/bootstrap");
  state.projects = payload.projects;
  state.linkTypes = payload.linkTypes;
  state.statuses = [{ key: "all", label: "Все" }, ...payload.statuses];

  const savedProject = Number(localStorage.getItem("task-journal:last-project"));
  const firstProject = state.projects[0]?.id || null;
  state.currentProjectId = state.projects.some((project) => project.id === savedProject)
    ? savedProject
    : firstProject;

  if (state.currentProjectId) {
    state.filter = localStorage.getItem(projectStorageKey("filter")) || "current";
    await loadTasks(true);
  }
  render();
}

async function reloadBootstrap() {
  const payload = await api("/api/bootstrap");
  state.projects = payload.projects;
  state.linkTypes = payload.linkTypes;
}

async function loadTasks(selectRemembered = false) {
  if (!state.currentProjectId) return;
  const payload = await api(`/api/tasks?project_id=${state.currentProjectId}`);
  state.tasks = payload.tasks;

  if (selectRemembered) {
    const project = currentProject();
    const savedTask = Number(localStorage.getItem(projectStorageKey("task")));
    const preferred = savedTask || project?.last_task_id || null;
    let nextTask = state.tasks.find((task) => task.id === preferred);
    if (!nextTask) {
      nextTask =
        state.tasks.find((task) => task.status === "current") ||
        state.tasks.find((task) => task.status === "next") ||
        state.tasks[0] ||
        null;
    }
    if (nextTask) {
      state.filter = nextTask.status || state.filter;
      await selectTask(nextTask.id, false);
    } else {
      state.task = null;
      state.links = [];
    }
  }
}

async function selectProject(projectId) {
  state.currentProjectId = projectId;
  localStorage.setItem("task-journal:last-project", String(projectId));
  state.filter = localStorage.getItem(projectStorageKey("filter")) || "current";
  state.search = "";
  await loadTasks(true);
  render();
}

async function selectTask(taskId, shouldRender = true) {
  const payload = await api(`/api/tasks/${taskId}`);
  state.task = payload.task;
  state.links = payload.links;
  localStorage.setItem(projectStorageKey("task"), String(taskId));
  await api(`/api/projects/${state.currentProjectId}`, {
    method: "PUT",
    body: JSON.stringify({ last_task_id: taskId }),
  }).catch(() => {});
  if (shouldRender) render();
}

function filteredTasks() {
  const search = state.search.trim().toLowerCase();
  return state.tasks.filter((task) => {
    const statusOk = state.filter === "all" || task.status === state.filter;
    if (!statusOk) return false;
    if (!search) return true;
    const haystack = [
      task.title,
      task.summary,
      task.next_step,
      task.notes,
      statusLabel(task.status),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(search);
  });
}

function statusCount(status) {
  if (status === "all") return state.tasks.length;
  return state.tasks.filter((task) => task.status === status).length;
}

function render() {
  const project = currentProject();
  app.innerHTML = `
    <header class="topbar">
      <nav class="project-tabs" aria-label="Проекты">
        ${state.projects
          .map(
            (item) => `
              <button class="project-tab ${item.id === state.currentProjectId ? "active" : ""}"
                data-project-id="${item.id}" title="${escapeHtml(item.description || item.name)}">
                <span>${escapeHtml(item.name)}</span>
                <span class="project-count">${item.active_count ?? 0}</span>
              </button>
            `
          )
          .join("")}
      </nav>
      <div class="top-actions">
        <button class="theme-toggle" data-action="toggle-theme" aria-label="${themeLabel()}" aria-pressed="${state.theme === "dark"}" title="${themeLabel()}">${themeButtonText()}</button>
        <button data-action="add-project" title="Добавить проект">+ проект</button>
        <button class="primary" data-action="add-task" title="Добавить задачу">+ задача</button>
      </div>
    </header>
    <main class="layout">
      ${renderSidebar(project)}
      ${renderEditor(project)}
      ${renderLinksPanel()}
    </main>
    ${state.notice ? `<div class="notice ${state.notice.kind === "error" ? "error" : ""}">${escapeHtml(state.notice.message)}</div>` : ""}
  `;
  bindEvents();
}

function renderSidebar(project) {
  if (!project) {
    return `<aside class="sidebar"><div class="empty">Создайте первый проект.</div></aside>`;
  }
  const tasks = filteredTasks();
  return `
    <aside class="sidebar">
      <div class="panel-head">
        <h1>${escapeHtml(project.name)}</h1>
        <button class="ghost" data-action="edit-project" title="Переименовать проект">править</button>
      </div>
      <input id="search" type="search" value="${escapeHtml(state.search)}" placeholder="Поиск по задачам">
      <div class="filters">
        ${state.statuses
          .map(
            (status) => `
              <button class="filter-btn ${state.filter === status.key ? "active" : ""}" data-filter="${status.key}">
                <span>${escapeHtml(status.label)}</span>
                <span class="badge">${statusCount(status.key)}</span>
              </button>
            `
          )
          .join("")}
      </div>
      <div class="task-list">
        ${
          tasks.length
            ? tasks.map(renderTaskItem).join("")
            : `<div class="empty">Здесь пока нет задач.</div>`
        }
      </div>
      <div class="export-row">
        <button data-action="backup" title="Создать резервную копию базы">Бэкап</button>
        <button data-action="export" title="Открыть JSON-выгрузку">JSON</button>
      </div>
    </aside>
  `;
}

function renderTaskItem(task) {
  const snippet = task.next_step || task.summary || task.notes || "";
  return `
    <button class="task-item ${state.task?.id === task.id ? "active" : ""}" data-task-id="${task.id}">
      <span>
        <span class="task-title">${escapeHtml(task.title)}</span>
        <span class="task-meta">${escapeHtml(statusLabel(task.status))} · ${shortDate(task.updated_at)}</span>
      </span>
      <span class="badge">${task.link_count || 0}</span>
      ${snippet ? `<span class="task-snippet">${escapeHtml(snippet)}</span>` : ""}
    </button>
  `;
}

function renderEditor(project) {
  if (!project) {
    return `<section class="editor"><div class="empty">Нет проекта для работы.</div></section>`;
  }
  if (!state.task) {
    return `
      <section class="editor">
        <div class="empty">Выберите задачу слева или создайте новую.</div>
      </section>
    `;
  }

  return `
    <section class="editor">
      <form id="task-form" class="task-form">
        <div class="title-row">
          <div class="field">
            <label for="task-title">Название</label>
            <input id="task-title" name="title" value="${escapeHtml(state.task.title)}" autocomplete="off">
          </div>
          <div class="field">
            <label for="task-status">Статус</label>
            <select id="task-status" name="status">
              ${Object.entries(statusNames)
                .filter(([key]) => key !== "all")
                .map(
                  ([key, label]) =>
                    `<option value="${key}" ${state.task.status === key ? "selected" : ""}>${escapeHtml(label)}</option>`
                )
                .join("")}
            </select>
          </div>
        </div>
        <div class="field">
          <label for="task-summary">Коротко</label>
          <textarea id="task-summary" name="summary" placeholder="Что это за задача">${escapeHtml(state.task.summary)}</textarea>
        </div>
        <div class="field">
          <label for="task-next-step">Следующий шаг</label>
          <textarea id="task-next-step" name="next_step" placeholder="Что сделать, когда вернешься к задаче">${escapeHtml(state.task.next_step)}</textarea>
        </div>
        <div class="field">
          <label for="task-notes">Заметки</label>
          <textarea id="task-notes" name="notes" rows="11" placeholder="Ход мыслей, решения, команды, важные детали">${escapeHtml(state.task.notes)}</textarea>
        </div>
        <div class="form-actions">
          <button class="primary" type="submit">Сохранить</button>
          <button type="button" data-action="archive-task">${state.task.status === "archive" ? "Вернуть в работу" : "В архив"}</button>
          <button type="button" class="danger" data-action="delete-task">Удалить</button>
          <span class="small-muted">Создано: ${shortDate(state.task.created_at)} · обновлено: ${shortDate(state.task.updated_at)}</span>
        </div>
      </form>
    </section>
  `;
}

function renderLinksPanel() {
  if (!state.task) {
    return `<aside class="links-panel"><div class="empty">Ссылки появятся после выбора задачи.</div></aside>`;
  }
  return `
    <aside class="links-panel">
      <div class="panel-head">
        <h2>Ссылки</h2>
        <button class="ghost" data-action="add-link-type" title="Добавить тип ссылки">+ тип</button>
      </div>
      <div class="link-list">
        ${
          state.links.length
            ? state.links.map(renderLinkItem).join("")
            : `<div class="empty">Добавьте путь к коду, документу, схеме или тикету.</div>`
        }
      </div>
      <form id="link-form" class="link-form">
        <div class="two-cols">
          <input name="label" placeholder="Название: файл, проект, схема">
          <select name="type_id">
            <option value="">Без типа</option>
            ${state.linkTypes
              .map((type) => `<option value="${type.id}">${escapeHtml(type.name)}</option>`)
              .join("")}
          </select>
        </div>
        <textarea name="target" placeholder="////100.100.100.100/папка/файл.txt или https://..." rows="3"></textarea>
        <input name="notes" placeholder="Комментарий к ссылке">
        <button class="primary" type="submit">Добавить ссылку</button>
      </form>
    </aside>
  `;
}

function renderLinkItem(link) {
  const color = link.type_color || "#59636e";
  return `
    <article class="link-item">
      <div class="link-top">
        <div>
          <div class="link-name">${escapeHtml(link.label)}</div>
          <div class="type-pill">
            <span class="type-dot" style="background:${escapeHtml(color)}"></span>
            <span>${escapeHtml(link.type_name || "Без типа")}</span>
          </div>
        </div>
        <span class="small-muted">${shortDate(link.updated_at)}</span>
      </div>
      <div class="link-path">${escapeHtml(link.target)}</div>
      ${link.notes ? `<div class="small-muted">${escapeHtml(link.notes)}</div>` : ""}
      <div class="link-actions">
        <button data-open-link="${link.id}">Открыть</button>
        <button data-edit-link="${link.id}">Править</button>
        <button class="danger" data-delete-link="${link.id}">Удалить</button>
      </div>
    </article>
  `;
}

function bindEvents() {
  document.querySelectorAll("[data-project-id]").forEach((button) => {
    button.addEventListener("click", () => selectProject(Number(button.dataset.projectId)).catch(showError));
  });

  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      localStorage.setItem(projectStorageKey("filter"), state.filter);
      render();
    });
  });

  document.querySelectorAll("[data-task-id]").forEach((button) => {
    button.addEventListener("click", () => selectTask(Number(button.dataset.taskId)).catch(showError));
  });

  const search = document.getElementById("search");
  if (search) {
    search.addEventListener("input", () => {
      state.search = search.value;
      render();
      const next = document.getElementById("search");
      if (next) {
        next.focus();
        next.setSelectionRange(next.value.length, next.value.length);
      }
    });
  }

  const taskForm = document.getElementById("task-form");
  if (taskForm) {
    taskForm.addEventListener("submit", saveTask);
  }

  const linkForm = document.getElementById("link-form");
  if (linkForm) {
    linkForm.addEventListener("submit", addLink);
  }

  document.querySelectorAll("[data-open-link]").forEach((button) => {
    button.addEventListener("click", () => openLink(Number(button.dataset.openLink)).catch(showError));
  });
  document.querySelectorAll("[data-edit-link]").forEach((button) => {
    button.addEventListener("click", () => editLink(Number(button.dataset.editLink)).catch(showError));
  });
  document.querySelectorAll("[data-delete-link]").forEach((button) => {
    button.addEventListener("click", () => deleteLink(Number(button.dataset.deleteLink)).catch(showError));
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => handleAction(button.dataset.action).catch(showError));
  });
}

async function handleAction(action) {
  if (action === "toggle-theme") return toggleTheme();
  if (action === "add-project") return addProject();
  if (action === "edit-project") return editProject();
  if (action === "add-task") return addTask();
  if (action === "archive-task") return archiveTask();
  if (action === "delete-task") return deleteTask();
  if (action === "add-link-type") return addLinkType();
  if (action === "backup") return createBackup();
  if (action === "export") {
    window.open("/api/export", "_blank");
  }
}

async function addProject() {
  const name = prompt("Название проекта");
  if (!name || !name.trim()) return;
  const payload = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name: name.trim() }),
  });
  await reloadBootstrap();
  await selectProject(payload.project.id);
  notify("Проект создан");
}

async function editProject() {
  const project = currentProject();
  if (!project) return;
  const name = prompt("Название проекта", project.name);
  if (!name || !name.trim()) return;
  const description = prompt("Описание проекта", project.description || "") ?? project.description;
  await api(`/api/projects/${project.id}`, {
    method: "PUT",
    body: JSON.stringify({ name: name.trim(), description }),
  });
  await reloadBootstrap();
  render();
  notify("Проект обновлен");
}

async function addTask() {
  if (!state.currentProjectId) return;
  const title = prompt("Название задачи");
  if (!title || !title.trim()) return;
  const payload = await api("/api/tasks", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.currentProjectId,
      title: title.trim(),
      status: state.filter === "archive" || state.filter === "done" ? "next" : state.filter === "all" ? "current" : state.filter,
    }),
  });
  await reloadBootstrap();
  await loadTasks(false);
  state.filter = payload.task.status;
  await selectTask(payload.task.id, false);
  render();
  notify("Задача создана");
}

async function saveTask(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  await api(`/api/tasks/${state.task.id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
  await reloadBootstrap();
  await loadTasks(false);
  await selectTask(state.task.id, false);
  state.filter = data.status;
  localStorage.setItem(projectStorageKey("filter"), state.filter);
  render();
  notify("Задача сохранена");
}

async function archiveTask() {
  if (!state.task) return;
  const nextStatus = state.task.status === "archive" ? "current" : "archive";
  await api(`/api/tasks/${state.task.id}`, {
    method: "PUT",
    body: JSON.stringify({ status: nextStatus }),
  });
  await reloadBootstrap();
  await loadTasks(false);
  await selectTask(state.task.id, false);
  state.filter = nextStatus;
  render();
  notify(nextStatus === "archive" ? "Задача в архиве" : "Задача возвращена");
}

async function deleteTask() {
  if (!state.task) return;
  if (!confirm(`Удалить задачу «${state.task.title}»?`)) return;
  const oldId = state.task.id;
  await api(`/api/tasks/${oldId}`, { method: "DELETE" });
  await reloadBootstrap();
  await loadTasks(false);
  const nextTask = filteredTasks()[0] || state.tasks[0] || null;
  if (nextTask) {
    await selectTask(nextTask.id, false);
  } else {
    state.task = null;
    state.links = [];
  }
  render();
  notify("Задача удалена");
}

async function addLink(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.target.trim()) {
    notify("Укажите путь или ссылку", "error");
    return;
  }
  await api("/api/links", {
    method: "POST",
    body: JSON.stringify({
      ...data,
      task_id: state.task.id,
      type_id: data.type_id || null,
    }),
  });
  form.reset();
  await loadTasks(false);
  await selectTask(state.task.id, false);
  render();
  notify("Ссылка добавлена");
}

async function openLink(linkId) {
  await api("/api/open-link", {
    method: "POST",
    body: JSON.stringify({ link_id: linkId }),
  });
  notify("Открываю ссылку");
}

async function editLink(linkId) {
  const link = state.links.find((item) => item.id === linkId);
  if (!link) return;
  const label = prompt("Название ссылки", link.label);
  if (!label || !label.trim()) return;
  const target = prompt("Путь или URL", link.target);
  if (!target || !target.trim()) return;
  const notes = prompt("Комментарий", link.notes || "") ?? link.notes;
  await api(`/api/links/${linkId}`, {
    method: "PUT",
    body: JSON.stringify({ label: label.trim(), target: target.trim(), notes }),
  });
  await loadTasks(false);
  await selectTask(state.task.id, false);
  render();
  notify("Ссылка обновлена");
}

async function deleteLink(linkId) {
  const link = state.links.find((item) => item.id === linkId);
  if (!confirm(`Удалить ссылку «${link?.label || ""}»?`)) return;
  await api(`/api/links/${linkId}`, { method: "DELETE" });
  await loadTasks(false);
  await selectTask(state.task.id, false);
  render();
  notify("Ссылка удалена");
}

async function addLinkType() {
  const name = prompt("Название нового типа ссылки");
  if (!name || !name.trim()) return;
  const color = prompt("Цвет в формате #RRGGBB", "#59636e") || "#59636e";
  await api("/api/link-types", {
    method: "POST",
    body: JSON.stringify({ name: name.trim(), color }),
  });
  await reloadBootstrap();
  render();
  notify("Тип ссылки добавлен");
}

async function createBackup() {
  const payload = await api("/api/backup", { method: "POST", body: "{}" });
  notify(`Бэкап создан: ${payload.path}`);
}

function showError(error) {
  console.error(error);
  notify(error.message || "Ошибка", "error");
}

bootstrap().catch(showError);
