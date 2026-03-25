//noinspection SpellCheckingInspection
(function () {
  /**
   * @typedef {{min?: number, max?: number}} DurationWeeks
   * @typedef {{status?: string, message?: string}} BudgetFit
   * @typedef {{label?: string, share_percent?: number, low_label?: string, high_label?: string}} QuoteBreakdownItem
   * @typedef {{
   *   low_label?: string,
   *   high_label?: string,
   *   duration_weeks?: DurationWeeks,
   *   confidence?: number,
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
  if (!quoteForm || !renderForm) return;

  /** @type {HTMLButtonElement|null} */
  const quoteSubmitBtn = document.getElementById("aiQuoteSubmit");
  /** @type {HTMLButtonElement|null} */
  const renderSubmitBtn = document.getElementById("aiRenderSubmit");

  const quoteStatusEl = document.getElementById("aiQuoteStatus");
  const renderStatusEl = document.getElementById("aiRenderStatus");
  const quoteDeliveryStatusEl = document.getElementById("aiQuoteDeliveryStatus");
  const renderDeliveryStatusEl = document.getElementById("aiRenderDeliveryStatus");

  const quoteRangeEl = document.getElementById("smartQuoteRange");
  const quoteMetaEl = document.getElementById("smartQuoteMeta");
  const quoteBudgetEl = document.getElementById("smartQuoteBudget");
  const quoteBreakdownEl = document.getElementById("smartQuoteBreakdown");
  const quoteAssumptionsEl = document.getElementById("smartQuoteAssumptions");

  const renderTitleEl = document.getElementById("smartRenderTitle");
  const renderSummaryEl = document.getElementById("smartRenderSummary");

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

  let quoteSubmitted = false;
  let quoteSubmitting = false;
  let renderSubmitting = false;
  let currentHandoffId = "";

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
    /** @type {DurationWeeks} */
    const duration = quote.duration_weeks || {};
    setText(
      quoteMetaEl,
      `Confiance ${Math.round((quote.confidence || 0) * 100)}% • ${quote.project_type_label || ""} • ${quote.scope_label || ""} • Délai ${duration.min || "?"}-${duration.max || "?"} semaines`
    );

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
      left.textContent = `${item.label || "Poste"} (${Number(item.share_percent || 0)}%)`;

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
    if (!renderSubmitBtn) return;
    const canRequest = quoteSubmitted || Boolean(currentRenderDossierId());
    renderSubmitBtn.disabled = renderSubmitting || !canRequest;
    if (canRequest) {
      setText(renderStatusEl, "Rendu 3D prêt sur demande. Téléversez plusieurs photos puis lancez.");
    } else {
      setText(renderStatusEl, "Posez d'abord le devis intelligent, puis demandez le rendu 3D.");
    }
  }

  async function submitQuote(event) {
    event.preventDefault();
    if (quoteSubmitting) return;
    quoteSubmitting = true;
    if (quoteSubmitBtn) quoteSubmitBtn.disabled = true;
    setText(quoteStatusEl, "Envoi du devis intelligent en cours...");

    try {
      const formData = new FormData(quoteForm);
      appendTracking(formData);
      const res = await fetch("/api/devis-intelligent", {
        method: "POST",
        body: formData,
      });
      /** @type {ApiPayload} */
      const data = await readJsonSafely(res);

      if (!res.ok || !data || !data.ok) {
        const err = data.error || "Erreur lors de la génération du devis.";
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

      copyQuoteToRenderPrefill();
      renderQuote(data.quote || null);
      /** @type {DeliveryStatus} */
      const quoteDelivery = data.delivery || {};
      renderDelivery(
        quoteDeliveryStatusEl,
        quoteDelivery,
        `Devis envoyé au client (${quoteDelivery.client_email || "email client"}) + copie interne.`
      );

      setText(quoteStatusEl, data.message || "Devis intelligent envoyé.");
      setText(renderTitleEl, currentHandoffId ? `Dossier #${currentHandoffId} prêt` : "Dossier devis prêt");
      setText(renderSummaryEl, "Le devis est posé. Vous pouvez maintenant demander le rendu 3D avec plusieurs photos.");
      refreshRenderEligibility();
    } catch (err) {
      const isNetworkError = err && err.name === "TypeError";
      if (isNetworkError) {
        setText(
          quoteStatusEl,
          "Connexion impossible au serveur local. Vérifiez que le serveur est démarré puis réessayez."
        );
      } else {
        setText(quoteStatusEl, `Erreur réseau devis. (${(err && err.message) || "inconnue"})`);
      }
    } finally {
      quoteSubmitting = false;
      if (quoteSubmitBtn) quoteSubmitBtn.disabled = false;
    }
  }

  async function submitRender(event) {
    event.preventDefault();
    if (renderSubmitting) return;

    const selectedFiles = renderPhotosInput ? Array.from(renderPhotosInput.files || []) : [];
    if (!selectedFiles.length) {
      setText(renderStatusEl, "Ajoutez au moins une photo du bâtiment pour le rendu 3D.");
      return;
    }

    const dossierId = currentRenderDossierId();
    if (!quoteSubmitted && !dossierId) {
      setText(renderStatusEl, "Posez d'abord un devis, ou indiquez un numéro de dossier devis.");
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
  renderForm.addEventListener("submit", submitRender);

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

  setText(quoteDeliveryStatusEl, "Aucun email envoyé pour l'instant.");
  setText(renderDeliveryStatusEl, "Aucun rendu 3D envoyé pour l'instant.");
  refreshRenderEligibility();
})();
