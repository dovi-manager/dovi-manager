function formatTimes(root = document) {
  root.querySelectorAll("time[data-utc]").forEach((element) => {
    const value = element.dataset.utc;
    if (!value) return;
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      element.textContent = parsed.toLocaleString();
      element.title = value;
    }
  });
}

function humanSize(value) {
  let size = Number(value);
  for (const unit of ["B", "KiB", "MiB", "GiB", "TiB"]) {
    if (size < 1024 || unit === "TiB") return `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} TiB`;
}

function setSummaryValue(name, value) {
  document.querySelectorAll(`[data-summary="${name}"]`).forEach((element) => {
    element.textContent = value;
  });
}

function updateSystemState(summary) {
  document.querySelectorAll("[data-system-state]").forEach((container) => {
    container.dataset.ready = summary.readiness.ok ? "true" : "false";
    const label = container.querySelector("[data-system-label]");
    const worker = container.querySelector("[data-worker-label]");
    if (label) {
      label.textContent = summary.readiness.ok ? "System ready" : "Needs attention";
    }
    if (worker) {
      worker.textContent = `Worker ${summary.worker.running ? "online" : "offline"}`;
    }
  });
  document.querySelectorAll("[data-system-orb]").forEach((orb) => {
    orb.classList.toggle("is-ready", summary.readiness.ok);
    orb.classList.toggle("is-warning", !summary.readiness.ok);
  });
  const mobileHealth = document.querySelector("[data-mobile-health]");
  if (mobileHealth) {
    mobileHealth.setAttribute(
      "aria-label",
      summary.readiness.ok ? "System ready" : "System needs attention",
    );
  }
}

function updateSummary(summary) {
  for (const [category, count] of Object.entries(summary.candidates)) {
    setSummaryValue(`candidates-${category}`, count);
  }
  for (const [state, count] of Object.entries(summary.jobs)) {
    setSummaryValue(`jobs-${state}`, count);
  }
  setSummaryValue(
    "jobs-active",
    Number(summary.jobs.queued) + Number(summary.jobs.running),
  );
  setSummaryValue("backups-count", summary.backups.count);
  setSummaryValue("backups-size", humanSize(summary.backups.size));
  updateSystemState(summary);

  if (summary.last_scan) {
    const state = document.querySelector("[data-last-scan-state]");
    if (state) {
      state.textContent = summary.last_scan.state;
      state.className = `badge state-${summary.last_scan.state}`;
    }
    const created = document.querySelector("[data-last-scan-time]");
    if (created) {
      created.dataset.utc = summary.last_scan.created_at;
      created.textContent = summary.last_scan.created_at;
      formatTimes(created.parentElement || document);
    }
  }
}

let summaryTimer;

function scheduleSummaryPoll(delay) {
  window.clearTimeout(summaryTimer);
  if (!document.hidden) {
    summaryTimer = window.setTimeout(pollSummary, delay);
  }
}

async function pollSummary() {
  if (document.hidden) return;
  try {
    const response = await fetch("/status/summary", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) {
      scheduleSummaryPoll(30000);
      return;
    }
    const summary = await response.json();
    updateSummary(summary);
    scheduleSummaryPoll(summary.active ? 5000 : 30000);
  } catch {
    scheduleSummaryPoll(30000);
  }
}

function renderTime(container, value) {
  if (!value) {
    container.textContent = container.dataset.empty || "-";
    return;
  }
  const time = document.createElement("time");
  time.dataset.utc = value;
  time.textContent = value;
  container.replaceChildren(time);
  formatTimes(container);
}

async function pollJob(detail) {
  if (detail.dataset.active !== "true") return;
  const jobId = detail.dataset.jobId;
  try {
    const response = await fetch(`/jobs/${jobId}/status`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) return;
    const job = await response.json();
    const state = document.querySelector("#job-state");
    state.textContent = job.state;
    state.className = `badge state-${job.state}`;
    renderTime(document.querySelector("#job-started"), job.started_at);
    renderTime(document.querySelector("#job-finished"), job.finished_at);
    document.querySelector("#job-exit-code").textContent =
      job.exit_code === null ? "-" : job.exit_code;
    document.querySelector("#job-error").textContent = job.error || "-";
    document
      .querySelector("#job-error")
      .classList.toggle("error-text", Boolean(job.error));

    const log = document.querySelector("#job-log");
    const following = detail.dataset.follow !== "false";
    log.textContent = job.log_text || "No output yet.";
    if (following) log.scrollTop = log.scrollHeight;
    document
      .querySelector("#job-log-truncated")
      .classList.toggle("hidden", !job.log_truncated);

    detail.dataset.active = job.active ? "true" : "false";
    updateJobLifecycle(detail, job);
    if (job.active) window.setTimeout(() => pollJob(detail), 2000);
  } catch {
    window.setTimeout(() => pollJob(detail), 5000);
  }
}

function updateJobLifecycle(detail, job) {
  const panel = detail.querySelector(".job-state-panel");
  if (panel) {
    panel.className = `job-state-panel state-panel-${job.state}`;
  }
  const progress = detail.querySelector(".state-progress");
  progress?.classList.toggle("is-running", job.state === "running");

  const queued = detail.querySelector('[data-step="queued"]');
  const running = detail.querySelector('[data-step="running"]');
  const finished = detail.querySelector('[data-step="finished"]');
  const runningLine = detail.querySelector('[data-line="running"]');
  const finishedLine = detail.querySelector('[data-line="finished"]');

  queued?.classList.toggle(
    "is-complete",
    job.state !== "cancelled" || Boolean(job.started_at),
  );
  queued?.classList.toggle("is-current", job.state === "queued");

  running?.classList.toggle("is-current", job.state === "running");
  running?.classList.toggle(
    "is-complete",
    Boolean(job.started_at) && job.state !== "running",
  );
  runningLine?.classList.toggle("is-complete", Boolean(job.started_at));

  const terminal = !job.active;
  if (finished) {
    finished.className = `timeline-step${terminal ? ` is-complete terminal-${job.state}` : ""}`;
  }
  finishedLine?.classList.toggle("is-complete", terminal);
  const finishedLabel = detail.querySelector("[data-finished-label]");
  if (finishedLabel && terminal) {
    finishedLabel.textContent =
      job.state === "succeeded"
        ? "Completed"
        : job.state === "failed"
          ? "Failed"
          : "Cancelled";
  }
}

function setupLogControls(detail) {
  detail.dataset.follow = "true";
  const followButton = detail.querySelector("[data-log-follow]");
  followButton?.addEventListener("click", () => {
    const following = detail.dataset.follow !== "false";
    detail.dataset.follow = following ? "false" : "true";
    followButton.setAttribute("aria-pressed", following ? "false" : "true");
    followButton.title = following ? "Resume following output" : "Pause following output";
    const label = followButton.querySelector(".sr-only");
    if (label) {
      label.textContent = following
        ? "Resume following output"
        : "Pause following output";
    }
    if (!following) {
      const log = detail.querySelector("#job-log");
      log.scrollTop = log.scrollHeight;
    }
  });

  detail.querySelector("[data-copy-log]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const log = detail.querySelector("#job-log");
    try {
      await navigator.clipboard.writeText(log.textContent || "");
      button.title = "Copied";
      button.classList.add("is-copied");
      window.setTimeout(() => {
        button.title = "Copy log";
        button.classList.remove("is-copied");
      }, 1600);
    } catch {
      button.title = "Copy failed";
    }
  });
}

