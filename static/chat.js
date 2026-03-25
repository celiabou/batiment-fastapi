//noinspection SpellCheckingInspection
(function () {
  const EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;
  const PHONE_CANDIDATE_RE = /\+?\d[\d\s().-]{6,}\d/g;
  const ZIP_RE = /\b\d{5}\b/;
  const YES_RE = /\b(oui|ok|yes|d'accord|dac|vas-y|go)\b/i;
  const NO_RE = /\b(non|pas maintenant|plus tard)\b/i;
  const DAY_FMT = new Intl.DateTimeFormat("fr-FR", { weekday: "short" });
  const APP_CONFIG = window.APP_CONFIG || {};
  const AGENDA_URL =
    typeof APP_CONFIG.agenda_url === "string" ? APP_CONFIG.agenda_url.trim() : "";
  const AGENT_PROFILES = [
    {
      name: "Antoine",
      title: "Conseiller renovation",
      photo: "/static/team/antoine-conseiller.jpg",
    },
    {
      name: "Kevin",
      title: "Conseiller renovation",
      photo: "/static/team/kevin-conseiller.jpg",
    },
    {
      name: "Lea",
      title: "Conseiller renovation",
      photo: "/static/team/lea-conseillere.jpg",
    },
  ];

  function pickAgentProfile() {
    const key = "rb_agent_rotation_v1";
    try {
      const current = Number(window.localStorage.getItem(key));
      const nextIndex =
        Number.isFinite(current) && current >= 0
          ? (current + 1) % AGENT_PROFILES.length
          : 0;
      window.localStorage.setItem(key, String(nextIndex));
      return AGENT_PROFILES[nextIndex];
    } catch (_) {
      return AGENT_PROFILES[0];
    }
  }

  const ACTIVE_AGENT = pickAgentProfile();
  const IA_AGENT_NAME = ACTIVE_AGENT.name;
  const IA_AGENT_TITLE = ACTIVE_AGENT.title;
  const IA_AGENT_PHOTO = ACTIVE_AGENT.photo;

  let pendingEstimate = null;
  let leadSent = false;
  let handoffSent = false;
  let handoffPromptShown = false;
  let awaitingHandoffConfirmation = false;
  let agendaPromptShown = false;
  let selectedAgendaSlot = null;

  const btn = document.createElement("button");
  btn.id = "chatBtn";
  btn.textContent = "Estimer mes travaux";
  document.body.appendChild(btn);

  const box = document.createElement("div");
  box.id = "chatBox";
  box.innerHTML = `
    <div id="chatHead">
      <strong>${IA_AGENT_NAME} - ${IA_AGENT_TITLE}</strong>
      <div class="chat-actions">
        <button id="chatHuman" class="chat-head-btn" title="Parler a un conseiller">Conseiller humain</button>
        <button id="chatClose" class="chat-head-btn" title="Fermer">Fermer</button>
      </div>
    </div>
    <div id="chatMsgs"></div>
    <div id="chatAgenda" class="chat-agenda" hidden>
      <div class="chat-agenda-head">
        <div class="chat-agenda-avatar">
          <img src="${IA_AGENT_PHOTO}" alt="${IA_AGENT_NAME} conseiller renovation">
        </div>
        <div class="chat-agenda-agent">
          <strong>${IA_AGENT_NAME}</strong>
          <p class="chat-agenda-role">${IA_AGENT_TITLE}</p>
        </div>
      </div>
      <p class="chat-agenda-intro">Prendre RDV en 10 secondes, top chrono !</p>
      <div id="chatAgendaSlots" class="chat-agenda-slots"></div>
      <div class="chat-agenda-actions">
        <button id="chatAgendaBook" class="chat-agenda-book" type="button">Prendre RDV</button>
        <button id="chatAgendaEstimate" class="chat-head-btn" type="button">Estimer mes travaux</button>
      </div>
      <a id="chatAgendaExternal" class="chat-agenda-link" target="_blank" rel="noopener" hidden>Ouvrir l'agenda complet</a>
    </div>
    <div id="chatFoot">
      <input id="chatInput" placeholder="Ex: salle de bain 6m2 a Saint-Denis" />
      <button id="chatSend" class="chat-send-btn">OK</button>
    </div>
  `;
  document.body.appendChild(box);

  const msgs = box.querySelector("#chatMsgs");
  const input = box.querySelector("#chatInput");
  const humanBtn = box.querySelector("#chatHuman");
  const agendaPanel = box.querySelector("#chatAgenda");
  const agendaSlotsWrap = box.querySelector("#chatAgendaSlots");
  const agendaBookBtn = box.querySelector("#chatAgendaBook");
  const agendaEstimateBtn = box.querySelector("#chatAgendaEstimate");
  const agendaExternalLink = box.querySelector("#chatAgendaExternal");

  let messages = [
    {
      role: "assistant",
      content:
        `Bonjour, je suis ${IA_AGENT_NAME}, ${IA_AGENT_TITLE} et conducteur de projet batiment. Racontez-moi votre projet (type de travaux, surface, ville) et je vous fais un retour clair, humain et concret comme sur chantier.`,
    },
  ];

  function buildAgendaSlots() {
    const start = new Date();
    const hours = [10, 11, 14, 16, 18];
    const slots = [];

    for (let i = 0; i < 5; i += 1) {
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      const hour = hours[i % hours.length];
      const day = DAY_FMT.format(d).replace(/\.$/, ".");
      const dayNum = String(d.getDate());
      const time = `${String(hour).padStart(2, "0")}:00`;
      const dateIso = [
        d.getFullYear(),
        String(d.getMonth() + 1).padStart(2, "0"),
        String(d.getDate()).padStart(2, "0"),
      ].join("-");
      slots.push({
        id: `slot-${i}`,
        day,
        dayNum,
        time,
        iso: `${dateIso} ${time}`,
        label: `${day} ${dayNum} a ${time}`,
      });
    }
    return slots;
  }

  const agendaSlots = buildAgendaSlots();
  if (agendaSlots.length) selectedAgendaSlot = agendaSlots[0];

  function renderAgendaSlots() {
    if (!agendaSlotsWrap) return;
    agendaSlotsWrap.innerHTML = "";
    agendaSlots.forEach((slot) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chat-slot" + (selectedAgendaSlot && selectedAgendaSlot.id === slot.id ? " active" : "");
      b.innerHTML = `<span>${slot.day}</span><strong>${slot.dayNum}</strong><small>${slot.time}</small>`;
      b.addEventListener("click", () => {
        selectedAgendaSlot = slot;
        renderAgendaSlots();
      });
      agendaSlotsWrap.appendChild(b);
    });
  }

  function openAgendaPanel() {
    agendaPanel.hidden = false;
    if (AGENDA_URL) {
      agendaExternalLink.hidden = false;
      agendaExternalLink.href = AGENDA_URL;
    } else {
      agendaExternalLink.hidden = true;
      agendaExternalLink.removeAttribute("href");
    }
    renderAgendaSlots();
    if (!agendaPromptShown) {
      const context = extractContext();
      const bits = [];
      if (context.work_type) bits.push(context.work_type);
      if (context.surface) bits.push(`${context.surface} m2`);
      if (context.city) bits.push(context.city);
      const contextLine = bits.length
        ? `Je garde le fil de votre dossier (${bits.join(", ")}). `
        : "Je garde le fil de votre projet. ";
      pushMessage(
        "assistant",
        `${contextLine}Choisissez un creneau puis cliquez sur Prendre RDV. Je transmets le contexte technique au conducteur de projet.`
      );
      agendaPromptShown = true;
      render();
    }
  }

  function closeAgendaPanel() {
    agendaPanel.hidden = true;
  }

  function pushMessage(role, content) {
    const last = messages[messages.length - 1];
    if (last && last.role === role && last.content === content) return;
    messages.push({ role, content });
  }

  function isAffirmative(text) {
    const t = (text || "").trim();
    return YES_RE.test(t) && t.length <= 40;
  }

  function isNegative(text) {
    const t = (text || "").trim();
    return NO_RE.test(t) && t.length <= 50;
  }

  function extractContact(text) {
    const email = (text.match(EMAIL_RE) || [null])[0];
    const candidates = text.match(PHONE_CANDIDATE_RE) || [];
    let phone = null;
    for (const c of candidates) {
      const digits = c.replace(/\D/g, "");
      if (digits.length >= 8 && digits.length <= 15) {
        phone = c.trim();
        break;
      }
    }
    return { email, phone };
  }

  function extractAllContacts() {
    const userTexts = messages.filter((m) => m.role === "user").map((m) => m.content);
    for (const text of userTexts.slice().reverse()) {
      const c = extractContact(text);
      if (c.phone || c.email) return c;
    }
    return { phone: null, email: null };
  }

  function firstMatch(list, re) {
    for (const item of list) {
      const m = item.match(re);
      if (m && m[0]) return m[0];
    }
    return null;
  }

  function inferWorkType(text) {
    const t = text.toLowerCase();
    if (t.includes("copropriete") || t.includes("parties communes")) return "copropriete";
    if (t.includes("commerce") || t.includes("boutique") || t.includes("local commercial")) return "commerce";
    if (t.includes("bureaux") || t.includes("bureau")) return "bureaux";
    if (t.includes("appartement")) return "appartement";
    if (t.includes("maison") || t.includes("pavillon")) return "maison";
    if (t.includes("salle de bain")) return "salle de bain";
    if (t.includes("cuisine")) return "cuisine";
    if (t.includes("peinture")) return "peinture";
    if (t.includes("toiture")) return "toiture";
    if (t.includes("isolation")) return "isolation";
    if (t.includes("electricite") || t.includes("electrique") || t.includes("electric")) return "electricite";
    if (t.includes("plomberie")) return "plomberie";
    if (t.includes("renov")) return "renovation";
    return null;
  }

  function extractContext() {
    const userTexts = messages.filter((m) => m.role === "user").map((m) => m.content);
    const all = userTexts.join(" ");
    const surfaceMatch = all.match(
      /(\d+(?:[.,]\d+)?)\s*(?:m[2²]|metres?\s*carres?|m[eè]tres?\s*carr[eé]s?|carr[eé]s?)\b/i
    );
    const cityMatch =
      all.match(/\b[àa]\s+([A-Za-zÀ-ÖØ-öø-ÿ' -]{2,})/i) ||
      all.match(
        /\b(?:maison|appartement|bureaux?|commerce|copropriete)\b[^0-9]{0,20}\d+(?:[.,]\d+)?\s*(?:m[2²]|carr[eé]s?)\s+([A-Za-zÀ-ÖØ-öø-ÿ' -]{2,})/i
      );
    const zip = firstMatch(userTexts, ZIP_RE);

    return {
      city: cityMatch ? cityMatch[1].trim().replace(/\b(?:budget|sous|travaux)\b.*$/i, "").trim() : null,
      postal_code: zip,
      surface: surfaceMatch ? surfaceMatch[1].replace(",", ".") : null,
      work_type: inferWorkType(all),
      raw_message: messages.map((m) => `${m.role}: ${m.content}`).join("\n"),
    };
  }

  function getTrackingContext() {
    try {
      if (window.RB_TRACKING && typeof window.RB_TRACKING.getContext === "function") {
        return window.RB_TRACKING.getContext() || {};
      }
    } catch (_) {
      // ignore tracking runtime failures
    }
    return {};
  }

  async function maybeSendLead() {
    if (leadSent || !pendingEstimate) return;

    const contact = extractAllContacts();
    if (!contact.phone && !contact.email) return;

    const context = extractContext();
    const tracking = getTrackingContext();
    const payload = {
      name: null,
      phone: contact.phone,
      email: contact.email,
      city: context.city,
      postal_code: context.postal_code,
      surface: context.surface,
      work_type: context.work_type,
      estimate_min: pendingEstimate.min,
      estimate_max: pendingEstimate.max,
      raw_message: context.raw_message,
      visitor_id: tracking.visitor_id || null,
      visitor_landing: tracking.visitor_landing || null,
      visitor_referrer: tracking.visitor_referrer || null,
      visitor_utm: tracking.visitor_utm || {},
    };

    try {
      const res = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        leadSent = true;
        pushMessage(
          "assistant",
          "Parfait. Votre demande est bien prise en compte. Si besoin, je peux transferer vers un conseiller humain tout de suite."
        );
        handoffPromptShown = true;
        awaitingHandoffConfirmation = true;
        render();
      }
    } catch (_) {
      // keep chat responsive even if persistence fails
    }
  }

  async function sendHandoff(reason, successMessage) {
    if (handoffSent) {
      pushMessage(
        "assistant",
        "Votre demande conseiller est deja envoyee. Un conseiller vous recontacte rapidement."
      );
      render();
      return true;
    }

    const contact = extractAllContacts();
    if (!contact.phone && !contact.email) {
      pushMessage(
        "assistant",
        "Pour transferer a un conseiller, indiquez votre telephone ou email dans le chat."
      );
      render();
      return false;
    }

    const context = extractContext();
    const tracking = getTrackingContext();
    const payload = {
      source: "chat_widget",
      priority: "high",
      reason: reason || "demande manuelle conseiller humain",
      name: null,
      phone: contact.phone,
      email: contact.email,
      city: context.city,
      postal_code: context.postal_code,
      surface: context.surface,
      work_type: context.work_type,
      estimate_min: pendingEstimate ? pendingEstimate.min : null,
      estimate_max: pendingEstimate ? pendingEstimate.max : null,
      conversation: context.raw_message,
      visitor_id: tracking.visitor_id || null,
      visitor_landing: tracking.visitor_landing || null,
      visitor_referrer: tracking.visitor_referrer || null,
      visitor_utm: tracking.visitor_utm || {},
    };

    try {
      const res = await fetch("/api/handoff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        handoffSent = true;
        handoffPromptShown = true;
        awaitingHandoffConfirmation = false;
        pushMessage(
          "assistant",
          successMessage || `C'est fait. Votre demande prioritaire #${data.id} est transmise a un conseiller humain.`
        );
        render();
        return true;
      } else {
        pushMessage(
          "assistant",
          "Le transfert humain a echoue. Reessayez dans quelques secondes."
        );
        render();
        return false;
      }
    } catch (_) {
      pushMessage(
        "assistant",
        "Le transfert humain a echoue. Reessayez dans quelques secondes."
      );
      render();
      return false;
    }
  }

  async function bookAgenda() {
    if (!selectedAgendaSlot) {
      pushMessage("assistant", "Selectionnez un creneau avant de valider le RDV.");
      render();
      return;
    }

    const ok = await sendHandoff(
      `rdv agenda ${selectedAgendaSlot.iso}`,
      `C'est valide. RDV demande pour ${selectedAgendaSlot.label}. Je transmets votre contexte chantier au conducteur de projet pour un appel utile, point par point.`
    );

    if (ok && AGENDA_URL) {
      pushMessage(
        "assistant",
        "Vous pouvez aussi ouvrir l'agenda complet avec le lien dans le bloc conseiller."
      );
      render();
    }
  }

  function render() {
    msgs.innerHTML = "";
    messages.forEach((m) => {
      const div = document.createElement("div");
      div.className = "msg" + (m.role === "user" ? " user" : "");
      div.textContent = m.content;
      msgs.appendChild(div);
    });
    msgs.scrollTop = msgs.scrollHeight;
  }

  async function send() {
    const text = (input.value || "").trim();
    if (!text) return;
    input.value = "";

    pushMessage("user", text);
    render();

    if (!handoffSent && awaitingHandoffConfirmation && isAffirmative(text)) {
      await sendHandoff("confirmation utilisateur");
      return;
    }
    if (awaitingHandoffConfirmation && isNegative(text)) {
      awaitingHandoffConfirmation = false;
      pushMessage("assistant", "Tres bien. Je continue l'estimation technique avec vous.");
      render();
      return;
    }

    await maybeSendLead();

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages,
        agent_name: IA_AGENT_NAME,
        agent_role: IA_AGENT_TITLE,
      }),
    });

    const data = await res.json();

    if (data && data.estimate && data.estimate.min && data.estimate.max) {
      pendingEstimate = { min: data.estimate.min, max: data.estimate.max };
    }

    pushMessage("assistant", data.reply || "Erreur. Reessayez.");

    // If estimate is obtained in this turn and contact already exists in previous messages,
    // persist the lead immediately.
    await maybeSendLead();

    if (data && data.hybrid && data.hybrid.suggest_handoff && !handoffSent && !handoffPromptShown) {
      pushMessage(
        "assistant",
        "Souhaitez-vous etre rappele par un conseiller humain maintenant ? Cliquez sur le bouton Conseiller humain."
      );
      handoffPromptShown = true;
      awaitingHandoffConfirmation = true;
    }

    render();
  }

  btn.onclick = () => {
    box.style.display = "block";
    render();
  };

  box.querySelector("#chatClose").onclick = () => {
    box.style.display = "none";
  };

  box.querySelector("#chatSend").onclick = send;
  humanBtn.onclick = () => {
    box.style.display = "block";
    openAgendaPanel();
  };
  agendaBookBtn.onclick = bookAgenda;
  agendaEstimateBtn.onclick = () => {
    closeAgendaPanel();
    input.focus();
  };

  window.addEventListener("open-chat-agenda", () => {
    box.style.display = "block";
    openAgendaPanel();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send();
  });

  renderAgendaSlots();
  render();
})();
