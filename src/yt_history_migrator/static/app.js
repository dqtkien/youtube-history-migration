"use strict";

(() => {
  const state = {
    csrfToken: "",
    currentStep: 1,
    takeoutLoaded: false,
    authenticated: false,
    loginWindowOpen: false,
    authPollTimer: null,
    migrationPollTimer: null,
  };

  const els = {
    navSteps: {
      1: document.getElementById("nav-step-1"),
      2: document.getElementById("nav-step-2"),
      3: document.getElementById("nav-step-3"),
    },
    panels: {
      1: document.getElementById("step-1"),
      2: document.getElementById("step-2"),
      3: document.getElementById("step-3"),
    },
    headings: {
      1: document.getElementById("step-1-heading"),
      2: document.getElementById("step-2-heading"),
      3: document.getElementById("step-3-heading"),
    },
    takeoutLoadedBanner: document.getElementById("takeout-loaded-banner"),
    loadedFilename: document.getElementById("loaded-filename"),
    loadedSummaryText: document.getElementById("loaded-summary-text"),
    btnChangeFile: document.getElementById("btn-change-file"),
    uploadBox: document.getElementById("upload-box"),
    dropzone: document.getElementById("dropzone"),
    takeoutInput: document.getElementById("takeout-input"),
    uploadProgressWrap: document.getElementById("upload-progress-wrap"),
    uploadProgressLabel: document.getElementById("upload-progress-label"),
    uploadProgress: document.getElementById("upload-progress"),
    uploadError: document.getElementById("upload-error"),
    step1Form: document.getElementById("step-1-form"),
    step1Error: document.getElementById("step-1-error"),
    includeMusic: document.getElementById("opt-include-music"),
    excludeShorts: document.getElementById("opt-exclude-shorts"),
    scopeMeta: {
      test5: document.getElementById("scope-meta-test5"),
      2026: document.getElementById("scope-meta-2026"),
      2024: document.getElementById("scope-meta-2024"),
      all: document.getElementById("scope-meta-all"),
    },
    authConnectedCard: document.getElementById("auth-connected-card"),
    authSetupCard: document.getElementById("auth-setup-card"),
    authAccountLabel: document.getElementById("auth-account-label"),
    btnSwitchAccount: document.getElementById("btn-switch-account"),
    btnOpenLogin: document.getElementById("btn-open-login"),
    btnConfirmLogin: document.getElementById("btn-confirm-login"),
    authWaitingBar: document.getElementById("auth-waiting-bar"),
    authStatusMessage: document.getElementById("auth-status-message"),
    authError: document.getElementById("auth-error"),
    btnBack1: document.getElementById("btn-back-1"),
    btnToStep3: document.getElementById("btn-to-step-3"),
    step3Summary: document.getElementById("step-3-summary"),
    btnStartImport: document.getElementById("btn-start-import"),
    btnStopImport: document.getElementById("btn-stop-import"),
    importProgressLabel: document.getElementById("import-progress-label"),
    importEta: document.getElementById("import-eta"),
    importProgress: document.getElementById("import-progress"),
    migrationStatusAnnouncement: document.getElementById("migration-status-announcement"),
    metricWatched: document.getElementById("metric-watched"),
    metricUnavailable: document.getElementById("metric-unavailable"),
    metricFailed: document.getElementById("metric-failed"),
    metricRemaining: document.getElementById("metric-remaining"),
    test5CompleteBanner: document.getElementById("test5-complete-banner"),
    btnRun2024: document.getElementById("btn-run-2024"),
    btnRunAll: document.getElementById("btn-run-all"),
    authExpiredBanner: document.getElementById("auth-expired-banner"),
    authExpiredText: document.getElementById("auth-expired-text"),
    btnReauthStep2: document.getElementById("btn-reauth-step2"),
    recentEventsList: document.getElementById("recent-events-list"),
    btnBack2: document.getElementById("btn-back-2"),
  };

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    }
    if (minutes > 0) {
      return `${minutes}m ${secs}s`;
    }
    return `${secs}s`;
  }

  function showError(element, message) {
    if (!message) {
      element.textContent = "";
      element.hidden = true;
      return;
    }
    element.textContent = message;
    element.hidden = false;
  }

  function getSelectedScope() {
    const checked = document.querySelector('input[name="scope"]:checked');
    return checked ? checked.value : "test5";
  }

  function setSelectedScope(scope) {
    const radio = document.querySelector(`input[name="scope"][value="${scope}"]`);
    if (radio) {
      radio.checked = true;
    }
  }

  function getSelectedBrowser() {
    const checked = document.querySelector('input[name="browser"]:checked');
    return checked ? checked.value : "chrome";
  }

  function getSelectedSpeed() {
    const checked = document.querySelector('input[name="speed"]:checked');
    return checked ? checked.value : "fast";
  }

  function setSelectedSpeed(speed) {
    const radio = document.querySelector(`input[name="speed"][value="${speed}"]`);
    if (radio) {
      radio.checked = true;
    }
  }

  async function apiPost(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  async function apiGet(url) {
    const response = await fetch(url, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  function goToStep(stepNumber) {
    state.currentStep = stepNumber;
    for (const num of [1, 2, 3]) {
      const active = num === stepNumber;
      els.panels[num].hidden = !active;
      els.navSteps[num].classList.toggle("is-active", active);
      els.navSteps[num].classList.toggle("is-done", num < stepNumber);
      if (active) {
        els.navSteps[num].setAttribute("aria-current", "step");
      } else {
        els.navSteps[num].removeAttribute("aria-current");
      }
    }
    els.headings[stepNumber].focus();
    if (stepNumber === 3) {
      refreshStep3Summary();
    }
  }

  function renderTakeoutSummary(takeout) {
    state.takeoutLoaded = Boolean(takeout && takeout.loaded);
    if (!state.takeoutLoaded) {
      els.takeoutLoadedBanner.hidden = true;
      els.uploadBox.hidden = false;
      return;
    }

    els.takeoutLoadedBanner.hidden = false;
    els.uploadBox.hidden = true;
    els.loadedFilename.textContent = takeout.filename;
    const total = takeout.uniqueVideos.toLocaleString();
    const adsCount = (takeout.skippedCounts && takeout.skippedCounts.google_ad) || 0;
    const removedCount = (takeout.skippedCounts && takeout.skippedCounts.removed_video) || 0;
    els.loadedSummaryText.textContent =
      `${total} live unique videos ready (${adsCount.toLocaleString()} ads & ${removedCount.toLocaleString()} deleted videos stripped).`;

    const scopes = takeout.scopeCounts || {};
    for (const key of ["test5", "2026", "2024", "all"]) {
      const info = scopes[key];
      if (!info || !els.scopeMeta[key]) {
        continue;
      }
      const eta = formatDuration(info.etaSeconds);
      els.scopeMeta[key].textContent =
        `${info.pending.toLocaleString()} videos left to import (~${eta})`;
    }
  }

  function renderAuthStatus(auth) {
    state.authenticated = Boolean(auth && auth.authenticated);
    state.loginWindowOpen = Boolean(auth && auth.loginWindowOpen);

    els.authConnectedCard.hidden = !state.authenticated;
    els.authSetupCard.hidden = state.authenticated;
    els.btnToStep3.disabled = !state.authenticated;

    if (state.authenticated) {
      els.authAccountLabel.textContent = auth.accountLabel
        ? `Signed in as ${auth.accountLabel}`
        : "YouTube session cookies saved locally.";
      els.authWaitingBar.hidden = true;
      els.btnConfirmLogin.hidden = true;
      els.authStatusMessage.textContent = "";
      stopAuthPolling();
    } else {
      els.btnConfirmLogin.hidden = !state.loginWindowOpen;
      els.authWaitingBar.hidden = !state.loginWindowOpen;
      els.authStatusMessage.textContent = state.loginWindowOpen
        ? "Sign-in browser window is open. Finish logging into your new Gmail on YouTube, then click 'I've Signed In'."
        : "";
    }

    showError(els.authError, (auth && auth.lastAuthError) || "");

    const available = new Set((auth && auth.availableBrowsers) || []);
    for (const browser of ["firefox", "chrome", "brave"]) {
      const radio = document.getElementById(`browser-${browser}`);
      if (!radio) {
        continue;
      }
      radio.disabled = available.size > 0 && !available.has(browser);
    }
  }

  function renderRecentEvents(events) {
    els.recentEventsList.textContent = "";
    if (!events || events.length === 0) {
      const emptyItem = document.createElement("li");
      emptyItem.className = "event-item";
      emptyItem.textContent = "No videos processed in this session yet.";
      els.recentEventsList.appendChild(emptyItem);
      return;
    }

    for (const ev of events) {
      const li = document.createElement("li");
      li.className = "event-item";

      const titleSpan = document.createElement("span");
      titleSpan.className = "event-title";
      const prefix = ev.channel ? `${ev.channel} — ` : "";
      titleSpan.textContent = `${prefix}${ev.title}`;

      const badge = document.createElement("span");
      badge.className = `event-badge is-${ev.status}`;
      badge.textContent = ev.status;

      li.appendChild(titleSpan);
      li.appendChild(badge);
      els.recentEventsList.appendChild(li);
    }
  }

  function migrationAnnouncementText(migration) {
    if (migration.running) {
      return migration.stopping
        ? "Pausing after current video..."
        : "Import running safely with checkpointing.";
    }
    if (!migration.finished) {
      return "";
    }
    return migration.interrupted
      ? "Import paused. Click Start Import anytime to resume."
      : "Import complete!";
  }

  function renderMigrationStatus(migration) {
    if (!migration) {
      return;
    }

    const total = migration.totalTarget || 0;
    const current = migration.currentIndex || 0;
    const running = Boolean(migration.running);

    els.metricWatched.textContent = (migration.watched || 0).toLocaleString();
    els.metricUnavailable.textContent = (migration.unavailable || 0).toLocaleString();
    els.metricFailed.textContent = ((migration.failed || 0) + (migration.authFailed || 0)).toLocaleString();
    els.metricRemaining.textContent = (migration.remaining || 0).toLocaleString();

    els.btnStartImport.hidden = running;
    els.btnStopImport.hidden = !running;
    els.btnStopImport.disabled = Boolean(migration.stopping);
    els.btnBack2.disabled = running;
    if (migration.speed) {
      setSelectedSpeed(migration.speed);
    }
    for (const radio of document.querySelectorAll('input[name="speed"]')) {
      radio.disabled = running;
    }

    if (total > 0) {
      els.importProgress.max = total;
      els.importProgress.value = current;
      els.importProgressLabel.textContent = running
        ? `Importing video ${current.toLocaleString()} of ${total.toLocaleString()}...`
        : `Processed ${current.toLocaleString()} of ${total.toLocaleString()} videos`;
    } else {
      els.importProgress.max = 100;
      els.importProgress.value = migration.finished ? 100 : 0;
      els.importProgressLabel.textContent = migration.finished ? "Already up to date" : "Ready to start";
    }

    els.importEta.textContent = running && migration.remaining > 0
      ? `ETA ~${formatDuration(migration.etaSeconds)}`
      : "";

    els.migrationStatusAnnouncement.textContent = migrationAnnouncementText(migration);

    const showTestNext = Boolean(
      migration.finished &&
      !running &&
      migration.scope === "test5" &&
      migration.watched > 0 &&
      !migration.abortedOnAuth
    );
    els.test5CompleteBanner.hidden = !showTestNext;

    const showAuthExpired = Boolean(migration.abortedOnAuth || migration.lastError);
    els.authExpiredBanner.hidden = !showAuthExpired;
    if (migration.lastError) {
      els.authExpiredText.textContent = migration.lastError;
    }

    renderRecentEvents(migration.recentEvents || []);

    if (!running) {
      stopMigrationPolling();
    }
  }

  async function refreshStep3Summary() {
    if (!state.takeoutLoaded) {
      return;
    }
    try {
      const preview = await apiPost("/api/takeout/preview", {
        scope: getSelectedScope(),
        speed: getSelectedSpeed(),
        includeMusic: els.includeMusic.checked,
        excludeShorts: els.excludeShorts.checked,
      });
      const eta = formatDuration(preview.etaSeconds);
      els.step3Summary.textContent =
        `${preview.targetCount.toLocaleString()} videos queued (oldest to newest, ~${eta}). ${preview.alreadySkipped.toLocaleString()} already imported.`;
    } catch (err) {
      els.step3Summary.textContent = err.message;
    }
  }

  function startAuthPolling() {
    stopAuthPolling();
    state.authPollTimer = window.setInterval(async () => {
      try {
        const auth = await apiGet("/api/auth/status");
        renderAuthStatus(auth);
      } catch (_err) {
        // Ignore transient polling errors.
      }
    }, 1500);
  }

  function stopAuthPolling() {
    if (state.authPollTimer !== null) {
      window.clearInterval(state.authPollTimer);
      state.authPollTimer = null;
    }
  }

  function startMigrationPolling() {
    stopMigrationPolling();
    state.migrationPollTimer = window.setInterval(async () => {
      try {
        const status = await apiGet("/api/migration/status");
        renderMigrationStatus(status);
      } catch (_err) {
        // Ignore transient polling errors.
      }
    }, 1000);
  }

  function stopMigrationPolling() {
    if (state.migrationPollTimer !== null) {
      window.clearInterval(state.migrationPollTimer);
      state.migrationPollTimer = null;
    }
  }

  function uploadFile(file) {
    if (!file) {
      return;
    }
    showError(els.uploadError, "");
    els.uploadProgressWrap.hidden = false;
    els.uploadProgress.value = 0;
    els.uploadProgressLabel.textContent = `Uploading ${file.name}...`;

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/takeout/upload", true);
    xhr.setRequestHeader("X-CSRF-Token", state.csrfToken);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));

    xhr.upload.addEventListener("progress", (ev) => {
      if (!ev.lengthComputable) {
        return;
      }
      const pct = Math.round((ev.loaded / ev.total) * 100);
      if (pct >= 100) {
        els.uploadProgress.removeAttribute("value");
        els.uploadProgressLabel.textContent = "Parsing Takeout watch history...";
        return;
      }
      els.uploadProgress.value = pct;
      els.uploadProgressLabel.textContent = `Uploading ${file.name} (${pct}%)...`;
    });

    xhr.addEventListener("load", () => {
      els.uploadProgressWrap.hidden = true;
      els.uploadProgress.value = 0;
      try {
        const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 400) {
          showError(els.uploadError, data.error || "Upload failed.");
          return;
        }
        renderTakeoutSummary(data.takeout);
      } catch (_err) {
        showError(els.uploadError, "Unexpected server response while parsing file.");
      }
    });

    xhr.addEventListener("error", () => {
      els.uploadProgressWrap.hidden = true;
      showError(els.uploadError, "Network error while uploading file.");
    });

    xhr.send(file);
  }

  async function startImport(scopeOverride) {
    if (scopeOverride) {
      setSelectedScope(scopeOverride);
    }
    try {
      const status = await apiPost("/api/migration/start", {
        scope: getSelectedScope(),
        speed: getSelectedSpeed(),
        includeMusic: els.includeMusic.checked,
        excludeShorts: els.excludeShorts.checked,
      });
      renderMigrationStatus(status);
      refreshStep3Summary();
      startMigrationPolling();
    } catch (err) {
      showError(els.authError, err.message);
      els.authExpiredBanner.hidden = false;
      els.authExpiredText.textContent = err.message;
    }
  }

  function wireEvents() {
    els.btnChangeFile.addEventListener("click", () => {
      els.uploadBox.hidden = false;
      els.takeoutInput.click();
    });

    els.takeoutInput.addEventListener("change", (ev) => {
      const file = ev.target.files && ev.target.files[0];
      uploadFile(file);
    });

    els.dropzone.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      els.dropzone.classList.add("is-dragover");
    });

    els.dropzone.addEventListener("dragleave", () => {
      els.dropzone.classList.remove("is-dragover");
    });

    els.dropzone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      els.dropzone.classList.remove("is-dragover");
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      uploadFile(file);
    });

    els.step1Form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      if (!state.takeoutLoaded) {
        showError(els.step1Error, "Please upload your Google Takeout .zip or watch-history file first.");
        return;
      }
      showError(els.step1Error, "");
      goToStep(2);
    });

    els.btnOpenLogin.addEventListener("click", async () => {
      showError(els.authError, "");
      els.btnOpenLogin.disabled = true;
      try {
        const auth = await apiPost("/api/auth/start", {
          browser: getSelectedBrowser(),
          fresh: false,
        });
        renderAuthStatus(auth);
        startAuthPolling();
      } catch (err) {
        showError(els.authError, err.message);
      } finally {
        els.btnOpenLogin.disabled = false;
      }
    });

    els.btnConfirmLogin.addEventListener("click", async () => {
      showError(els.authError, "");
      els.btnConfirmLogin.disabled = true;
      try {
        const auth = await apiPost("/api/auth/complete", {});
        renderAuthStatus(auth);
      } catch (err) {
        renderAuthStatus({ authenticated: false, loginWindowOpen: false, lastAuthError: err.message });
      } finally {
        els.btnConfirmLogin.disabled = false;
      }
    });

    els.btnSwitchAccount.addEventListener("click", async () => {
      showError(els.authError, "");
      try {
        const auth = await apiPost("/api/auth/start", {
          browser: getSelectedBrowser(),
          fresh: true,
        });
        renderAuthStatus(auth);
        startAuthPolling();
      } catch (err) {
        showError(els.authError, err.message);
      }
    });

    els.btnBack1.addEventListener("click", () => goToStep(1));
    els.btnToStep3.addEventListener("click", () => {
      if (!state.authenticated) {
        showError(els.authError, "Please sign in to your new YouTube account first.");
        return;
      }
      goToStep(3);
    });

    els.btnStartImport.addEventListener("click", () => startImport(null));
    els.btnStopImport.addEventListener("click", async () => {
      try {
        const status = await apiPost("/api/migration/stop", {});
        renderMigrationStatus(status);
      } catch (_err) {
        // Ignore transient stop errors.
      }
    });

    els.btnRun2024.addEventListener("click", () => startImport("2024"));
    els.btnRunAll.addEventListener("click", () => startImport("all"));
    els.btnReauthStep2.addEventListener("click", () => goToStep(2));
    els.btnBack2.addEventListener("click", () => goToStep(2));
    for (const radio of document.querySelectorAll('input[name="speed"]')) {
      radio.addEventListener("change", () => refreshStep3Summary());
    }
  }

  async function init() {
    wireEvents();
    try {
      const boot = await apiGet("/api/bootstrap");
      state.csrfToken = boot.csrfToken || "";
      renderTakeoutSummary(boot.takeout);
      renderAuthStatus(boot.auth);
      renderMigrationStatus(boot.migration);

      if (boot.migration && boot.migration.running) {
        goToStep(3);
        startMigrationPolling();
        return;
      }
      if (boot.auth && boot.auth.loginWindowOpen) {
        goToStep(2);
        startAuthPolling();
      }
    } catch (err) {
      showError(els.step1Error, `Failed to connect to local server: ${err.message}`);
    }
  }

  init();
})();