function setupBackupSelection() {
  const form = document.querySelector("[data-backup-form]");
  if (!form) return;
  const checkboxes = Array.from(
    form.querySelectorAll('input[name="selected"]:not(:disabled)'),
  );
  const count = form.querySelector("[data-selected-count]");
  const size = form.querySelector("[data-selected-size]");
  const review = form.querySelector("[data-review-deletions]");
  const selectAll = form.querySelector("[data-select-eligible]");
  const clear = form.querySelector("[data-clear-selection]");

  const update = () => {
    const selected = checkboxes.filter((checkbox) => checkbox.checked);
    const totalSize = selected.reduce(
      (total, checkbox) => total + Number(checkbox.dataset.size || 0),
      0,
    );
    count.textContent = `${selected.length} selected`;
    size.textContent = humanSize(totalSize);
    review.disabled = selected.length === 0;
    if (selectAll) selectAll.disabled = checkboxes.length === 0;
    if (clear) clear.disabled = checkboxes.length === 0;
  };

  checkboxes.forEach((checkbox) => checkbox.addEventListener("change", update));
  selectAll?.addEventListener("click", () => {
    checkboxes.forEach((checkbox) => {
      checkbox.checked = true;
    });
    update();
  });
  clear?.addEventListener("click", () => {
    checkboxes.forEach((checkbox) => {
      checkbox.checked = false;
    });
    update();
  });
  form.addEventListener("submit", (event) => {
    const overrideSelected = checkboxes.some(
      (checkbox) => checkbox.checked && checkbox.dataset.retentionOverride === "true",
    );
    if (
      overrideSelected &&
      !window.confirm(
        "One or more selected backups are still inside the retention period. Continue to the final deletion review?",
      )
    ) {
      event.preventDefault();
    }
  });
  update();
}

