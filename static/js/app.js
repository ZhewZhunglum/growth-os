/* PUKO Growth OS — shared client-side interactions.
 * Modules: mobile drawer, async forms, confirm modal, markdown editor.
 */
(function () {
  "use strict";

  /* ──────────────────────────────────────────────
   * 1. Mobile navigation drawer
   * ────────────────────────────────────────────── */
  function initMobileNav() {
    var sidebar = document.querySelector(".sidebar");
    var toggle = document.querySelector(".nav-toggle");
    var overlay = document.querySelector(".nav-overlay");
    if (!sidebar || !toggle) return;

    function openDrawer() {
      sidebar.classList.add("is-open");
      if (overlay) overlay.classList.add("is-visible");
      document.body.style.overflow = "hidden";
      toggle.setAttribute("aria-expanded", "true");
    }
    function closeDrawer() {
      sidebar.classList.remove("is-open");
      if (overlay) overlay.classList.remove("is-visible");
      document.body.style.overflow = "";
      toggle.setAttribute("aria-expanded", "false");
    }

    toggle.addEventListener("click", function () {
      if (sidebar.classList.contains("is-open")) closeDrawer();
      else openDrawer();
    });
    if (overlay) overlay.addEventListener("click", closeDrawer);

    // Close drawer when a nav link is tapped (mobile)
    sidebar.querySelectorAll(".nav-item, .sidebar-user-popover a").forEach(function (el) {
      el.addEventListener("click", function () {
        if (window.matchMedia("(max-width: 850px)").matches) closeDrawer();
      });
    });

    // Close on Escape
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && sidebar.classList.contains("is-open")) closeDrawer();
    });
  }

  /* ──────────────────────────────────────────────
   * 2. Custom confirm modal (replaces window.confirm)
   * ────────────────────────────────────────────── */
  var confirmModalEl = null;

  function ensureConfirmModal() {
    if (confirmModalEl) return confirmModalEl;
    var wrapper = document.createElement("div");
    wrapper.className = "modal-overlay";
    wrapper.setAttribute("role", "dialog");
    wrapper.setAttribute("aria-modal", "true");
    wrapper.innerHTML =
      '<div class="modal-card">' +
      '<h3 class="modal-title"></h3>' +
      '<p class="modal-message"></p>' +
      '<div class="modal-actions">' +
      '<button type="button" class="button button-secondary modal-cancel">取消</button>' +
      '<button type="button" class="button button-danger modal-confirm">确认</button>' +
      '</div></div>';
    document.body.appendChild(wrapper);
    confirmModalEl = wrapper;
    return wrapper;
  }

  function showConfirm(opts) {
    var modal = ensureConfirmModal();
    modal.querySelector(".modal-title").textContent = opts.title || "请确认";
    modal.querySelector(".modal-message").textContent = opts.message || "";
    var confirmBtn = modal.querySelector(".modal-confirm");
    confirmBtn.textContent = opts.confirmText || "确认";
    confirmBtn.className = "button modal-confirm " + (opts.confirmClass || "button-danger");
    modal.classList.add("is-visible");

    return new Promise(function (resolve) {
      function cleanup() {
        modal.classList.remove("is-visible");
        confirmBtn.removeEventListener("click", onConfirm);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onOverlay);
        document.removeEventListener("keydown", onKey);
      }
      function onConfirm() { cleanup(); resolve(true); }
      function onCancel() { cleanup(); resolve(false); }
      function onOverlay(e) { if (e.target === modal) onCancel(); }
      function onKey(e) { if (e.key === "Escape") onCancel(); }

      var cancelBtn = modal.querySelector(".modal-cancel");
      cancelBtn.textContent = opts.cancelText || "取消";
      confirmBtn.addEventListener("click", onConfirm);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onOverlay);
      document.addEventListener("keydown", onKey);
    });
  }

  /* ──────────────────────────────────────────────
   * 3. Async form submission
   * ────────────────────────────────────────────── */
  function initAsyncForms() {
    document.querySelectorAll("form[data-async]").forEach(function (form) {
      if (form.dataset.asyncBound) return;
      form.dataset.asyncBound = "1";
      form.addEventListener("submit", async function (e) {
        e.preventDefault();
        var submitBtn = form.querySelector('button[type="submit"]');
        var originalText = submitBtn ? submitBtn.textContent : "";
        var originalDisabled = submitBtn ? submitBtn.disabled : false;

        // Show loading state
        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.dataset.originalText = originalText;
          submitBtn.innerHTML =
            '<span class="btn-spinner" aria-hidden="true"></span> 处理中…';
        }

        try {
          var formData = new FormData(form);
          var response = await fetch(form.action, {
            method: form.method || "POST",
            body: formData,
            headers: { "X-Requested-With": "XMLHttpRequest" },
            credentials: "same-origin",
          });

          if (response.ok) {
            var contentType = response.headers.get("content-type") || "";
            if (contentType.indexOf("application/json") !== -1) {
              var data = await response.json();
              if (data.redirect) {
                window.location.href = data.redirect;
                return;
              }
              if (data.message) {
                showFlashMessage(data.message, data.level || "success");
              }
              if (data.reload) {
                window.location.reload();
                return;
              }
            } else {
              // Non-JSON success: reload to show server-rendered state
              window.location.reload();
              return;
            }
          } else if (response.status === 400) {
            var errData = null;
            try { errData = await response.json(); } catch (_) {}
            if (errData && errData.errors) {
              showFormErrors(form, errData.errors);
            } else if (errData && errData.message) {
              showFlashMessage(errData.message, "error");
            } else {
              showFlashMessage("提交失败，请检查表单后重试。", "error");
            }
          } else {
            showFlashMessage("服务器错误，请稍后重试。", "error");
          }
        } catch (err) {
          showFlashMessage("网络错误，请检查连接后重试。", "error");
        } finally {
          if (submitBtn) {
            submitBtn.disabled = originalDisabled;
            submitBtn.textContent = submitBtn.dataset.originalText || originalText;
          }
        }
      });
    });
  }

  function showFormErrors(form, errors) {
    // Clear existing errors
    form.querySelectorAll(".async-field-error").forEach(function (el) { el.remove(); });
    Object.keys(errors).forEach(function (fieldName) {
      var field = form.querySelector('[name="' + fieldName + '"]');
      if (!field) return;
      var wrapper = field.closest(".field-row") || field.parentElement;
      var errorEl = document.createElement("div");
      errorEl.className = "async-field-error errorlist";
      errorEl.textContent = errors[fieldName];
      wrapper.appendChild(errorEl);
    });
  }

  function showFlashMessage(message, level) {
    var section = document.querySelector(".messages");
    if (!section) {
      section = document.createElement("section");
      section.className = "messages";
      section.setAttribute("aria-live", "polite");
      var topbar = document.querySelector(".topbar");
      if (topbar && topbar.parentNode) {
        topbar.parentNode.insertBefore(section, topbar.nextSibling);
      } else {
        document.body.insertBefore(section, document.body.firstChild);
      }
    }
    var p = document.createElement("p");
    p.textContent = message;
    if (level === "error") p.style.background = "#fde8e7";
    else if (level === "warning") p.style.background = "#fff4d8";
    section.appendChild(p);
    // Auto-dismiss after 6s
    setTimeout(function () {
      p.style.transition = "opacity 0.5s";
      p.style.opacity = "0";
      setTimeout(function () { if (p.parentNode) p.parentNode.removeChild(p); }, 500);
    }, 6000);
  }

  /* ──────────────────────────────────────────────
   * 4. Markdown editor (lightweight, no external deps)
   * ────────────────────────────────────────────── */
  function initMarkdownEditors() {
    document.querySelectorAll("[data-md-editor]").forEach(function (container) {
      if (container.dataset.mdBound) return;
      container.dataset.mdBound = "1";

      var textarea = container.querySelector("textarea");
      if (!textarea) return;

      // Build editor chrome
      var editorWrap = document.createElement("div");
      editorWrap.className = "md-editor";

      var toolbar = document.createElement("div");
      toolbar.className = "md-toolbar";
      toolbar.innerHTML =
        '<button type="button" data-cmd="bold" title="加粗"><b>B</b></button>' +
        '<button type="button" data-cmd="italic" title="斜体"><i>I</i></button>' +
        '<button type="button" data-cmd="heading" title="标题">H</button>' +
        '<button type="button" data-cmd="link" title="链接">🔗</button>' +
        '<button type="button" data-cmd="list" title="列表">•</button>' +
        '<button type="button" data-cmd="quote" title="引用">"</button>' +
        '<span class="md-spacer"></span>' +
        '<span class="md-word-count">0 字</span>' +
        '<button type="button" class="md-preview-toggle" data-mode="edit">预览</button>';

      var preview = document.createElement("div");
      preview.className = "md-preview";
      preview.style.display = "none";

      // Replace textarea with editor structure
      textarea.parentNode.insertBefore(editorWrap, textarea);
      editorWrap.appendChild(toolbar);
      editorWrap.appendChild(textarea);
      editorWrap.appendChild(preview);
      textarea.classList.add("md-textarea");

      // Word count
      function updateWordCount() {
        var text = textarea.value || "";
        var count = text.replace(/\s/g, "").length;
        toolbar.querySelector(".md-word-count").textContent = count + " 字";
      }
      textarea.addEventListener("input", updateWordCount);
      updateWordCount();

      // Toolbar actions
      toolbar.querySelectorAll("button[data-cmd]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var cmd = btn.dataset.cmd;
          var start = textarea.selectionStart;
          var end = textarea.selectionEnd;
          var selected = textarea.value.substring(start, end);
          var before = textarea.value.substring(0, start);
          var after = textarea.value.substring(end);
          var insertion = "";
          var cursorOffset = 0;

          switch (cmd) {
            case "bold":
              insertion = "**" + (selected || "加粗文字") + "**";
              cursorOffset = selected ? 0 : 2;
              break;
            case "italic":
              insertion = "*" + (selected || "斜体文字") + "*";
              cursorOffset = selected ? 0 : 1;
              break;
            case "heading":
              insertion = "## " + (selected || "标题");
              cursorOffset = selected ? 0 : 3;
              break;
            case "link":
              insertion = "[" + (selected || "链接文字") + "](https://)";
              cursorOffset = selected ? 0 : 1;
              break;
            case "list":
              insertion = "- " + (selected || "列表项");
              cursorOffset = selected ? 0 : 2;
              break;
            case "quote":
              insertion = "> " + (selected || "引用文字");
              cursorOffset = selected ? 0 : 2;
              break;
          }

          textarea.value = before + insertion + after;
          textarea.focus();
          var newPos = start + insertion.length - cursorOffset;
          textarea.setSelectionRange(newPos, newPos + (selected ? 0 : insertion.length - cursorOffset * 2));
          updateWordCount();
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
        });
      });

      // Preview toggle
      var previewBtn = toolbar.querySelector(".md-preview-toggle");
      previewBtn.addEventListener("click", function () {
        if (preview.style.display === "none") {
          preview.innerHTML = renderMarkdown(textarea.value);
          preview.style.display = "block";
          textarea.style.display = "none";
          previewBtn.textContent = "编辑";
          previewBtn.dataset.mode = "preview";
        } else {
          preview.style.display = "none";
          textarea.style.display = "block";
          previewBtn.textContent = "预览";
          previewBtn.dataset.mode = "edit";
          textarea.focus();
        }
      });
    });
  }

  // Minimal Markdown renderer (covers common syntax used in content ops)
  function renderMarkdown(text) {
    if (!text) return '<p class="muted">暂无内容</p>';
    var html = text
      // Escape HTML first
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      // Headings
      .replace(/^### (.*$)/gim, "<h3>$1</h3>")
      .replace(/^## (.*$)/gim, "<h2>$1</h2>")
      .replace(/^# (.*$)/gim, "<h1>$1</h1>")
      // Bold
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      // Italic
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      // Links
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      // Blockquote
      .replace(/^> (.*$)/gim, "<blockquote>$1</blockquote>")
      // Unordered list
      .replace(/^- (.*$)/gim, "<li>$1</li>");

    // Wrap consecutive <li> in <ul>
    html = html.replace(/(<li>.*<\/li>\n?)+/g, function (match) {
      return "<ul>" + match.replace(/\n/g, "") + "</ul>";
    });

    // Paragraphs: split by double newline, wrap non-block elements
    var blocks = html.split(/\n{2,}/);
    var wrapped = blocks.map(function (block) {
      block = block.trim();
      if (!block) return "";
      if (/^<(h[1-6]|ul|ol|blockquote|p)/.test(block)) return block;
      return "<p>" + block.replace(/\n/g, "<br>") + "</p>";
    });
    return wrapped.join("\n");
  }

  /* ──────────────────────────────────────────────
   * 5. data-confirm forms (custom modal)
   * ────────────────────────────────────────────── */
  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      if (form.dataset.confirmBound) return;
      form.dataset.confirmBound = "1";
      form.addEventListener("submit", async function (e) {
        e.preventDefault();
        var confirmed = await showConfirm({
          title: form.dataset.confirmTitle || "请确认",
          message: form.dataset.confirmMessage || "确定要执行此操作吗？",
          confirmText: form.dataset.confirmText || "确认",
          confirmClass: form.dataset.confirmClass || "button-danger",
          cancelText: form.dataset.confirmCancelText || "取消",
        });
        if (confirmed) {
          form.removeEventListener("submit", arguments.callee);
          form.submit();
        }
      });
    });
  }

  /* ──────────────────────────────────────────────
   * 6. Notification polling (every 30s)
   * ────────────────────────────────────────────── */
  function initNotificationPolling() {
    var badge = document.querySelector(".notification-badge");
    if (!badge) return;
    var apiUrl = "/api/notifications/";
    var lastCount = parseInt(badge.textContent, 10) || 0;

    function poll() {
      fetch(apiUrl, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          var count = data.total_count || 0;
          if (count !== lastCount) {
            badge.textContent = count;
            if (count === 0) {
              badge.style.display = "none";
            } else {
              badge.style.display = "";
              // Brief pulse animation on change
              badge.style.transition = "transform .3s";
              badge.style.transform = "scale(1.3)";
              setTimeout(function () { badge.style.transform = "scale(1)"; }, 300);
            }
            lastCount = count;
          }
        })
        .catch(function () { /* silent fail, retry next interval */ });
    }

    setInterval(poll, 30000);
  }

  /* ──────────────────────────────────────────────
   * Boot
   * ────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", function () {
    initMobileNav();
    initAsyncForms();
    initConfirmForms();
    initMarkdownEditors();
    initNotificationPolling();
  });

  // Expose for template inline use
  window.GrowthOS = { showConfirm: showConfirm };
})();
