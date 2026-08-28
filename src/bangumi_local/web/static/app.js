"use strict";

// Local, dependency-free helper for future progressively enhanced POST forms.
window.bldFetch = function bldFetch(url, options) {
  const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const next = Object.assign({}, options || {});
  next.headers = Object.assign({}, next.headers || {}, {"X-CSRF-Token": token});
  return fetch(url, next);
};

function initializeTagPicker(root) {
  const select = root.querySelector("[data-tag-select]");
  const input = root.querySelector("[data-tag-input]");
  const add = root.querySelector("[data-tag-add]");
  const chips = root.querySelector("[data-tag-chips]");
  const enhancement = root.querySelector(".tag-picker-enhancement");
  const fallback = root.querySelector(".tag-picker-fallback");
  if (!select || !input || !add || !chips || !enhancement || !fallback) return;

  function render() {
    chips.replaceChildren();
    for (const option of select.options) {
      if (!option.selected) continue;
      const chip = document.createElement("span");
      chip.className = "tag-picker-chip";
      chip.append(document.createTextNode(option.value));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `移除 ${option.value}`);
      remove.addEventListener("click", function removeTag() {
        option.selected = false;
        render();
      });
      chip.append(remove);
      chips.append(chip);
    }
  }

  function addTag() {
    const requested = input.value.trim();
    if (!requested) return;
    const option = Array.from(select.options).find(item => item.value === requested);
    if (!option) {
      input.setCustomValidity("请选择列表中已有的个人 Tag。");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    option.selected = !option.selected;
    input.value = "";
    render();
  }

  add.addEventListener("click", addTag);
  input.addEventListener("input", () => input.setCustomValidity(""));
  input.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addTag();
  });
  fallback.hidden = true;
  enhancement.hidden = false;
  render();
}

for (const picker of document.querySelectorAll("[data-tag-picker]")) {
  initializeTagPicker(picker);
}

function steamRulesPayload(form) {
  return Array.from(form.querySelectorAll("[data-rule-row]")).map(row => ({
    match: row.querySelector("[data-rule-match]").value,
    pattern: row.querySelector("[data-rule-pattern]").value.trim(),
    status: row.querySelector("[data-rule-status]").value,
    case_sensitive: row.querySelector("[data-rule-case-sensitive]").checked
  }));
}

function initializeSteamRulesForm(form) {
  const mode = form.querySelector("[data-rule-mode]");
  const editor = form.querySelector("[data-rule-editor]");
  const summary = form.querySelector("[data-saved-rule-summary]");
  const rows = form.querySelector("[data-rule-rows]");
  const add = form.querySelector("[data-rule-add]");
  const first = rows?.querySelector("[data-rule-row]");
  if (!editor || !rows || !add || !first) return;
  const prototype = first.cloneNode(true);

  function bindRemove(row) {
    row.querySelector("[data-rule-remove]")?.addEventListener("click", () => row.remove());
  }
  for (const row of rows.querySelectorAll("[data-rule-row]")) bindRemove(row);
  add.addEventListener("click", () => {
    const row = prototype.cloneNode(true);
    row.querySelector("[data-rule-match]").value = "contains";
    row.querySelector("[data-rule-pattern]").value = "";
    row.querySelector("[data-rule-status]").value = "done";
    row.querySelector("[data-rule-case-sensitive]").checked = true;
    bindRemove(row);
    rows.append(row);
  });
  function toggle() {
    if (!mode || !summary) {
      editor.hidden = false;
      return;
    }
    const custom = mode.value === "custom";
    editor.hidden = !custom;
    summary.hidden = custom;
  }
  mode?.addEventListener("change", toggle);
  toggle();
}

for (const form of document.querySelectorAll("[data-steam-rules-form]")) {
  initializeSteamRulesForm(form);
}

document.addEventListener("submit", async function submitSecureForm(event) {
  const form = event.target.closest("form[data-secure-post]");
  if (!form) return;
  event.preventDefault();
  if (form.matches("[data-steam-rules-form]")) {
    const target = form.querySelector("[data-rules-json]");
    if (target) target.value = JSON.stringify(steamRulesPayload(form));
  }
  const response = await window.bldFetch(form.action, {
    method: (form.method || "post").toUpperCase(),
    body: new FormData(form),
    credentials: "same-origin"
  });
  if (response.redirected) {
    window.location.assign(response.url);
    return;
  }
  const html = await response.text();
  document.open();
  document.write(html);
  document.close();
});

function jsonFormValue(element) {
  const kind = element.dataset.type || "string";
  if (kind === "boolean") return element.checked || element.value === "true";
  if (kind === "number") return element.value === "" ? null : Number(element.value);
  if (kind === "csv-number") return element.value.split(",").map(v => v.trim()).filter(Boolean).map(Number);
  if (kind === "csv-string") return element.value.split(",").map(v => v.trim()).filter(Boolean);
  return element.value;
}

document.addEventListener("submit", async function submitJsonForm(event) {
  const form = event.target.closest("form[data-json-post]");
  if (!form) return;
  event.preventDefault();
  const payload = {};
  for (const element of form.elements) {
    if (!element.name || element.disabled) continue;
    if ((element.type === "checkbox" || element.type === "radio") && !element.checked) {
      if (element.dataset.type === "boolean") payload[element.name] = false;
      continue;
    }
    const value = jsonFormValue(element);
    if (element.dataset.array === "true") {
      if (!Array.isArray(payload[element.name])) payload[element.name] = [];
      payload[element.name].push(value);
    } else {
      payload[element.name] = value;
    }
  }
  const submitter = event.submitter;
  if (submitter) submitter.disabled = true;
  try {
    const response = await window.bldFetch(form.action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
      credentials: "same-origin"
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    if (body.job_id) return window.location.assign(`/jobs/${body.job_id}`);
    if (body.session_id && form.dataset.successPrefix) {
      return window.location.assign(`${form.dataset.successPrefix}${body.session_id}`);
    }
    if (form.dataset.successUrl) return window.location.assign(form.dataset.successUrl);
    if (form.dataset.reload === "true") return window.location.reload();
    window.alert(JSON.stringify(body, null, 2));
  } catch (error) {
    window.alert(`请求失败：${String(error)}`);
    if (submitter) submitter.disabled = false;
  }
});

for (const selector of document.querySelectorAll("[data-rating-action-selector]")) {
  const panels = Array.from(document.querySelectorAll("[data-rating-action-panel]"));
  const refresh = () => {
    const selected = selector.querySelector("input:checked")?.value || "rate";
    for (const panel of panels) panel.hidden = panel.dataset.ratingActionPanel !== selected;
  };
  selector.addEventListener("change", refresh);
  refresh();
}

for (const toggle of document.querySelectorAll("[data-public-comment-toggle]")) {
  const form = toggle.closest("form");
  const fields = Array.from(form?.querySelectorAll("[data-public-comment-fields]") || []);
  const refresh = () => {
    for (const field of fields) {
      field.hidden = !toggle.checked;
      for (const control of field.querySelectorAll("input, textarea")) {
        control.disabled = !toggle.checked;
      }
    }
  };
  toggle.addEventListener("change", refresh);
  refresh();
}

for (const form of document.querySelectorAll('form[action="/discovery/sessions/browse"]')) {
  form.addEventListener("submit", event => {
    const year = form.querySelector('[name="year"]')?.value.trim();
    const platform = form.querySelector('[name="platform"]')?.value.trim();
    if (year || platform) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    window.alert("Bangumi Browse 必须至少填写年份或平台。");
  }, true);
}