function setupSettingsSections() {
  document.querySelectorAll(".settings-section-header").forEach((header, index) => {
    const section = header.closest(".settings-section");
    if (!section || section.dataset.collapsibleReady === "true") return;
    const content = Array.from(section.children).filter((child) => child !== header);
    if (content.length === 0) return;
    const contentId = `settings-section-content-${index}`;
    const wrapper = document.createElement("div");
    wrapper.className = "settings-section-content";
    wrapper.id = contentId;
    content.forEach((child) => wrapper.appendChild(child));
    section.appendChild(wrapper);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "settings-collapse-toggle";
    button.setAttribute("aria-expanded", "true");
    button.setAttribute("aria-controls", contentId);
    button.innerHTML =
      '<span class="sr-only">Collapse section</span><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6.3 8.7a1 1 0 0 1 1.4 0L12 13l4.3-4.3a1 1 0 1 1 1.4 1.4l-5 5a1 1 0 0 1-1.4 0l-5-5a1 1 0 0 1 0-1.4Z"/></svg>';
    header.appendChild(button);
    section.dataset.collapsibleReady = "true";

    button.addEventListener("click", () => {
      const collapsed = section.classList.toggle("is-collapsed");
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      button.querySelector(".sr-only").textContent = collapsed
        ? "Expand section"
        : "Collapse section";
    });
  });
}

function setupAutomationToggle() {
  const toggle = document.querySelector("[data-automation-toggle]");
  const disclosure = document.querySelector("[data-automation-disclosure]");
  const acknowledgement = document.querySelector("[data-automation-ack]");
  const acknowledgementWrapper = document.querySelector("[data-automation-ack-wrapper]");
  if (!toggle || !disclosure || !acknowledgement) return;

  const update = () => {
    const acknowledgementRequired =
      toggle.checked && toggle.dataset.initial !== "true";
    disclosure.classList.toggle("hidden", !toggle.checked);
    acknowledgement.required = acknowledgementRequired;
    acknowledgementWrapper?.classList.toggle("hidden", !acknowledgementRequired);
    if (!acknowledgementRequired) acknowledgement.checked = false;
  };
  toggle.addEventListener("change", update);
  update();
}

