const chat = document.querySelector("#chat-scroll");
const form = document.querySelector("#chat-form");
const field = document.querySelector("#message");
const sendButton = document.querySelector("#send-button");
const status = document.querySelector("#service-status");
const resetButton = document.querySelector("#new-chat");
const suggestions = [...document.querySelectorAll("[data-prompt]")];

const newSessionId = () => `web-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
let sessionId = newSessionId();
let sending = false;

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function formatReply(value) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

function scrollToLatest() {
  chat.scrollTop = chat.scrollHeight;
}

function addMessage(role, content) {
  const message = document.createElement("div");
  message.className = `message ${role}-message`;
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = role === "user" ? "YOU" : "TRENDLY ASSIST";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") bubble.innerHTML = formatReply(content);
  else bubble.textContent = content;
  message.append(label, bubble);
  chat.append(message);
  scrollToLatest();
}

function addTyping() {
  const message = document.createElement("div");
  message.className = "message assistant-message typing";
  message.id = "typing";
  message.innerHTML = '<span class="message-label">TRENDLY ASSIST</span><div class="bubble"><i></i><i></i><i></i></div>';
  chat.append(message);
  scrollToLatest();
}

function removeTyping() {
  document.querySelector("#typing")?.remove();
}

function setSending(value) {
  sending = value;
  sendButton.disabled = value;
  suggestions.forEach((button) => { button.disabled = value; });
}

async function sendMessage(text) {
  const message = text.trim();
  if (!message || sending) return;
  addMessage("user", message);
  field.value = "";
  autoResize();
  setSending(true);
  addTyping();
  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "We couldn't process that message.");
    addMessage("assistant", data.reply);
  } catch (error) {
    addMessage("assistant", "I’m having trouble connecting right now. Please try again in a moment.");
  } finally {
    removeTyping();
    setSending(false);
    field.focus();
  }
}

function autoResize() {
  field.style.height = "auto";
  field.style.height = `${Math.min(field.scrollHeight, 110)}px`;
}

form.addEventListener("submit", (event) => { event.preventDefault(); sendMessage(field.value); });
field.addEventListener("input", autoResize);
field.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
});
suggestions.forEach((button) => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
resetButton.addEventListener("click", () => {
  sessionId = newSessionId();
  chat.innerHTML = '<div class="message assistant-message intro-message"><span class="message-label">TRENDLY ASSIST</span><div class="bubble">New conversation, fresh start. How can I help with Trendly today?</div></div>';
  field.focus();
});

fetch("/health")
  .then((response) => { if (!response.ok) throw new Error(); status.textContent = "Support is online"; })
  .catch(() => { status.textContent = "Service unavailable"; document.querySelector(".signal").style.background = "#d47a70"; });
