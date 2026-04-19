//noinspection SpellCheckingInspection
(function () {
  /**
   * @typedef {{status?: string, message?: string}} BudgetFit
   * @typedef {{label?: string, detail?: string, share_percent?: number, low_label?: string, high_label?: string}} QuoteBreakdownItem
   * @typedef {{
   *   estimate_mode?: string,
   *   estimate_mode_label?: string,
   *   low_label?: string,
   *   high_label?: string,
   *   pricing_context?: string,
   *   project_type_label?: string,
   *   scope_label?: string,
   *   budget_fit?: BudgetFit,
   *   breakdown?: QuoteBreakdownItem[],
   *   assumptions?: string[]
   * }} SmartQuote
   * @typedef {{client_email_sent?: boolean, internal_email_sent?: boolean, client_email?: string}} DeliveryStatus
   * @typedef {{
   *   ok?: boolean,
   *   error?: string,
   *   details?: string[],
   *   message?: string,
   *   handoff_id?: string|number,
   *   prequote_url?: string,
   *   account_optional?: boolean,
   *   quote?: SmartQuote,
   *   delivery?: DeliveryStatus,
   *   source_photos?: string[],
   *   renders?: string[]
   * }} ApiPayload
   */

  /** @type {HTMLFormElement|null} */
  const quoteForm = document.getElementById("aiQuoteForm");
  /** @type {HTMLFormElement|null} */
  const renderForm = document.getElementById("aiRenderForm");
  if (!quoteForm) return;
  const hasRenderForm = Boolean(renderForm);

  /** @type {HTMLButtonElement|null} */
  const quoteSubmitBtn = document.getElementById("aiQuoteSubmit");
  /** @type {HTMLButtonElement|null} */
  const renderSubmitBtn = document.getElementById("aiRenderSubmit");

  const quoteStatusEl = document.getElementById("aiQuoteStatus");
  const renderStatusEl = document.getElementById("aiRenderStatus");
  const quoteDeliveryStatusEl = document.getElementById("aiQuoteDeliveryStatus");
  const renderDeliveryStatusEl = document.getElementById("aiRenderDeliveryStatus");
  const quotePdfLinkEl = document.getElementById("aiQuotePdfLink");

  const quoteRangeEl = document.getElementById("smartQuoteRange");
  const quoteMetaEl = document.getElementById("smartQuoteMeta");
  const quoteBudgetEl = document.getElementById("smartQuoteBudget");
  const quoteBreakdownEl = document.getElementById("smartQuoteBreakdown");
  const quoteAssumptionsEl = document.getElementById("smartQuoteAssumptions");
  const quoteAccountEl = document.getElementById("smartQuoteAccount");

  const renderTitleEl = document.getElementById("smartRenderTitle");
  const renderSummaryEl = document.getElementById("smartRenderSummary");

  const liveFields = {
    work_item_key: document.getElementById("liveWorkItem"),
    work_quantity: document.getElementById("liveQuantity"),
    work_unit: document.getElementById("liveQuantity"),
    project_type: document.getElementById("liveType"),
    scope: document.getElementById("liveScope"),
    style: document.getElementById("liveStyle"),
    surface: document.getElementById("liveSurface"),
    rooms: document.getElementById("liveRooms"),
    budget: document.getElementById("liveBudget"),
    timeline: document.getElementById("liveTimeline"),
    city: document.getElementById("liveCity"),
  };
  const liveSavedHint = document.getElementById("liveSavedHint");
  const DRAFT_KEY = "rb_quote_draft_v1";

  const sourceGalleryEl = document.getElementById("ai3dSource");
  const rendersGalleryEl = document.getElementById("ai3dRenders");

  /** @type {HTMLInputElement|null} */
  const renderPhotosInput = document.getElementById("renderPhotosInput");
  const renderPreviewEl = document.getElementById("aiRenderPhotoPreview");
  /** @type {HTMLInputElement|null} */
  const renderHandoffHidden = document.getElementById("aiRenderHandoffId");
  /** @type {HTMLInputElement|null} */
  const renderHandoffManual = document.getElementById("aiRenderHandoffManual");
  const backToQuoteBtn = document.getElementById("ai3dBackToQuoteForm");

  // Photo preview state (accessible from submitQuote for form reset)
  const quotePhotosPreview = document.getElementById("quotePhotosPreview");
  let quotePhotosSelectedFiles = [];

  const workItemSelects = Array.from(document.querySelectorAll(".js-work-item"));
  const workItemBlocks = Array.from(document.querySelectorAll(".js-work-item-block"));
  const workQtyInputs = Array.from(document.querySelectorAll(".js-work-qty"));
  const workUnitTags = Array.from(document.querySelectorAll(".js-work-unit"));
  const workQtyHelpTexts = Array.from(document.querySelectorAll(".js-work-qty-help"));
  const workQtyLabelTexts = Array.from(document.querySelectorAll(".js-work-qty-label-text"));
  const surfaceHelpTexts = Array.from(document.querySelectorAll(".js-surface-help"));
  const estimateModeHints = Array.from(document.querySelectorAll(".js-estimate-mode-hint"));
  const workUnitHidden = document.getElementById("workUnitHidden");
  const workUnitOverride = document.getElementById("workUnitOverride");
  const SCOPE_WORK_ITEM_ONLY = "par_choix_prestation";

  let quoteSubmitted = false;
  let quoteSubmitting = false;
  let renderSubmitting = false;
  let currentHandoffId = "";

  function readFormSnapshot() {
    const formData = new FormData(quoteForm);
    return {
      project_type: formData.get("project_type") || "",
      scope: formData.get("scope") || "",
      style: formData.get("style") || "",
      finishing_level: formData.get("finishing_level") || "",
      work_item_key: formData.get("work_item_key") || "",
      work_quantity: formData.get("work_quantity") || "",
      work_unit: formData.get("work_unit") || "",
      city: formData.get("city") || "",
      surface: formData.get("surface") || "",
      rooms: formData.get("rooms") || "",
      budget: formData.get("budget") || "",
      timeline: formData.get("timeline") || "",
      notes: formData.get("notes") || "",
      name: formData.get("name") || "",
      phone: formData.get("phone") || "",
      email: formData.get("email") || "",
    };
  }

  function formatBudget(value) {
    const clean = String(value || "").trim();
    if (!clean) return "--";
    const num = Number(clean.replace(/\s+/g, "").replace(",", "."));
    if (!Number.isFinite(num)) return clean;
    return Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(num) + " €";
  }

  function formatQuantity(value) {
    const raw = String(value || "").trim();
    if (!raw) return "--";
    const num = Number(raw.replace(",", "."));
    if (!Number.isFinite(num)) return raw;
    const formatted = num % 1 === 0 ? num.toString() : num.toString();
    return formatted.replace(".", ",");
  }

  function updateLiveRecap() {
    const snap = readFormSnapshot();
    if (liveFields.work_item_key) {
      const selected = workItemSelects[0]?.selectedOptions?.[0];
      const label = selected ? selected.textContent : "";
      liveFields.work_item_key.textContent = label || snap.work_item_key || "--";
    }
    if (liveFields.work_quantity) {
      const qty = formatQuantity(snap.work_quantity);
      const unit = snap.work_unit ? ` ${snap.work_unit}` : "";
      liveFields.work_quantity.textContent = qty !== "--" ? `${qty}${unit}` : "--";
    }
    if (liveFields.project_type) liveFields.project_type.textContent = snap.project_type || "--";
    if (liveFields.scope) liveFields.scope.textContent = snap.scope || "--";
    if (liveFields.style) liveFields.style.textContent = snap.style || "--";
    if (liveFields.surface) liveFields.surface.textContent = snap.surface ? `${snap.surface} m2` : "--";
    if (liveFields.rooms) liveFields.rooms.textContent = snap.rooms || "--";
    if (liveFields.budget) liveFields.budget.textContent = snap.budget ? formatBudget(snap.budget) : "--";
    if (liveFields.timeline) liveFields.timeline.textContent = snap.timeline || "--";
    if (liveFields.city) liveFields.city.textContent = snap.city || "--";
  }

  function saveDraft() {
    try {
      const snap = readFormSnapshot();
      localStorage.setItem(DRAFT_KEY, JSON.stringify(snap));
      if (liveSavedHint) liveSavedHint.textContent = "Brouillon sauvegardé localement.";
    } catch (e) {
      if (liveSavedHint) liveSavedHint.textContent = "Sauvegarde locale indisponible.";
    }
  }

  function loadDraft() {
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return;
      const snap = JSON.parse(raw);
      Object.entries(snap || {}).forEach(([key, val]) => {
        const control = quoteForm.elements.namedItem(key);
        writeControlValue(control, String(val || ""));
      });
      syncWorkItemUnits();
      updateLiveRecap();
      if (liveSavedHint) liveSavedHint.textContent = "Brouillon chargé.";
    } catch (e) {
      // ignore
    }
  }

  /**
   * @param {Element|null} el
   * @param {string|number|null|undefined} value
   */
  function setText(el, value) {
    if (!el) return;
    el.textContent = String(value ?? "");
  }

  /**
   * @param {Response} res
   * @returns {Promise<ApiPayload>}
   */
  function readJsonSafely(res) {
    const contentType = (res.headers.get("content-type") || "").toLowerCase();
    if (!contentType.includes("application/json")) {
      return res.text().then((txt) => ({
        ok: false,
        error: "Réponse serveur invalide.",
        details: [txt.slice(0, 220)],
      }));
    }
    return res.json().then((raw) => {
      if (!raw || typeof raw !== "object") {
        return {
          ok: false,
          error: "Réponse JSON invalide.",
        };
      }
      return /** @type {ApiPayload} */ (raw);
    });
  }

  /**
   * @param {unknown} value
   * @returns {string[]}
   */
  function asStringArray(value) {
    if (!Array.isArray(value)) return [];
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }

  /**
   * @param {RadioNodeList|Element|null} entry
   * @returns {string}
   */
  function readControlValue(entry) {
    const control = asFormControl(entry);
    if (!control) return "";
    if (control instanceof RadioNodeList) return String(control.value || "");
    return String(control.value || "");
  }

  /**
   * @param {RadioNodeList|Element|null} entry
   * @returns {RadioNodeList|HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|null}
   */
  function asFormControl(entry) {
    if (!entry) return null;
    if (entry instanceof RadioNodeList) return entry;
    if (entry instanceof HTMLInputElement) return entry;
    if (entry instanceof HTMLSelectElement) return entry;
    if (entry instanceof HTMLTextAreaElement) return entry;
    return null;
  }

  /**
   * @param {RadioNodeList|Element|null} entry
   * @param {string} value
   */
  function writeControlValue(entry, value) {
    const control = asFormControl(entry);
    if (!control) return;
    if (control instanceof RadioNodeList) {
      control.value = value;
      return;
    }
    control.value = value;
  }

  function appendTracking(formData) {
    if (!window.RB_TRACKING || typeof window.RB_TRACKING.getContext !== "function") {
      return;
    }
    const tracking = window.RB_TRACKING.getContext() || {};
    if (tracking.visitor_id) formData.set("visitor_id", tracking.visitor_id);
    if (tracking.visitor_landing) formData.set("visitor_landing", tracking.visitor_landing);
    if (tracking.visitor_referrer) formData.set("visitor_referrer", tracking.visitor_referrer);
    if (tracking.visitor_utm) formData.set("visitor_utm", JSON.stringify(tracking.visitor_utm));
  }

  /**
   * @param {Element|null} container
   * @param {unknown} images
   * @param {string} titlePrefix
   */
  function renderGallery(container, images, titlePrefix) {
    if (!container) return;
    container.innerHTML = "";

    const seen = new Set();
    const list = [];
    asStringArray(images).forEach((src) => {
      const key = String(src || "").trim();
      if (!key || seen.has(key)) return;
      seen.add(key);
      list.push(key);
    });

    list.forEach((src, idx) => {
      const article = document.createElement("article");
      article.className = "ai3d-card";

      const img = document.createElement("img");
      img.src = src;
      img.alt = `${titlePrefix} ${idx + 1}`;
      img.loading = "lazy";

      const caption = document.createElement("p");
      caption.textContent = `${titlePrefix} ${idx + 1}`;

      article.appendChild(img);
      article.appendChild(caption);
      container.appendChild(article);
    });
  }

  function renderPhotoPreview() {
    if (!renderPreviewEl || !renderPhotosInput) return;
    const files = Array.from(renderPhotosInput.files || /** @type {File[]} */ ([]));
    renderPreviewEl.innerHTML = "";
    if (!files.length) return;

    const seen = new Set();
    files.forEach((fileRaw) => {
      if (!(fileRaw instanceof File)) return;
      const file = fileRaw;
      const key = `${file.name}-${file.size}-${file.lastModified}`;
      if (seen.has(key)) return;
      seen.add(key);

      const article = document.createElement("article");
      article.className = "ai3d-card";

      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      img.alt = `Photo sélectionnée ${file.name}`;
      img.loading = "lazy";

      const caption = document.createElement("p");
      caption.textContent = file.name;
      article.appendChild(img);
      article.appendChild(caption);
      renderPreviewEl.appendChild(article);
    });
  }

  /**
   * @param {SmartQuote|null|undefined} quote
   */
  function renderQuote(quote) {
    if (!quote || !quoteRangeEl || !quoteMetaEl || !quoteBudgetEl || !quoteBreakdownEl || !quoteAssumptionsEl) {
      return;
    }

    setText(quoteRangeEl, `${quote.low_label || ""} - ${quote.high_label || ""}`);
    const metaParts = [];
    if (quote.estimate_mode_label) metaParts.push(quote.estimate_mode_label);
    if (quote.pricing_context) metaParts.push(quote.pricing_context);
    else {
      if (quote.project_type_label) metaParts.push(quote.project_type_label);
      if (quote.scope_label) metaParts.push(quote.scope_label);
    }
    setText(quoteMetaEl, metaParts.join(" • ") || "Calcul catalogue en attente.");

    /** @type {BudgetFit} */
    const budgetFit = quote.budget_fit || {};
    quoteBudgetEl.textContent = budgetFit.message || "Budget non analysé.";
    quoteBudgetEl.className = `ai3d-budget-hint ai3d-budget-${budgetFit.status || "unknown"}`;

    quoteBreakdownEl.innerHTML = "";
    (quote.breakdown || []).forEach((item) => {
      const row = document.createElement("div");
      row.className = "ai3d-breakdown-row";

      const left = document.createElement("span");
      left.className = "ai3d-breakdown-left";
      const hasShare = Number.isFinite(Number(item.share_percent));
      const label = item.label || "Poste";
      const detail = item.detail ? ` • ${item.detail}` : "";
      left.textContent = hasShare ? `${label} (${Number(item.share_percent || 0)}%)${detail}` : `${label}${detail}`;

      const right = document.createElement("span");
      right.className = "ai3d-breakdown-right";
      right.textContent = `${item.low_label || "?"} - ${item.high_label || "?"}`;

      row.appendChild(left);
      row.appendChild(right);
      quoteBreakdownEl.appendChild(row);
    });

    quoteAssumptionsEl.innerHTML = "";
    asStringArray(quote.assumptions).forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      quoteAssumptionsEl.appendChild(li);
    });
  }

  /**
   * @param {Element|null} el
   * @param {DeliveryStatus|null|undefined} delivery
   * @param {string|undefined} successText
   */
  function renderDelivery(el, delivery, successText) {
    if (!el) return;
    const clientDone = Boolean(delivery && delivery.client_email_sent);
    const internalDone = Boolean(delivery && delivery.internal_email_sent);
    const clientEmail = (delivery && delivery.client_email) || "email client";

    if (clientDone && internalDone) {
      el.textContent = successText || `Envoyé: client (${clientEmail}) + copie interne.`;
      el.className = "ai3d-budget-hint ai3d-budget-aligned";
      return;
    }
    if (clientDone && !internalDone) {
      el.textContent = `Client envoyé (${clientEmail}), copie interne en attente.`;
      el.className = "ai3d-budget-hint ai3d-budget-over_budget";
      return;
    }
    if (!clientDone && internalDone) {
      el.textContent = "Copie interne envoyée, envoi client en attente.";
      el.className = "ai3d-budget-hint ai3d-budget-under_budget";
      return;
    }
    el.textContent = "Envois email en cours de vérification.";
    el.className = "ai3d-budget-hint ai3d-budget-under_budget";
  }

  function copyQuoteToRenderPrefill() {
    if (!renderForm) return;
    const fields = ["project_type", "scope", "style", "timeline", "city", "surface", "rooms", "budget", "name", "phone", "email", "notes"];
    fields.forEach((key) => {
      const source = quoteForm.elements.namedItem(key);
      const target = renderForm.elements.namedItem(key);
      if (!source || !target) return;
      writeControlValue(target, readControlValue(source));
    });
  }

  function currentRenderDossierId() {
    const manual = String((renderHandoffManual && renderHandoffManual.value) || "").trim();
    if (manual) return manual;
    return String(currentHandoffId || "").trim();
  }

  function refreshRenderEligibility() {
    if (!hasRenderForm) return;
    if (!renderSubmitBtn) return;
    const canRequest = quoteSubmitted || Boolean(currentRenderDossierId());
    renderSubmitBtn.disabled = renderSubmitting || !canRequest;
    if (canRequest) {
      setText(renderStatusEl, "Rendu 3D prêt sur demande. Téléversez plusieurs photos puis lancez.");
    } else {
      setText(renderStatusEl, "Posez d'abord l'estimation, puis demandez le rendu 3D.");
    }
  }

  function syncWorkItemUnits() {
    const modeHint = "Type de travaux sert au cadrage. Le calcul est effectue uniquement depuis le poste catalogue selectionne.";
    const surfaceContextHelp = "Champ contextuel (optionnel): non utilise pour le calcul catalogue du pre-devis.";
    const itemHelpText = "A renseigner uniquement si vous choisissez une prestation precise. Exemple : 12 m2 de cloison, 3 portes, 8 ml de plan de travail. Ce champ ne correspond pas a la surface totale du bien.";
    estimateModeHints.forEach((el) => {
      el.textContent = modeHint;
    });
    surfaceHelpTexts.forEach((el) => {
      el.textContent = surfaceContextHelp;
    });
    workItemBlocks.forEach((block) => {
      block.hidden = false;
    });

    function qtyLabelFor(unit, hasItem) {
      if (!hasItem) return "Quantite du poste selectionne";
      if (unit === "m2") return "Surface a traiter (m2)";
      if (unit === "ml") return "Longueur estimee (ml)";
      if (unit === "unite") return "Nombre d'unites";
      if (unit === "forfait") return "Forfait - aucune quantite a renseigner";
      return "Quantite du poste selectionne";
    }

    workItemSelects.forEach((selectEl, idx) => {
      const selected = selectEl.options[selectEl.selectedIndex];
      const hasWorkItem = Boolean(selected && selected.value);
      const autoUnit = hasWorkItem ? selected.getAttribute("data-unit") || "--" : "--";
      const overrideUnit = (hasWorkItem && workUnitOverride) ? workUnitOverride.value : "";
      const unit = overrideUnit || autoUnit || "--";
      const unitTag = workUnitTags[idx];
      const qtyInput = workQtyInputs[idx];
      const helpText = workQtyHelpTexts[idx];
      const qtyLabel = workQtyLabelTexts[idx];
      if (qtyLabel) qtyLabel.textContent = qtyLabelFor(unit, hasWorkItem);
      selectEl.required = true;
      selectEl.disabled = false;
      if (unitTag) unitTag.textContent = unit;
      if (workUnitHidden) workUnitHidden.value = unit;
      if (helpText) {
        helpText.textContent = !hasWorkItem
          ? "Selectionnez d'abord un poste de travaux dans la grille catalogue."
          : unit === "forfait"
          ? "Forfait - aucune quantite a renseigner."
          : itemHelpText;
      }
      if (qtyInput) {
        qtyInput.disabled = !hasWorkItem || unit === "forfait" || unit === "--";
        qtyInput.required = hasWorkItem && unit !== "forfait" && unit !== "--";
        if (unit === "forfait") {
          qtyInput.value = "";
          qtyInput.placeholder = "N/A";
        } else if (unit === "--") {
          qtyInput.placeholder = "Ex: 80";
        } else {
          qtyInput.placeholder = unit === "unite" ? "Ex: 4" : "Ex: 80";
        }
      }
    });
  }

  async function submitQuote(event) {
    event.preventDefault();
    if (quoteSubmitting) return;
    quoteSubmitting = true;
    if (quoteSubmitBtn) quoteSubmitBtn.disabled = true;
    setText(quoteStatusEl, "Envoi de l'estimation en cours...");

    try {
      const formData = new FormData(quoteForm);
      const workItemValue = String(formData.get("work_item_key") || "").trim();
      if (!workItemValue) {
        setText(quoteStatusEl, "Selectionnez un poste de travaux dans la grille catalogue pour lancer l'estimation.");
        return;
      }
      appendTracking(formData);
      const res = await fetch("/api/devis-intelligent", {
        method: "POST",
        body: formData,
      });
      /** @type {ApiPayload} */
      const data = await readJsonSafely(res);

      if (!res.ok || !data || !data.ok) {
        const err = data.error || "Erreur lors de la generation de l'estimation.";
        const details = Array.isArray(data.details) && data.details.length
          ? ` Details: ${data.details.join(" | ")}`
          : "";
        if (res.status === 503) {
          setText(
            quoteStatusEl,
            `${err} Lancez ./run_gmail.sh puis renseignez le mot de passe d'application Gmail.${details}`
          );
        } else {
          setText(quoteStatusEl, `${err}${details}`);
        }
        return;
      }

      quoteSubmitted = true;
      currentHandoffId = data.handoff_id ? String(data.handoff_id) : "";
      if (renderHandoffHidden) renderHandoffHidden.value = currentHandoffId;
      if (renderHandoffManual && currentHandoffId) renderHandoffManual.value = currentHandoffId;
      try {
        localStorage.removeItem(DRAFT_KEY);
        if (liveSavedHint) liveSavedHint.textContent = "Brouillon envoyé.";
      } catch (e) {
        // ignore
      }

      copyQuoteToRenderPrefill();
      renderQuote(data.quote || null);
      /** @type {DeliveryStatus} */
      const quoteDelivery = data.delivery || {};
      renderDelivery(
        quoteDeliveryStatusEl,
        quoteDelivery,
        `Pre-devis envoye au client (${quoteDelivery.client_email || "email client"}) + copie interne.`
      );

      if (quotePdfLinkEl) {
        const prequoteUrl = String(data.prequote_url || "").trim();
        if (prequoteUrl) {
          quotePdfLinkEl.hidden = false;
          quotePdfLinkEl.setAttribute("href", prequoteUrl);
          quotePdfLinkEl.textContent = "Telecharger mon pre-devis PDF";
        } else {
          quotePdfLinkEl.hidden = true;
          quotePdfLinkEl.removeAttribute("href");
        }
      }

      if (quoteAccountEl) {
        if (data.project_saved) {
          quoteAccountEl.textContent = "Votre dossier a été sauvegardé dans votre espace client.";
          quoteAccountEl.className = "ai3d-budget-hint ai3d-budget-aligned";
        } else {
          quoteAccountEl.textContent = "Votre pre-devis est deja envoye par email. Creer un espace client reste optionnel.";
          quoteAccountEl.className = "ai3d-budget-hint";
        }
      }

      setText(quoteStatusEl, data.message || "Estimation envoyee.");
      // Clear the form after successful submission
      try {
        quoteForm.reset();
        if (quotePhotosPreview) {
          quotePhotosPreview.innerHTML = "";
          quotePhotosSelectedFiles = [];
        }
        updateLiveRecap();
      } catch (e) {
        // ignore
      }
      if (hasRenderForm) {
        setText(renderTitleEl, currentHandoffId ? `Dossier #${currentHandoffId} pret` : "Dossier devis pret");
        setText(renderSummaryEl, "Le devis est pose. Vous pouvez maintenant demander le rendu 3D avec plusieurs photos.");
        refreshRenderEligibility();
      }
    } catch (err) {
      const isNetworkError = err && err.name === "TypeError";
      if (isNetworkError) {
        setText(
          quoteStatusEl,
          "Connexion impossible au serveur local. Vérifiez que le serveur est démarré puis réessayez."
        );
      } else {
        setText(quoteStatusEl, `Erreur reseau estimation. (${(err && err.message) || "inconnue"})`);
      }
    } finally {
      quoteSubmitting = false;
      if (quoteSubmitBtn) quoteSubmitBtn.disabled = false;
    }
  }

  async function submitRender(event) {
    if (!renderForm) return;
    event.preventDefault();
    if (renderSubmitting) return;

    const selectedFiles = renderPhotosInput ? Array.from(renderPhotosInput.files || []) : [];
    if (!selectedFiles.length) {
      setText(renderStatusEl, "Ajoutez au moins une photo du bâtiment pour le rendu 3D.");
      return;
    }

    const dossierId = currentRenderDossierId();
    if (!quoteSubmitted && !dossierId) {
      setText(renderStatusEl, "Posez d'abord une estimation, ou indiquez un numero de dossier devis.");
      return;
    }

    renderSubmitting = true;
    refreshRenderEligibility();
    setText(renderStatusEl, "Génération du rendu 3D sur demande en cours...");

    try {
      const formData = new FormData(renderForm);
      if (dossierId) {
        formData.set("handoff_id", dossierId);
      }
      appendTracking(formData);

      const res = await fetch("/api/rendu-3d-sur-demande", {
        method: "POST",
        body: formData,
      });
      /** @type {ApiPayload} */
      const data = await readJsonSafely(res);

      if (!res.ok || !data || !data.ok) {
        const err = data.error || "Erreur lors de la génération du rendu 3D.";
        const details = Array.isArray(data.details) && data.details.length
          ? ` Details: ${data.details.join(" | ")}`
          : "";
        if (res.status === 503) {
          setText(
            renderStatusEl,
            `${err} Renseignez OPENAI_API_KEY et SMTP_PASSWORD puis relancez.${details}`
          );
        } else {
          setText(renderStatusEl, `${err}${details}`);
        }
        return;
      }

      if (data.handoff_id) {
        currentHandoffId = String(data.handoff_id);
        if (renderHandoffHidden) renderHandoffHidden.value = currentHandoffId;
        if (renderHandoffManual && !renderHandoffManual.value.trim()) {
          renderHandoffManual.value = currentHandoffId;
        }
      }

      if (data.quote) {
        renderQuote(data.quote);
      }
      renderGallery(sourceGalleryEl, asStringArray(data.source_photos), "Photo source");
      renderGallery(rendersGalleryEl, asStringArray(data.renders), "Rendu 3D");
      /** @type {DeliveryStatus} */
      const renderDeliveryData = data.delivery || {};
      renderDelivery(
        renderDeliveryStatusEl,
        renderDeliveryData,
        `Rendu 3D envoyé au client (${renderDeliveryData.client_email || "email client"}) + copie interne.`
      );
      setText(renderStatusEl, data.message || "Rendu 3D sur demande envoyé.");
      setText(renderTitleEl, "Rendu 3D envoyé");
      setText(renderSummaryEl, "Les visuels 3D ont été transmis au client et à l'équipe interne.");
    } catch (err) {
      const isNetworkError = err && err.name === "TypeError";
      if (isNetworkError) {
        setText(
          renderStatusEl,
          "Connexion impossible au serveur local. Vérifiez que le serveur est démarré puis réessayez."
        );
      } else {
        setText(renderStatusEl, `Erreur réseau rendu 3D. (${(err && err.message) || "inconnue"})`);
      }
    } finally {
      renderSubmitting = false;
      refreshRenderEligibility();
    }
  }

  quoteForm.addEventListener("submit", submitQuote);
  if (renderForm) {
    renderForm.addEventListener("submit", submitRender);
  }

  if (renderPhotosInput) {
    renderPhotosInput.addEventListener("change", renderPhotoPreview);
  }
  if (renderHandoffManual) {
    renderHandoffManual.addEventListener("input", refreshRenderEligibility);
  }
  if (backToQuoteBtn) {
    backToQuoteBtn.addEventListener("click", function () {
      quoteForm.scrollIntoView({ behavior: "smooth", block: "start" });
      const firstField = quoteForm.querySelector('select[name="project_type"]');
      if (firstField && typeof firstField.focus === "function") firstField.focus();
    });
  }

  workItemSelects.forEach((selectEl) => {
    selectEl.addEventListener("change", syncWorkItemUnits);
  });
  const quoteScopeSelect = quoteForm.querySelector('select[name="scope"]');
  if (quoteScopeSelect) {
    quoteScopeSelect.addEventListener("change", syncWorkItemUnits);
  }
  if (workUnitOverride) {
    workUnitOverride.addEventListener("change", () => {
      syncWorkItemUnits();
      updateLiveRecap();
      saveDraft();
    });
  }
  quoteForm.addEventListener("input", () => {
    updateLiveRecap();
    saveDraft();
  });
  quoteForm.addEventListener("change", () => {
    updateLiveRecap();
    saveDraft();
  });

  setText(quoteDeliveryStatusEl, "Aucun pre-devis envoye pour l'instant.");
  setText(renderDeliveryStatusEl, "Aucun rendu 3D envoye pour l'instant.");
  if (quotePdfLinkEl) {
    quotePdfLinkEl.hidden = true;
    quotePdfLinkEl.removeAttribute("href");
  }
  syncWorkItemUnits();
  loadDraft();
  updateLiveRecap();
  refreshRenderEligibility();

  // Photo preview with add/remove support (event delegation for remove buttons)
  const quotePhotosInput = document.getElementById("quotePhotosInput");
  if (quotePhotosInput && quotePhotosPreview) {
    // Use event delegation for remove buttons (they are added async via FileReader)
    quotePhotosPreview.addEventListener("click", function (e) {
      var btn = e.target.closest(".remove-photo");
      if (!btn) return;
      var idx = parseInt(btn.getAttribute("data-index"));
      if (isNaN(idx)) return;
      quotePhotosSelectedFiles.splice(idx, 1);
      updateInputFiles();
      renderPreview();
    });

    function renderPreview() {
      quotePhotosPreview.innerHTML = "";
      quotePhotosSelectedFiles.forEach(function (file, i) {
        if (!file.type.startsWith("image/")) return;
        const reader = new FileReader();
        reader.onload = function (e) {
          const div = document.createElement("div");
          div.className = "photo-preview-item";
          div.innerHTML =
            '<img src="' + e.target.result + '" alt="' + file.name + '">' +
            '<button type="button" class="remove-photo" data-index="' + i + '">&times;</button>';
          quotePhotosPreview.appendChild(div);
        };
        reader.readAsDataURL(file);
      });
    }

    function updateInputFiles() {
      var dt = new DataTransfer();
      quotePhotosSelectedFiles.forEach(function (f) { dt.items.add(f); });
      quotePhotosInput.files = dt.files;
    }

    quotePhotosInput.addEventListener("change", function () {
      var newFiles = Array.from(this.files || []);
      quotePhotosSelectedFiles = quotePhotosSelectedFiles.concat(newFiles);
      updateInputFiles();
      renderPreview();
      // Reset input so same file can be re-added if removed
      this.value = "";
    });

    renderPreview();
  }
})();