function setupInspectionGate() {
  const automation = document.querySelector("[data-automation-toggle]");
  const gate = document.querySelector("[data-inspection-gate]");
  const inspection = document.querySelector("[data-mel-inspection-toggle]");
  const lockNote = document.querySelector("[data-mel-inspection-lock-note]");
  if (!automation || !gate || !inspection || !lockNote) return;

  const update = () => {
    gate.disabled = !automation.checked;
    if (!automation.checked) gate.checked = false;
    if (gate.checked) inspection.checked = true;
    inspection.disabled = gate.checked;
    lockNote.classList.toggle("hidden", !gate.checked);
  };
  automation.addEventListener("change", update);
  gate.addEventListener("change", update);
  update();
}

function setupMappingEditor() {
  const editor = document.querySelector(".mapping-editor");
  const list = editor?.querySelector("[data-mapping-list]");
  const template = editor?.querySelector("[data-mapping-template]");
  const addButton = editor?.querySelector("[data-add-mapping]");
  if (!editor || !list || !template || !addButton) return;

  addButton.addEventListener("click", () => {
    const row = template.content.firstElementChild.cloneNode(true);
    list.appendChild(row);
    row.querySelector("input")?.focus();
  });
}

function setupFileBrowser() {
  const openButton = document.querySelector("[data-file-browser-open]");
  const dialog = document.querySelector("[data-file-browser-dialog]");
  const content = dialog?.querySelector("[data-file-browser-content]");
  const closeButton = dialog?.querySelector("[data-file-browser-close]");
  const form = document.querySelector("[data-file-scan-form]");
  if (!openButton || !dialog || !content || !closeButton || !form) return;

  const rootInput = form.querySelector("[data-selected-root]");
  const pathInput = form.querySelector("[data-selected-path]");
  const summary = form.querySelector("[data-selected-file]");
  const fileName = form.querySelector("[data-selected-file-name]");
  const fileMeta = form.querySelector("[data-selected-file-meta]");
  const submit = form.querySelector("[data-file-scan-submit]");
  let currentUrl = openButton.dataset.browserUrl;

  const load = async (url) => {
    content.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {
        headers: { "X-Requested-With": "file-browser-dialog" },
      });
      if (!response.ok || response.redirected) throw new Error("Browser request failed");
      const html = await response.text();
      if (!html.includes("data-file-browser-fragment")) {
        throw new Error("Invalid browser response");
      }
      content.innerHTML = html;
      currentUrl = response.url;
      bindContent();
    } catch {
      content.innerHTML =
        '<div class="empty-state compact-empty"><div class="empty-state-content"><h3>Unable to load files</h3><p>Close the browser and use the standalone file picker.</p><a class="button secondary" href="/scans/browse">Open file picker</a></div></div>';
    } finally {
      content.removeAttribute("aria-busy");
    }
  };

  const bindContent = () => {
    content.querySelectorAll("[data-browser-nav]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        load(link.href);
      });
    });
    const browserForm = content.querySelector("[data-browser-form]");
    browserForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      load(`${browserForm.action}?${new URLSearchParams(new FormData(browserForm))}`);
    });
    content.querySelector("[data-browser-root]")?.addEventListener("change", (event) => {
      const params = new URLSearchParams({ root_id: event.target.value, fragment: "1" });
      load(`/scans/browse?${params}`);
    });
    content.querySelectorAll("[data-file-choice]").forEach((choice) => {
      choice.addEventListener("click", (event) => {
        event.preventDefault();
        rootInput.value = choice.dataset.rootId;
        pathInput.value = choice.dataset.relativePath;
        fileName.textContent = choice.dataset.relativePath.split("/").pop();
        fileMeta.textContent = `${choice.dataset.rootLabel} \u00b7 ${choice.dataset.relativePath}`;
        summary.classList.remove("is-empty");
        submit.disabled = false;
        dialog.close();
      });
    });
  };

  openButton.addEventListener("click", () => {
    dialog.showModal();
    load(currentUrl);
  });
  closeButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function setupCopyFields() {
  const copyInputValue = async (input) => {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(input.value);
        return true;
      } catch {
        // Fall back for browsers that expose Clipboard API but deny this page.
      }
    }

    input.focus();
    input.select();
    input.setSelectionRange?.(0, input.value.length);
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    }
  };

  document.querySelectorAll("[data-copy-value]").forEach((button) => {
    button.addEventListener("click", async () => {
      const input = document.querySelector(button.dataset.copyValue);
      if (!input) return;
      const original = button.innerHTML;
      button.textContent = (await copyInputValue(input)) ? "Copied" : "Selected";
      window.setTimeout(() => {
        button.innerHTML = original;
      }, 1600);
    });
  });
}

