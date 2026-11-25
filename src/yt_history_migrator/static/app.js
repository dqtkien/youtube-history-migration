"use strict";

(() => {
  const STEP_TITLES = {
    1: "Step 1: Import Takeout",
    2: "Step 2: Sign In to New Gmail",
    3: "Step 3: Import History",
  };

  const STATUS_BADGE_LABELS = {
    watched: "Watched",
    unavailable: "Unavailable",
    failed: "Failed",
    auth_failed: "Auth failed",
  };

  const state = {
    csrfToken: "",
    currentStep: 1,
    currentYear: new Date().getUTCFullYear(),
    threeYearsAgoYear: new Date().getUTCFullYear() - 2,
    takeoutLoaded: false,
    takeout: null,
    authenticated: false,
    accountLabel: null,
    availableBrowsers: [],
    loginWindowOpen: false,
    checkpoint: {
      totalRecorded: 0,
      watched: 0,
      unavailable: 0,
      failed: 0,
      authFailed: 0,
    },
    migration: null,
    latestPreview: null,
    eventFilter: "all",
    consecutivePollErrors: 0,
    serverStopped: false,
    authPollTimer: null,
    migrationPollTimer: null,
    customPreviewTimer: null,
  };

  const els = {
    btnStopServer: document.getElementById("btn-stop-server"),
    serverOfflineBanner: document.getElementById("server-offline-banner"),
    serverOfflineText: document.getElementById("server-offline-text"),
    btnReconnect: document.getElementById("btn-reconnect"),
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
    loadedMetaText: document.getElementById("loaded-meta-text"),
    btnChangeFile: document.getElementById("btn-change-file"),
    btnCancelUpload: document.getElementById("btn-cancel-upload"),
    uploadBox: document.getElementById("upload-box"),
    dropzone: document.getElementById("dropzone"),
    takeoutInput: document.getElementById("takeout-input"),
    uploadProgressWrap: document.getElementById("upload-progress-wrap"),
    uploadProgressLabel: document.getElementById("upload-progress-label"),
    uploadProgress: document.getElementById("upload-progress"),
    uploadError: document.getElementById("upload-error"),
    step1Form: document.getElementById("step-1-form"),
    step1Error: document.getElementById("step-1-error"),
    labelThisYear: document.getElementById("label-this-year"),
    labelThreeYears: document.getElementById("label-3-years"),
    customDateFields: document.getElementById("custom-date-fields"),
    customAfterDate: document.getElementById("custom-after-date"),
    customBeforeDate: document.getElementById("custom-before-date"),
    customDateError: document.getElementById("custom-date-error"),
    includeMusic: document.getElementById("opt-include-music"),
    excludeShorts: document.getElementById("opt-exclude-shorts"),
    retryUnavailable: document.getElementById("opt-retry-unavailable"),
    scopeMeta: {
      test5: document.getElementById("scope-meta-test5"),
      this_year: document.getElementById("scope-meta-this_year"),
      last_3_years: document.getElementById("scope-meta-last_3_years"),
      all: document.getElementById("scope-meta-all"),
      custom: document.getElementById("scope-meta-custom"),
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
    step3AccountChip: document.getElementById("step3-account-chip"),
    step3ScopeChip: document.getElementById("step3-scope-chip"),
    btnEditScope: document.getElementById("btn-edit-scope"),
    step3CheckpointChip: document.getElementById("step3-checkpoint-chip"),
    btnResetProgress: document.getElementById("btn-reset-progress"),
    step3Summary: document.getElementById("step-3-summary"),
    queuePreviewDetails: document.getElementById("queue-preview-details"),
    queuePreviewBody: document.getElementById("queue-preview-body"),
    btnStartImport: document.getElementById("btn-start-import"),
    btnStopImport: document.getElementById("btn-stop-import"),
    importProgressLabel: document.getElementById("import-progress-label"),
    importPercent: document.getElementById("import-percent"),
    importEta: document.getElementById("import-eta"),
    importProgress: document.getElementById("import-progress"),
    migrationStatusAnnouncement: document.getElementById("migration-status-announcement"),
    metricWatched: document.getElementById("metric-watched"),
    metricUnavailable: document.getElementById("metric-unavailable"),
    metricFailed: document.getElementById("metric-failed"),
    metricRemaining: document.getElementById("metric-remaining"),
    test5CompleteBanner: document.getElementById("test5-complete-banner"),
    btnRunLast3: document.getElementById("btn-run-last3"),
    btnRunAll: document.getElementById("btn-run-all"),
    fullCompleteBanner: document.getElementById("full-complete-banner"),
    fullCompleteText: document.getElementById("full-complete-text"),
    btnChangeScopeDone: document.getElementById("btn-change-scope-done"),
    authExpiredBanner: document.getElementById("auth-expired-banner"),
    authExpiredText: document.getElementById("auth-expired-text"),
    btnReauthStep2: document.getElementById("btn-reauth-step2"),
    migrationErrorBanner: document.getElementById("migration-error-banner"),
    migrationErrorText: document.getElementById("migration-error-text"),
    btnDismissError: document.getElementById("btn-dismiss-error"),
    recentEventsList: document.getElementById("recent-events-list"),
    btnBack2: document.getElementById("btn-back-2"),
    confirmDialog: document.getElementById("confirm-dialog"),
    confirmDialogTitle: document.getElementById("confirm-dialog-title"),
    confirmDialogBody: document.getElementById("confirm-dialog-body"),
    confirmDialogCancel: document.getElementById("confirm-dialog-cancel"),
    confirmDialogAlt: document.getElementById("confirm-dialog-alt"),
    confirmDialogOk: document.getElementById("confirm-dialog-ok"),
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

  function formatShortDate(rawDate) {
    if (!rawDate) {
      return "";
    }
    const parsed = new Date(rawDate);
    if (Number.isNaN(parsed.getTime())) {
      return String(rawDate).slice(0, 10);
    }
    return parsed.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
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

  function openConfirmDialog({
    title,
    body,
    confirmLabel = "Confirm",
    altLabel = "",
    cancelLabel = "Cancel",
  }) {
    if (!els.confirmDialog || typeof els.confirmDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(`${title}\n\n${body}`) ? "confirm" : "cancel");
    }

    els.confirmDialogTitle.textContent = title;
    els.confirmDialogBody.textContent = body;
    els.confirmDialogCancel.textContent = cancelLabel;
    els.confirmDialogOk.textContent = confirmLabel;
    if (altLabel) {
      els.confirmDialogAlt.textContent = altLabel;
      els.confirmDialogAlt.hidden = false;
    } else {
      els.confirmDialogAlt.hidden = true;
    }
    els.confirmDialog.returnValue = "";

    return new Promise((resolve) => {
      els.confirmDialog.addEventListener(
        "close",
        () => {
          const result = els.confirmDialog.returnValue || "cancel";
          els.confirmDialog.returnValue = "";
          resolve(result);
        },
        { once: true }
      );
      els.confirmDialog.showModal();
      els.confirmDialogCancel.focus();
    });
  }

  function getSelectedScope() {
    const checked = document.querySelector('input[name="scope"]:checked');
    return checked ? checked.value : "test5";
  }

  function setSelectedScope(scope) {
    const normalized = scope === "2026" ? "this_year" : scope === "2024" ? "last_3_years" : scope;
    const radio = document.querySelector(`input[name="scope"][value="${normalized}"]`);
    if (radio) {
      radio.checked = true;
    }
    syncCustomDateVisibility();
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

  function getFilterPayload(scopeOverride) {
    const scope = scopeOverride || getSelectedScope();
    return {
      scope,
      speed: getSelectedSpeed(),
      includeMusic: els.includeMusic.checked,
      excludeShorts: els.excludeShorts.checked,
      retryUnavailable: els.retryUnavailable.checked,
      afterDate: scope === "custom" ? els.customAfterDate.value || null : null,
      beforeDate: scope === "custom" ? els.customBeforeDate.value || null : null,
    };
  }

  function validateCustomDates() {
    const scope = getSelectedScope();
    if (scope !== "custom") {
      els.customAfterDate.removeAttribute("aria-invalid");
      els.customBeforeDate.removeAttribute("aria-invalid");
      els.customAfterDate.removeAttribute("aria-describedby");
      els.customBeforeDate.removeAttribute("aria-describedby");
      showError(els.customDateError, "");
      return true;
    }

    const afterVal = els.customAfterDate.value;
    const beforeVal = els.customBeforeDate.value;
    if (afterVal && beforeVal && afterVal > beforeVal) {
      els.customAfterDate.setAttribute("aria-invalid", "true");
      els.customBeforeDate.setAttribute("aria-invalid", "true");
      els.customAfterDate.setAttribute("aria-describedby", "custom-date-error");
      els.customBeforeDate.setAttribute("aria-describedby", "custom-date-error");
      showError(els.customDateError, "Start date must be on or before end date.");
      return false;
    }

    els.customAfterDate.removeAttribute("aria-invalid");
    els.customBeforeDate.removeAttribute("aria-invalid");
    els.customAfterDate.removeAttribute("aria-describedby");
    els.customBeforeDate.removeAttribute("aria-describedby");
    showError(els.customDateError, "");
    return true;
  }

  function syncCustomDateVisibility() {
    const isCustom = getSelectedScope() === "custom";
    els.customDateFields.hidden = !isCustom;
    validateCustomDates();
  }

  function markServerOnline() {
    state.consecutivePollErrors = 0;
    if (!state.serverStopped) {
      els.serverOfflineBanner.hidden = true;
    }
  }

  function recordPollError() {
    if (state.serverStopped) {
      return;
    }
    state.consecutivePollErrors += 1;
    if (state.consecutivePollErrors >= 3) {
      els.serverOfflineText.textContent =
        "Lost connection to the local server. Make sure 'yt-migrate ui' is still running and click Reconnect.";
      els.serverOfflineBanner.hidden = false;
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
    markServerOnline();
    return data;
  }

  async function apiGet(url) {
    const response = await fetch(url, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    markServerOnline();
    return data;
  }

  function updateDocumentTitle() {
    const m = state.migration;
    if (m && m.running) {
      if (m.phase === "connecting") {
        document.title = "Connecting… — YouTube History Migrator";
        return;
      }
      if (m.phase === "rate_limited" || m.phase === "cooldown") {
        document.title = "Cooldown… — YouTube History Migrator";
        return;
      }
      const total = m.totalTarget || 0;
      const pct = total > 0 ? Math.min(100, Math.round(((m.currentIndex || 0) / total) * 100)) : 0;
      document.title = `${pct}% • Importing — YouTube History Migrator`;
      return;
    }
    if (m && m.finished && m.interrupted) {
      document.title = "Paused — YouTube History Migrator";
      return;
    }
    if (m && m.finished && !m.abortedOnAuth && !m.lastError) {
      document.title = "Import Complete — YouTube History Migrator";
      return;
    }
    document.title = `${STEP_TITLES[state.currentStep] || "Wizard"} — YouTube History Migrator`;
  }

  function syncStepperState() {
    const isRunning = Boolean(state.migration && state.migration.running);
    const canStep2 = state.takeoutLoaded && !isRunning;
    const canStep3 = state.takeoutLoaded && state.authenticated;

    els.navSteps[1].disabled = isRunning;
    els.navSteps[2].disabled = !canStep2;
    els.navSteps[3].disabled = isRunning ? state.currentStep !== 3 : !canStep3;

    for (const num of [1, 2, 3]) {
      const btn = els.navSteps[num];
      const active = num === state.currentStep;
      const done = num < state.currentStep;
      btn.classList.toggle("is-active", active);
      btn.classList.toggle("is-done", done);

      const numBadge = btn.querySelector(".step-num");
      const srState = btn.querySelector(".step-sr-state");
      if (numBadge) {
        numBadge.textContent = done ? "✓" : String(num);
      }
      if (srState) {
        srState.textContent = done ? " (Completed)" : "";
      }

      if (active) {
        btn.setAttribute("aria-current", "step");
      } else {
        btn.removeAttribute("aria-current");
      }
    }
  }

  function goToStep(stepNumber, { moveFocus = true } = {}) {
    state.currentStep = stepNumber;
    for (const num of [1, 2, 3]) {
      els.panels[num].hidden = num !== stepNumber;
    }
    syncStepperState();
    updateDocumentTitle();

    if (moveFocus) {
      els.headings[stepNumber].focus();
    }
    if (stepNumber === 1 && state.takeoutLoaded) {
      refreshTakeoutSummary();
    }
    if (stepNumber === 3) {
      renderStep3Context();
      refreshStep3Summary();
    }
  }

  function renderTakeoutSummary(takeout) {
    state.takeout = takeout || null;
    state.takeoutLoaded = Boolean(takeout && takeout.loaded);
    if (takeout && typeof takeout.currentYear === "number") {
      state.currentYear = takeout.currentYear;
      state.threeYearsAgoYear = takeout.threeYearsAgoYear || takeout.currentYear - 2;
      els.labelThisYear.textContent = String(state.currentYear);
      els.labelThreeYears.textContent = String(state.threeYearsAgoYear);
    }

    if (!state.takeoutLoaded) {
      els.takeoutLoadedBanner.hidden = true;
      els.uploadBox.hidden = false;
      els.btnCancelUpload.hidden = true;
      syncStepperState();
      return;
    }

    els.takeoutLoadedBanner.hidden = false;
    els.uploadBox.hidden = true;
    els.btnCancelUpload.hidden = true;
    els.loadedFilename.textContent = takeout.filename;

    const total = takeout.uniqueVideos.toLocaleString();
    const adsCount = (takeout.skippedCounts && takeout.skippedCounts.google_ad) || 0;
    const removedCount = (takeout.skippedCounts && takeout.skippedCounts.removed_video) || 0;
    els.loadedSummaryText.textContent =
      `${total} live unique videos ready (${adsCount.toLocaleString()} ads & ${removedCount.toLocaleString()} deleted videos stripped).`;

    const metaParts = [];
    if (takeout.oldestDate && takeout.newestDate) {
      metaParts.push(`${formatShortDate(takeout.oldestDate)} → ${formatShortDate(takeout.newestDate)}`);
    }
    if (takeout.duplicatesMerged > 0) {
      metaParts.push(`${takeout.duplicatesMerged.toLocaleString()} rewatches merged`);
    }
    els.loadedMetaText.textContent = metaParts.join(" · ");
    els.loadedMetaText.hidden = metaParts.length === 0;

    const scopes = takeout.scopeCounts || {};
    for (const key of ["test5", "this_year", "last_3_years", "all"]) {
      const info = scopes[key];
      if (!info || !els.scopeMeta[key]) {
        continue;
      }
      const eta = formatDuration(info.etaSeconds);
      els.scopeMeta[key].textContent =
        `${info.pending.toLocaleString()} videos left to import (~${eta})`;
    }
    syncStepperState();
  }

  function renderAuthStatus(auth) {
    state.authenticated = Boolean(auth && auth.authenticated);
    state.accountLabel = (auth && auth.accountLabel) || null;
    state.loginWindowOpen = Boolean(auth && auth.loginWindowOpen);
    if (auth && Array.isArray(auth.availableBrowsers)) {
      state.availableBrowsers = auth.availableBrowsers;
    }

    els.authConnectedCard.hidden = !state.authenticated;
    els.authSetupCard.hidden = state.authenticated;
    els.btnToStep3.disabled = !state.authenticated;

    if (state.authenticated) {
      els.authAccountLabel.textContent = state.accountLabel
        ? `Signed in as ${state.accountLabel}`
        : "YouTube session cookies saved locally.";
      els.authWaitingBar.hidden = true;
      els.btnConfirmLogin.hidden = true;
      els.authStatusMessage.textContent = "";
      stopAuthPolling();
    } else {
      els.btnConfirmLogin.hidden = !state.loginWindowOpen;
      els.authWaitingBar.hidden = !state.loginWindowOpen;
      els.authStatusMessage.textContent = state.loginWindowOpen
        ? "Sign-in browser window is open. Finish logging into your new Gmail on YouTube, then click \"I've Signed In — Save & Close Window\"."
        : "";
    }

    showError(els.authError, (auth && auth.lastAuthError) || "");

    const available = new Set(state.availableBrowsers);
    for (const browser of ["chrome", "firefox", "brave"]) {
      const radio = document.getElementById(`browser-${browser}`);
      const statusEl = document.getElementById(`browser-status-${browser}`);
      if (!radio) {
        continue;
      }
      const unavailable = available.size > 0 && !available.has(browser);
      radio.disabled = unavailable;
      if (statusEl) {
        statusEl.textContent = unavailable ? "Not installed" : "";
      }
    }

    const currentBrowserRadio = document.querySelector('input[name="browser"]:checked');
    if (currentBrowserRadio && currentBrowserRadio.disabled) {
      const fallback = document.querySelector('input[name="browser"]:not(:disabled)');
      if (fallback) {
        fallback.checked = true;
      }
    }

    syncStepperState();
    renderStep3Context();
  }

  function describeActiveScope(payload) {
    if (payload.scope === "test5") {
      return "Quick 5-Video Test";
    }
    if (payload.scope === "this_year") {
      return `This Year (${state.currentYear})`;
    }
    if (payload.scope === "last_3_years") {
      return `Last 3 Years (${state.threeYearsAgoYear}+)`;
    }
    if (payload.scope === "all") {
      return "All History";
    }
    if (payload.scope === "custom") {
      if (payload.afterDate && payload.beforeDate) {
        return `${payload.afterDate} → ${payload.beforeDate}`;
      }
      if (payload.afterDate) {
        return `From ${payload.afterDate}`;
      }
      if (payload.beforeDate) {
        return `Until ${payload.beforeDate}`;
      }
      return "Custom (All Dates)";
    }
    return payload.scope;
  }

  function renderStep3Context() {
    const payload = getFilterPayload();
    els.step3AccountChip.textContent = state.accountLabel || "Connected account";

    const filterFlags = [];
    if (payload.includeMusic) {
      filterFlags.push("+Music");
    }
    if (payload.excludeShorts) {
      filterFlags.push("No Shorts");
    }
    if (payload.retryUnavailable) {
      filterFlags.push("Retry Unavailable");
    }
    const scopeLabel = describeActiveScope(payload);
    els.step3ScopeChip.textContent = filterFlags.length > 0
      ? `${scopeLabel} (${filterFlags.join(", ")})`
      : scopeLabel;

    const cp = state.checkpoint || { totalRecorded: 0, watched: 0 };
    const recorded = cp.totalRecorded || 0;
    const watched = cp.watched || 0;
    els.step3CheckpointChip.textContent = recorded > 0
      ? `${watched.toLocaleString()} watched (${recorded.toLocaleString()} saved)`
      : "0 saved";

    const isRunning = Boolean(state.migration && state.migration.running);
    els.btnResetProgress.hidden = recorded === 0 || isRunning;
    els.btnEditScope.disabled = isRunning;
  }

  function renderQueuePreview(preview) {
    els.queuePreviewBody.textContent = "";
    if (!preview || preview.targetCount === 0) {
      const p = document.createElement("p");
      p.className = "banner-subtext";
      p.textContent = "All matching videos in this scope are already imported or skipped.";
      els.queuePreviewBody.appendChild(p);
      return;
    }

    const groups = [
      { label: "First up (oldest)", items: preview.sampleFirst || [] },
      { label: "Last up (newest)", items: preview.sampleLast || [] },
    ];

    for (const group of groups) {
      if (group.items.length === 0) {
        continue;
      }
      const wrap = document.createElement("div");
      wrap.className = "queue-sample-group";

      const heading = document.createElement("p");
      heading.className = "queue-sample-heading";
      heading.textContent = group.label;
      wrap.appendChild(heading);

      const list = document.createElement("ul");
      list.className = "queue-sample-list";
      list.setAttribute("role", "list");

      for (const item of group.items) {
        const li = document.createElement("li");
        li.className = "queue-sample-item";

        const title = document.createElement("span");
        title.className = "queue-sample-title";
        const prefix = item.channel ? `${item.channel} — ` : "";
        title.textContent = `${prefix}${item.title}`;

        const date = document.createElement("span");
        date.className = "queue-sample-date";
        date.textContent = item.watchedAt ? formatShortDate(item.watchedAt) : "Unknown date";

        li.appendChild(title);
        li.appendChild(date);
        list.appendChild(li);
      }

      wrap.appendChild(list);
      els.queuePreviewBody.appendChild(wrap);
    }
  }

  function matchesEventFilter(ev, filter) {
    if (filter === "all") {
      return true;
    }
    if (filter === "failed") {
      return ev.status === "failed" || ev.status === "auth_failed";
    }
    return ev.status === filter;
  }

  function updateEventFilterButtons(events) {
    const counts = {
      all: events.length,
      watched: 0,
      unavailable: 0,
      failed: 0,
    };
    for (const ev of events) {
      if (ev.status === "watched") {
        counts.watched += 1;
      } else if (ev.status === "unavailable") {
        counts.unavailable += 1;
      } else if (ev.status === "failed" || ev.status === "auth_failed") {
        counts.failed += 1;
      }
    }

    const labels = {
      all: "All",
      watched: "Watched",
      unavailable: "Unavailable",
      failed: "Failed",
    };

    for (const btn of document.querySelectorAll(".filter-pill")) {
      const key = btn.getAttribute("data-event-filter") || "all";
      btn.setAttribute("aria-pressed", key === state.eventFilter ? "true" : "false");
      btn.textContent = `${labels[key] || key} (${counts[key] || 0})`;
    }
  }

  function renderRecentEvents(events) {
    const list = events || [];
    updateEventFilterButtons(list);
    els.recentEventsList.textContent = "";

    if (list.length === 0) {
      const emptyItem = document.createElement("li");
      emptyItem.className = "event-empty";
      emptyItem.textContent = "No videos processed in this session yet.";
      els.recentEventsList.appendChild(emptyItem);
      return;
    }

    const filtered = list.filter((ev) => matchesEventFilter(ev, state.eventFilter));
    if (filtered.length === 0) {
      const emptyFilter = document.createElement("li");
      emptyFilter.className = "event-empty";
      emptyFilter.textContent = `No ${state.eventFilter} videos in this session.`;
      els.recentEventsList.appendChild(emptyFilter);
      return;
    }

    for (const ev of filtered) {
      const li = document.createElement("li");
      li.className = "event-item";

      const mainRow = document.createElement("div");
      mainRow.className = "event-row-main";

      const titleWrap = document.createElement("div");
      titleWrap.className = "event-title-wrap";

      const prefix = ev.channel ? `${ev.channel} — ` : "";
      const fullTitle = `${prefix}${ev.title}`;
      const isSafeUrl = typeof ev.url === "string" && ev.url.startsWith("https://");

      if (isSafeUrl) {
        const link = document.createElement("a");
        link.className = "event-link";
        link.href = ev.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = fullTitle;
        titleWrap.appendChild(link);
      } else {
        const titleSpan = document.createElement("span");
        titleSpan.className = "event-title";
        titleSpan.textContent = fullTitle;
        titleWrap.appendChild(titleSpan);
      }

      const metaRight = document.createElement("div");
      metaRight.className = "event-meta-right";

      if (ev.watchedAt) {
        const dateSpan = document.createElement("span");
        dateSpan.className = "event-date";
        dateSpan.textContent = String(ev.watchedAt).slice(0, 10);
        metaRight.appendChild(dateSpan);
      }

      const badge = document.createElement("span");
      badge.className = `event-badge is-${ev.status}`;
      badge.textContent = STATUS_BADGE_LABELS[ev.status] || ev.status;
      metaRight.appendChild(badge);

      mainRow.appendChild(titleWrap);
      mainRow.appendChild(metaRight);
      li.appendChild(mainRow);

      if (ev.reason) {
        const reasonLine = document.createElement("span");
        reasonLine.className = "event-reason";
        reasonLine.textContent = ev.reason;
        li.appendChild(reasonLine);
      }

      els.recentEventsList.appendChild(li);
    }
  }

  function migrationAnnouncementText(migration) {
    if (migration.running) {
      if (migration.stopping) {
        return "Pausing after current video...";
      }
      if (migration.phaseDetail) {
        return migration.phaseDetail;
      }
      return "Import running safely with per-video checkpointing.";
    }
    if (!migration.finished) {
      return "";
    }
    if (migration.abortedOnAuth || migration.lastError) {
      return "";
    }
    return migration.interrupted
      ? "Import paused. Click Resume Import anytime to continue."
      : "Import complete!";
  }

  function syncStartButtonLabel() {
    const m = state.migration;
    if (m && m.running) {
      return;
    }
    if (m && m.finished && m.interrupted) {
      els.btnStartImport.textContent = "Resume Import";
      els.btnStartImport.disabled = false;
      return;
    }
    if (state.latestPreview && state.latestPreview.targetCount === 0) {
      els.btnStartImport.textContent = "All Caught Up";
      els.btnStartImport.disabled = true;
      return;
    }
    if (state.latestPreview && state.latestPreview.alreadySkipped > 0) {
      els.btnStartImport.textContent = "Resume Import";
      els.btnStartImport.disabled = false;
      return;
    }
    els.btnStartImport.textContent = "Start Import";
    els.btnStartImport.disabled = false;
  }

  function renderMigrationStatus(migration) {
    if (!migration) {
      return;
    }
    state.migration = migration;
    if (migration.checkpoint) {
      state.checkpoint = migration.checkpoint;
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

    if (running && migration.phase === "connecting") {
      els.importProgress.removeAttribute("value");
      els.importProgressLabel.textContent = "Connecting to YouTube...";
      els.importPercent.textContent = "";
    } else if (total > 0) {
      els.importProgress.max = total;
      els.importProgress.value = current;
      const pct = Math.min(100, Math.round((current / total) * 100));
      els.importPercent.textContent = `${pct}%`;
      els.importProgressLabel.textContent = running
        ? `Importing video ${current.toLocaleString()} of ${total.toLocaleString()}...`
        : `Processed ${current.toLocaleString()} of ${total.toLocaleString()} videos`;
    } else {
      els.importProgress.max = 100;
      els.importProgress.value = migration.finished ? 100 : 0;
      els.importPercent.textContent = migration.finished ? "100%" : "";
      els.importProgressLabel.textContent = migration.finished ? "Already up to date" : "Ready to start";
    }

    els.importEta.textContent = running && migration.remaining > 0 && migration.phase !== "connecting"
      ? `ETA ~${formatDuration(migration.etaSeconds)}`
      : "";

    els.migrationStatusAnnouncement.textContent = migrationAnnouncementText(migration);

    const cleanFinish = Boolean(
      migration.finished &&
      !running &&
      !migration.interrupted &&
      !migration.abortedOnAuth &&
      !migration.lastError
    );
    const showTestNext = Boolean(cleanFinish && migration.scope === "test5" && migration.watched > 0);
    const showFullComplete = Boolean(cleanFinish && !showTestNext);

    els.test5CompleteBanner.hidden = !showTestNext;
    els.fullCompleteBanner.hidden = !showFullComplete;
    if (showFullComplete) {
      els.fullCompleteText.textContent =
        `${(migration.watched || 0).toLocaleString()} videos marked as watched and ${(migration.unavailable || 0).toLocaleString()} skipped as unavailable.`;
    }

    const showAuthExpired = Boolean(migration.abortedOnAuth);
    els.authExpiredBanner.hidden = !showAuthExpired;
    if (showAuthExpired && migration.lastError) {
      els.authExpiredText.textContent = migration.lastError;
    }

    const showGenericError = Boolean(!migration.abortedOnAuth && migration.lastError);
    els.migrationErrorBanner.hidden = !showGenericError;
    if (showGenericError) {
      els.migrationErrorText.textContent = migration.lastError;
    }

    renderRecentEvents(migration.recentEvents || []);
    renderStep3Context();
    syncStepperState();
    syncStartButtonLabel();
    updateDocumentTitle();

    if (!running) {
      stopMigrationPolling();
    }
  }

  async function refreshTakeoutSummary() {
    if (!state.takeoutLoaded) {
      return;
    }
    try {
      const payload = getFilterPayload();
      const data = await apiPost("/api/takeout/summary", {
        includeMusic: payload.includeMusic,
        excludeShorts: payload.excludeShorts,
        retryUnavailable: payload.retryUnavailable,
        speed: payload.speed,
      });
      renderTakeoutSummary(data.takeout);
      if (payload.scope === "custom") {
        refreshCustomDatePreview();
      }
    } catch (_err) {
      // Keep current counts on transient errors.
    }
  }

  async function refreshCustomDatePreview() {
    if (!state.takeoutLoaded || getSelectedScope() !== "custom" || !validateCustomDates()) {
      return;
    }
    try {
      const preview = await apiPost("/api/takeout/preview", getFilterPayload("custom"));
      const eta = formatDuration(preview.etaSeconds);
      els.scopeMeta.custom.textContent =
        `${preview.targetCount.toLocaleString()} videos left to import (~${eta})`;
    } catch (err) {
      els.scopeMeta.custom.textContent = err.message;
    }
  }

  async function refreshStep3Summary() {
    if (!state.takeoutLoaded) {
      return;
    }
    try {
      const preview = await apiPost("/api/takeout/preview", getFilterPayload());
      state.latestPreview = preview;
      const eta = formatDuration(preview.etaSeconds);
      els.step3Summary.textContent =
        `${preview.targetCount.toLocaleString()} videos queued (oldest to newest, ~${eta}). ${preview.alreadySkipped.toLocaleString()} already imported.`;
      renderQueuePreview(preview);
      renderStep3Context();
      syncStartButtonLabel();
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
        recordPollError();
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
        recordPollError();
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
    els.btnCancelUpload.hidden = true;
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
          els.btnCancelUpload.hidden = !state.takeoutLoaded;
          return;
        }
        renderTakeoutSummary(data.takeout);
        refreshTakeoutSummary();
      } catch (_err) {
        showError(els.uploadError, "Unexpected server response while parsing file.");
        els.btnCancelUpload.hidden = !state.takeoutLoaded;
      }
    });

    xhr.addEventListener("error", () => {
      els.uploadProgressWrap.hidden = true;
      showError(els.uploadError, "Network error while uploading file.");
      els.btnCancelUpload.hidden = !state.takeoutLoaded;
    });

    xhr.send(file);
  }

  async function startImport(scopeOverride) {
    if (scopeOverride) {
      setSelectedScope(scopeOverride);
    }
    els.migrationErrorBanner.hidden = true;
    els.authExpiredBanner.hidden = true;
    try {
      const status = await apiPost("/api/migration/start", getFilterPayload(scopeOverride));
      renderMigrationStatus(status);
      refreshStep3Summary();
      startMigrationPolling();
    } catch (err) {
      els.migrationErrorText.textContent = err.message;
      els.migrationErrorBanner.hidden = false;
    }
  }

  async function handleSwitchAccount() {
    showError(els.authError, "");
    let resetProgress = false;
    const recorded = (state.checkpoint && state.checkpoint.totalRecorded) || 0;

    if (recorded > 0) {
      const choice = await openConfirmDialog({
        title: "Switch Destination Account?",
        body: `You already have ${recorded.toLocaleString()} videos saved in your checkpoint from a previous run. Keeping the checkpoint skips those videos on the next account; resetting clears saved progress so you can import fresh.`,
        confirmLabel: "Reset Checkpoint & Switch",
        altLabel: "Keep Checkpoint & Switch",
        cancelLabel: "Cancel",
      });
      if (choice === "cancel") {
        return;
      }
      resetProgress = choice === "confirm";
    }

    els.btnSwitchAccount.disabled = true;
    try {
      const auth = await apiPost("/api/auth/start", {
        browser: getSelectedBrowser(),
        fresh: true,
        resetProgress,
      });
      if (resetProgress) {
        state.checkpoint = { totalRecorded: 0, watched: 0, unavailable: 0, failed: 0, authFailed: 0 };
        state.migration = null;
        refreshTakeoutSummary();
      }
      renderAuthStatus(auth);
      startAuthPolling();
    } catch (err) {
      showError(els.authError, err.message);
    } finally {
      els.btnSwitchAccount.disabled = false;
    }
  }

  async function handleResetProgress() {
    const recorded = (state.checkpoint && state.checkpoint.totalRecorded) || 0;
    const choice = await openConfirmDialog({
      title: "Reset Saved Checkpoint?",
      body: `This clears ${recorded.toLocaleString()} saved video outcomes from state/progress.jsonl. Future imports will start from the oldest video again.`,
      confirmLabel: "Reset Checkpoint",
      cancelLabel: "Cancel",
    });
    if (choice !== "confirm") {
      return;
    }
    try {
      const data = await apiPost("/api/progress/reset", {});
      state.checkpoint = data.checkpoint || { totalRecorded: 0, watched: 0, unavailable: 0, failed: 0, authFailed: 0 };
      renderTakeoutSummary(data.takeout);
      renderMigrationStatus(data.migration);
      refreshStep3Summary();
    } catch (err) {
      els.migrationErrorText.textContent = err.message;
      els.migrationErrorBanner.hidden = false;
    }
  }

  async function handleStopServer() {
    const running = Boolean(state.migration && state.migration.running);
    const body = running
      ? "An import is currently running. Stopping the server will pause after the current video, save your checkpoint, and exit."
      : "Shut down the local YouTube History Migrator server? All saved progress stays on disk.";
    const choice = await openConfirmDialog({
      title: "Stop Local Server?",
      body,
      confirmLabel: "Stop Server",
      cancelLabel: "Keep Running",
    });
    if (choice !== "confirm") {
      return;
    }
    try {
      await apiPost("/api/server/shutdown", {});
      state.serverStopped = true;
      stopAuthPolling();
      stopMigrationPolling();
      els.btnStopServer.disabled = true;
      els.serverOfflineText.textContent =
        "Local server stopped cleanly. Your progress is saved — you can close this tab.";
      els.serverOfflineBanner.hidden = false;
    } catch (err) {
      els.serverOfflineText.textContent = err.message;
      els.serverOfflineBanner.hidden = false;
    }
  }

  async function loadBootstrap({ initial = false } = {}) {
    const boot = await apiGet("/api/bootstrap");
    state.csrfToken = boot.csrfToken || "";
    state.serverStopped = false;
    els.btnStopServer.disabled = false;
    if (typeof boot.currentYear === "number") {
      state.currentYear = boot.currentYear;
      state.threeYearsAgoYear = boot.threeYearsAgoYear || boot.currentYear - 2;
      els.labelThisYear.textContent = String(state.currentYear);
      els.labelThreeYears.textContent = String(state.threeYearsAgoYear);
    }
    if (boot.checkpoint) {
      state.checkpoint = boot.checkpoint;
    }

    renderTakeoutSummary(boot.takeout);
    renderAuthStatus(boot.auth);
    renderMigrationStatus(boot.migration);

    if (boot.migration && boot.migration.running) {
      if (boot.migration.scope) {
        setSelectedScope(boot.migration.scope);
      }
      goToStep(3, { moveFocus: !initial });
      startMigrationPolling();
      return;
    }
    if (boot.auth && boot.auth.loginWindowOpen) {
      goToStep(2, { moveFocus: !initial });
      startAuthPolling();
      return;
    }
    if (initial && state.takeoutLoaded && state.authenticated) {
      goToStep(3, { moveFocus: false });
    }
  }

  function wireEvents() {
    window.addEventListener("dragover", (ev) => ev.preventDefault());
    window.addEventListener("drop", (ev) => ev.preventDefault());

    els.btnStopServer.addEventListener("click", handleStopServer);
    els.btnReconnect.addEventListener("click", async () => {
      try {
        await loadBootstrap({ initial: false });
      } catch (_err) {
        els.serverOfflineText.textContent =
          "Still unable to reach the local server. Start it again with './.venv/bin/yt-migrate ui'.";
      }
    });

    for (const num of [1, 2, 3]) {
      els.navSteps[num].addEventListener("click", () => {
        if (els.navSteps[num].disabled) {
          return;
        }
        if (state.currentStep === 1 && num > 1 && !validateCustomDates()) {
          showError(els.step1Error, "Please fix the custom date range before continuing.");
          return;
        }
        goToStep(num);
      });
    }

    els.btnChangeFile.addEventListener("click", () => {
      els.takeoutInput.value = "";
      els.uploadBox.hidden = false;
      els.btnCancelUpload.hidden = false;
      els.takeoutInput.click();
    });

    els.btnCancelUpload.addEventListener("click", () => {
      els.uploadBox.hidden = true;
      els.btnCancelUpload.hidden = true;
      showError(els.uploadError, "");
    });

    els.takeoutInput.addEventListener("change", (ev) => {
      const file = ev.target.files && ev.target.files[0];
      uploadFile(file);
    });

    els.dropzone.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      els.dropzone.classList.add("is-dragover");
    });

    els.dropzone.addEventListener("dragleave", (ev) => {
      if (ev.relatedTarget && els.dropzone.contains(ev.relatedTarget)) {
        return;
      }
      els.dropzone.classList.remove("is-dragover");
    });

    els.dropzone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      els.dropzone.classList.remove("is-dragover");
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      uploadFile(file);
    });

    for (const radio of document.querySelectorAll('input[name="scope"]')) {
      radio.addEventListener("change", () => {
        syncCustomDateVisibility();
        if (getSelectedScope() === "custom") {
          refreshCustomDatePreview();
        }
      });
    }

    for (const dateInput of [els.customAfterDate, els.customBeforeDate]) {
      dateInput.addEventListener("change", () => {
        window.clearTimeout(state.customPreviewTimer);
        state.customPreviewTimer = window.setTimeout(refreshCustomDatePreview, 120);
      });
    }

    for (const filterCheckbox of [els.includeMusic, els.excludeShorts, els.retryUnavailable]) {
      filterCheckbox.addEventListener("change", () => refreshTakeoutSummary());
    }

    els.step1Form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      if (!state.takeoutLoaded) {
        showError(els.step1Error, "Please upload your Google Takeout .zip or watch-history file first.");
        return;
      }
      if (!validateCustomDates()) {
        showError(els.step1Error, "Please fix the custom date range before continuing.");
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
          resetProgress: false,
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

    els.btnSwitchAccount.addEventListener("click", handleSwitchAccount);

    els.btnBack1.addEventListener("click", () => goToStep(1));
    els.btnToStep3.addEventListener("click", () => {
      if (!state.authenticated) {
        showError(els.authError, "Please sign in to your new YouTube account first.");
        return;
      }
      goToStep(3);
    });

    els.btnEditScope.addEventListener("click", () => goToStep(1));
    els.btnChangeScopeDone.addEventListener("click", () => goToStep(1));
    els.btnResetProgress.addEventListener("click", handleResetProgress);

    els.btnStartImport.addEventListener("click", () => startImport(null));
    els.btnStopImport.addEventListener("click", async () => {
      try {
        const status = await apiPost("/api/migration/stop", {});
        renderMigrationStatus(status);
      } catch (_err) {
        // Ignore transient stop errors.
      }
    });

    els.btnRunLast3.addEventListener("click", () => startImport("last_3_years"));
    els.btnRunAll.addEventListener("click", () => startImport("all"));
    els.btnReauthStep2.addEventListener("click", () => goToStep(2));
    els.btnDismissError.addEventListener("click", () => {
      els.migrationErrorBanner.hidden = true;
    });
    els.btnBack2.addEventListener("click", () => goToStep(2));

    for (const radio of document.querySelectorAll('input[name="speed"]')) {
      radio.addEventListener("change", () => refreshStep3Summary());
    }

    for (const filterBtn of document.querySelectorAll(".filter-pill")) {
      filterBtn.addEventListener("click", () => {
        state.eventFilter = filterBtn.getAttribute("data-event-filter") || "all";
        const events = (state.migration && state.migration.recentEvents) || [];
        renderRecentEvents(events);
      });
    }
  }

  async function init() {
    wireEvents();
    syncCustomDateVisibility();
    try {
      await loadBootstrap({ initial: true });
    } catch (err) {
      showError(els.step1Error, `Failed to connect to local server: ${err.message}`);
    }
  }

  init();
})();
