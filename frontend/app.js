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

// /chat stays the reliable, simpler primary path; streaming is attempted
// first and falls back to it silently (see sendMessageStreaming) whenever
// the stream can't be established at all, so flipping this to false is
// always a safe way to disable streaming without touching anything else.
const USE_STREAMING = true;

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

function addStreamingBubble() {
  // Replaces the typing indicator once the first token arrives: progressively
  // appearing text *is* the "still working on it" signal while streaming.
  removeTyping();
  const message = document.createElement("div");
  message.className = "message assistant-message";
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = "TRENDLY ASSIST";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  message.append(label, bubble);
  chat.append(message);
  scrollToLatest();
  return bubble;
}

function updateStreamingBubble(bubble, fullText) {
  bubble.innerHTML = formatReply(fullText);
  scrollToLatest();
}

function setSending(value) {
  sending = value;
  sendButton.disabled = value;
  suggestions.forEach((button) => { button.disabled = value; });
}

/**
 * Attempts /chat/stream. Returns true if the turn was fully (or at least
 * partially) delivered via streaming — in which case the caller must not
 * also fall back to /chat, which would duplicate the reply. Returns false
 * only when nothing was ever shown to the customer, meaning it's safe for
 * the caller to retry the whole turn through the plain /chat endpoint.
 */
async function sendMessageStreaming(message) {
  let bubble = null;
  let fullText = "";

  try {
    const response = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Session-ID": sessionId },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
    if (!response.ok || !response.body) return false;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";
      for (const event of events) {
        if (!event.startsWith("data: ")) continue;
        const raw = event.slice(6);
        if (raw === "[DONE]") continue;
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (error) {
          continue;
        }
        if (typeof payload.token === "string") {
          if (!bubble) bubble = addStreamingBubble();
          fullText += payload.token;
          updateStreamingBubble(bubble, fullText);
        } else if (payload.error) {
          if (!bubble) return false; // nothing shown yet — safe to fall back to /chat
          fullText += "\n\nI’m having trouble connecting right now. Please try again in a moment.";
          updateStreamingBubble(bubble, fullText);
        }
      }
    }
    return bubble !== null;
  } catch (error) {
    return bubble !== null; // something already shown; do not also fall back
  }
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
    if (USE_STREAMING && (await sendMessageStreaming(message))) return;
    // Streaming disabled, or failed before showing anything — fall back to /chat.
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Session-ID": sessionId },
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
  const oldSessionId = sessionId;
  // Fire-and-forget: clearing server-side session state must never block or
  // fail the UI reset, so errors are swallowed and nothing here is awaited.
  fetch(`/chat/${oldSessionId}`, { method: "DELETE" }).catch(() => {});
  sessionId = newSessionId();
  chat.innerHTML = '<div class="message assistant-message intro-message"><span class="message-label">TRENDLY ASSIST</span><div class="bubble">New conversation, fresh start. How can I help with Trendly today?</div></div>';
  field.focus();
});

fetch("/health")
  .then((response) => { if (!response.ok) throw new Error(); status.textContent = "Support is online"; })
  .catch(() => { status.textContent = "Service unavailable"; document.querySelector(".signal").style.background = "#d47a70"; });
