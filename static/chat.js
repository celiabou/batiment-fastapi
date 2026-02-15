(function () {
  const btn = document.createElement("button");
  btn.id = "chatBtn";
  btn.textContent = "Estimer mes travaux";
  document.body.appendChild(btn);

  const box = document.createElement("div");
  box.id = "chatBox";
  box.innerHTML = `
    <div id="chatHead">
      <strong>Assistant devis</strong>
      <button id="chatClose" style="border:0;background:transparent;cursor:pointer;">Fermer</button>
    </div>
    <div id="chatMsgs"></div>
    <div id="chatFoot">
      <input id="chatInput" placeholder="Ex: salle de bain 6m² à Saint-Denis" />
      <button id="chatSend" style="border:1px solid #111;border-radius:12px;padding:8px 10px;background:#fff;cursor:pointer;">OK</button>
    </div>
  `;
  document.body.appendChild(box);

  const msgs = box.querySelector("#chatMsgs");
  const input = box.querySelector("#chatInput");

  let messages = [{ role: "assistant", content: "Bonjour 👋 Décrivez vos travaux, je vous donne une estimation en 1 minute." }];

  function render() {
    msgs.innerHTML = "";
    messages.forEach(m => {
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
    messages.push({ role: "user", content: text });
    render();

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages })
    });
    const data = await res.json();
    messages.push({ role: "assistant", content: data.reply || "Erreur. Réessayez." });
    render();
  }

  btn.onclick = () => { box.style.display = "block"; render(); };
  box.querySelector("#chatClose").onclick = () => { box.style.display = "none"; };
  box.querySelector("#chatSend").onclick = send;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

  render();
})();
