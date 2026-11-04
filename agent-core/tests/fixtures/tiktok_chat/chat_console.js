// A tiny imitation of the TikTok LIVE Console chat, for tests/test_chat_sender_playwright.py.
// Each *.html sets window.CHAT_FLAGS, then loads this file:
//   duplicate_rows  the source comment is shown twice      no_reply     rows have no Reply button
//   no_mention      typing "@name" shows no suggestions    two_composers  two chat textboxes
//   disabled        chat turned off                        notice       "too fast" toast instead of sending
//   enter_noop      Enter does nothing (only Send works)   keep_text    Enter sends but the box keeps the text
//   no_echo         sent messages never appear in the list
(function () {
  const F = window.CHAT_FLAGS || {};
  const viewers = [
    {nick: "Lan", user: "lan.nguyen"}, {nick: "Lan 2", user: "lan.nguyen2"}, {nick: "Hoa", user: "hoa.tran"}];
  const rows = [
    {nick: "Lan", text: "55kg mặc size gì"},
    {nick: "Hoa", text: "ship bao lâu vậy shop, quá nhanh không"},
    {nick: "Minh", text: "bình luận đã bị tắt à?"},
  ];
  if (F.duplicate_rows) rows.push({nick: "Lan", text: "55kg mặc size gì"});
  window.SENT = [];
  window.ENTER_COUNT = 0;
  window.SUBMIT_COUNT = 0;

  const list = document.createElement("div");
  list.setAttribute("data-e2e", "chat-list");
  document.body.appendChild(list);

  function addRow(row, own) {
    const el = document.createElement("div");
    el.setAttribute("data-e2e", "chat-message");
    const author = document.createElement("span");
    author.className = "nickname";
    author.textContent = row.nick;
    el.appendChild(author);
    if (own) {
      const badge = document.createElement("span");
      badge.setAttribute("data-e2e", "chat-self");
      badge.textContent = "Bạn";
      el.appendChild(badge);
    }
    const text = document.createElement("span");
    text.className = "comment-content";
    text.textContent = row.text;
    el.appendChild(text);
    if (!F.no_reply && !own) {
      const reply = document.createElement("button");
      reply.textContent = "Trả lời";
      reply.addEventListener("click", () => {
        const box = document.querySelector("textarea");
        box.dataset.replyTo = row.nick;
        document.getElementById("reply-banner").textContent = "Đang trả lời " + row.nick;
        box.focus();
      });
      el.appendChild(reply);
    }
    list.appendChild(el);
  }
  rows.forEach((r) => addRow(r, false));

  const banner = document.createElement("div");
  banner.id = "reply-banner";
  document.body.appendChild(banner);

  function composer() {
    const wrap = document.createElement("div");
    wrap.className = "chat-input";
    const box = document.createElement("textarea");
    box.setAttribute("placeholder", "Thêm bình luận...");
    if (F.disabled) box.disabled = true;
    const send = document.createElement("button");
    send.textContent = "Gửi";
    wrap.appendChild(box);
    wrap.appendChild(send);
    document.body.appendChild(wrap);
    box.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      window.ENTER_COUNT += 1;
      if (!F.enter_noop) submit(box, !F.keep_text);
    });
    box.addEventListener("input", () => suggest(box));
    send.addEventListener("click", () => submit(box, true));
  }
  composer();
  if (F.two_composers) composer();
  if (F.disabled) {
    const off = document.createElement("div");
    off.textContent = "Bình luận đã bị tắt";
    document.body.appendChild(off);
  }

  const popup = document.createElement("ul");
  popup.setAttribute("role", "listbox");
  popup.style.display = "none";
  document.body.appendChild(popup);

  function suggest(box) {
    const m = /@([\w.]+)$/.exec(box.value);
    popup.innerHTML = "";
    if (F.no_mention || !m) { popup.style.display = "none"; return; }
    const hits = viewers.filter((v) => v.user.startsWith(m[1]));
    hits.forEach((v) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.textContent = v.nick + " @" + v.user;
      li.addEventListener("click", () => {
        box.value = box.value.replace(/@([\w.]+)$/, "@" + v.user + " ");
        box.dataset.mention = v.user;
        popup.style.display = "none";
        box.focus();
      });
      popup.appendChild(li);
    });
    popup.style.display = hits.length ? "block" : "none";
  }

  function submit(box, clear) {
    const text = box.value.trim();
    if (!text) return;
    window.SUBMIT_COUNT += 1;
    if (F.notice) {
      const toast = document.createElement("div");
      toast.className = "toast";
      toast.textContent = "Bạn bình luận quá nhanh, vui lòng thử lại sau";
      document.body.appendChild(toast);
      return;
    }
    window.SENT.push({text, replyTo: box.dataset.replyTo || null, mention: box.dataset.mention || null});
    if (!F.no_echo) addRow({nick: "Sarae Shop", text}, true);
    if (clear) {
      box.value = "";
      delete box.dataset.replyTo;
      delete box.dataset.mention;
      document.getElementById("reply-banner").textContent = "";
    }
  }
})();