function setupSecretToggles() {
  document.querySelectorAll("[data-secret-toggle]").forEach((button) => {
    const input = document.querySelector(button.dataset.secretToggle);
    if (!input) return;
    button.addEventListener("click", () => {
      const revealing = input.type === "password";
      input.type = revealing ? "text" : "password";
      button.setAttribute("aria-pressed", revealing ? "true" : "false");
      const label = button.querySelector("[data-secret-toggle-label]");
      if (label) label.textContent = revealing ? "Hide" : "Reveal";
    });
  });
}

function setupRecursiveControls() {
  document.querySelectorAll("[data-recursive-toggle]").forEach((toggle) => {
    const form = toggle.closest("form");
    const depth = form?.querySelector("[data-recursive-depth]");
    if (!depth) return;
    const update = () => {
      depth.disabled = !toggle.checked;
    };
    toggle.addEventListener("change", update);
    update();
  });
}

function setupInfoTips() {
  document.querySelectorAll(".info-tip-trigger").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      const tip = trigger.closest(".info-tip");
      const opening = !tip.classList.contains("is-open");
      document.querySelectorAll(".info-tip.is-open").forEach((item) => {
        item.classList.remove("is-open");
      });
      tip.classList.toggle("is-open", opening);
      if (opening) trigger.focus();
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        trigger.closest(".info-tip")?.classList.remove("is-open");
      }
    });
  });
  document.addEventListener("click", () => {
    document.querySelectorAll(".info-tip.is-open").forEach((item) => {
      item.classList.remove("is-open");
    });
  });
}

function setupBackupDialogs() {
  document.querySelectorAll("[data-dialog-open]").forEach((button) => {
    const dialog = document.getElementById(button.dataset.dialogOpen);
    if (!dialog?.showModal) return;
    button.addEventListener("click", () => dialog.showModal());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });
}

function setupCompactBackupPolicy() {
  const compact = document.querySelector("[data-compact-backup-toggle]");
  const compactOnly = document.querySelector("[data-compact-only-toggle]");
  const acknowledgement = document.querySelector("[data-compact-only-ack]");
  const wrapper = document.querySelector("[data-compact-only-ack-wrapper]");
  if (!compact || !compactOnly || !acknowledgement) return;
  const update = () => {
    compactOnly.disabled = !compact.checked;
    if (!compact.checked) compactOnly.checked = false;
    const required = compactOnly.checked && compactOnly.dataset.initial !== "true";
    acknowledgement.required = required;
    wrapper?.classList.toggle("hidden", !required);
    if (!required) acknowledgement.checked = false;
  };
  compact.addEventListener("change", update);
  compactOnly.addEventListener("change", update);
  update();
}

document.addEventListener("DOMContentLoaded", () => {
  formatTimes();
  document.querySelectorAll(".dismiss-alert").forEach((button) => {
    button.addEventListener("click", () => {
      button.closest(".alert")?.classList.add("is-dismissed");
    });
  });
  const detail = document.querySelector("#job-detail");
  if (detail) {
    setupLogControls(detail);
    pollJob(detail);
  }
  setupBackupSelection();
  setupAutomationToggle();
  setupInspectionGate();
  setupMappingEditor();
  setupFileBrowser();
  setupCopyFields();
  setupSecretToggles();
  setupRecursiveControls();
  setupInfoTips();
  setupBackupDialogs();
  setupCompactBackupPolicy();
  setupSettingsSections();
  pollSummary();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(summaryTimer);
  } else {
    pollSummary();
  }
});
