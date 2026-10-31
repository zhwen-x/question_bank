/* qbank 界面脚本。
 *
 * 刻意不引入任何前端框架或库。界面真正需要 JS 的只有四件事：
 *
 *   1. 编辑器的实时预览（公式/表格/图片要看到渲染结果）
 *   2. 图片粘贴与拖放上传（老师从 Word 里直接 Ctrl+V 是主要录题方式）
 *   3. 选项与小问的动态增删
 *   4. 深浅色偏好
 *
 * 这些加起来一百多行，用原生 JS 写完全够。这样整个应用零前端依赖、
 * 完全离线可用——题库是本地工具，不该因为取不到 CDN 就不能用。
 *
 * 公式渲染不在这里：服务端已经把 LaTeX 转成 MathML，浏览器原生渲染，
 * 不需要 KaTeX 的 JS 与字体文件。
 */

(function () {
  "use strict";

  /* ---------------------------------------------------------- 深浅色 */
  const THEME_KEY = "qbank.theme";

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function readTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "system";
    } catch (_) {
      return "system"; // 隐私模式下 localStorage 会直接抛异常
    }
  }

  function cycleTheme() {
    const order = ["system", "light", "dark"];
    const next = order[(order.indexOf(readTheme()) + 1) % order.length];
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {
      /* 存不下就只对本页生效 */
    }
    applyTheme(next);
    updateThemeButton();
  }

  function updateThemeButton() {
    const button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    const labels = { system: "跟随系统", light: "浅色", dark: "深色" };
    const theme = readTheme();
    button.textContent = labels[theme];
    button.title = "外观：" + labels[theme] + "（点击切换）";
  }

  applyTheme(readTheme());

  /* ------------------------------------------------------ 实时预览 */
  const PREVIEW_DELAY = 250;

  function setupPreview(source) {
    const target = document.getElementById(source.dataset.preview);
    if (!target) return;
    let timer = null;
    let lastSent = null;
    let pending = false;

    async function render() {
      const text = source.value;
      if (text === lastSent) return;
      if (pending) {
        // 上一次还没回来：等它回来后由 finally 里的再次调度处理
        return;
      }
      lastSent = text;
      pending = true;
      try {
        const response = await fetch("/preview", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ text: text }),
        });
        if (!response.ok) throw new Error("预览请求失败 " + response.status);
        target.innerHTML = await response.text();
      } catch (error) {
        target.innerHTML =
          '<p class="placeholder">预览暂时不可用：' +
          String(error.message || error) +
          "</p>";
      } finally {
        pending = false;
        if (source.value !== lastSent) schedule();
      }
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(render, PREVIEW_DELAY);
    }

    source.addEventListener("input", schedule);
    if (source.value.trim()) render();
  }

  /* -------------------------------------------- 图片粘贴 / 拖放上传 */
  async function uploadImage(file) {
    const body = new FormData();
    body.append("file", file, file.name || "pasted.png");
    const response = await fetch("/assets", { method: "POST", body: body });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || "上传失败");
    }
    return response.json();
  }

  function insertAtCursor(textarea, text) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = textarea.value;
    textarea.value = value.slice(0, start) + text + value.slice(end);
    const caret = start + text.length;
    textarea.setSelectionRange(caret, caret);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function imageFilesFrom(items) {
    const files = [];
    for (const item of items || []) {
      if (item.kind === "file" && item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    return files;
  }

  async function absorb(textarea, files) {
    for (const file of files) {
      const marker = "![上传中…]()";
      insertAtCursor(textarea, marker);
      try {
        const asset = await uploadImage(file);
        textarea.value = textarea.value.replace(
          marker,
          "![" + (asset.name || "图") + "](" + asset.ref + ")"
        );
      } catch (error) {
        textarea.value = textarea.value.replace(
          marker,
          "<!-- 图片上传失败：" + String(error.message || error) + " -->"
        );
      }
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  function setupImageDrop(textarea) {
    textarea.addEventListener("paste", function (event) {
      const files = imageFilesFrom(event.clipboardData && event.clipboardData.items);
      if (!files.length) return;
      event.preventDefault();
      absorb(textarea, files);
    });
    textarea.addEventListener("dragover", function (event) {
      if (event.dataTransfer && event.dataTransfer.types.includes("Files")) {
        event.preventDefault();
      }
    });
    textarea.addEventListener("drop", function (event) {
      const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []).filter(
        (file) => file.type.startsWith("image/")
      );
      if (!files.length) return;
      event.preventDefault();
      absorb(textarea, files);
    });
  }

  /* ------------------------------------------------ 动态行（选项/小问） */
  function nextLabel(container) {
    const used = Array.from(container.querySelectorAll(".label-input"))
      .map((input) => input.value.trim().toUpperCase())
      .filter(Boolean);
    for (let code = 65; code < 65 + 26; code += 1) {
      const candidate = String.fromCharCode(code);
      if (!used.includes(candidate)) return candidate;
    }
    return "";
  }

  function renumber(container) {
    container.querySelectorAll("[data-row-number]").forEach((node, index) => {
      node.textContent = String(index + 1);
    });
    // 小问的 hidden 序号也要跟上，否则删掉中间一个后序号会错位
    container.querySelectorAll("[data-row-index]").forEach((node, index) => {
      node.value = String(index);
    });
  }

  function setupRepeater(root) {
    const template = root.querySelector("template");
    const list = root.querySelector("[data-rows]");
    if (!template || !list) return;

    root.querySelectorAll("[data-add-row]").forEach((button) => {
      button.addEventListener("click", function () {
        const fragment = template.content.cloneNode(true);
        const labelInput = fragment.querySelector(".label-input");
        if (labelInput && !labelInput.value) labelInput.value = nextLabel(list);
        list.appendChild(fragment);
        renumber(list);
        const added = list.lastElementChild;
        const focusTarget = added && added.querySelector("textarea, input[type=text]");
        if (focusTarget) focusTarget.focus();
        wire(added);
        // 新行是从 <template> 克隆的，里面的正确项输入还是服务端渲染时的类型；
        // 重新跑一遍题型同步，让它跟当前题型一致
        const typeSelect = document.querySelector("[data-type-switch]");
        if (typeSelect) typeSelect.dispatchEvent(new Event("change"));
      });
    });

    list.addEventListener("click", function (event) {
      const button = event.target.closest("[data-remove-row]");
      if (!button) return;
      const row = button.closest("[data-row]");
      if (row) row.remove();
      renumber(list);
    });
  }

  /* ---------------------------------------------- 题型切换显示对应字段 */

  /* 单选题用 radio、多选题用 checkbox。
   *
   * 不做这个转换的话，从「单选」切到「多选」之后，正确项仍然是一组 radio——
   * 用户只能勾一个，却完全看不出为什么。服务端渲染时只知道初始题型，
   * 所以转换必须在前端跟着题型走。 */
  function syncCorrectInputs(type) {
    const wanted = type === "multiple_choice" ? "checkbox" : "radio";
    const inputs = [...document.querySelectorAll('input[name="correct"]')];
    if (!inputs.length || inputs.every((input) => input.type === wanted)) return;

    // 必须分三步：先记下选中状态并全部清空，再统一改类型，最后回填。
    //
    // 不能边改类型边设 checked——转换过程中会短暂出现「同一个 radio 组里有
    // 多个选中项」这种非法状态，此时把其中一个设为 false，浏览器会把**整组**
    // 都取消掉。表现就是从多选切回单选后一个答案都没了。
    const wasChecked = inputs.map((input) => input.checked);
    inputs.forEach((input) => {
      input.checked = false;
      input.type = wanted;
    });
    if (wanted === "radio") {
      // 单选只能留一个，否则提交上去会被判为「单选题标了多个正确选项」
      const first = wasChecked.indexOf(true);
      if (first >= 0) inputs[first].checked = true;
    } else {
      inputs.forEach((input, index) => {
        input.checked = wasChecked[index];
      });
    }
  }

  function setupTypeSwitch(select) {
    function update() {
      const current = select.value;
      document.querySelectorAll("[data-for-type]").forEach((node) => {
        const allowed = node.dataset.forType.split(/\s+/).filter(Boolean);
        node.hidden = !allowed.includes(current);
        // 隐藏的字段要一并禁用，否则它们仍会被提交，
        // 把上一个题型的残留数据带进校验
        node.querySelectorAll("input, textarea, select").forEach((input) => {
          input.disabled = node.hidden;
        });
      });
      syncCorrectInputs(current);
    }
    select.addEventListener("change", update);
    update();
  }

  /* ------------------------------------------------------ 危险操作确认 */

  /* data-confirm 可以挂在 <form> 上，也可以挂在某一个提交按钮上。

     后者是必需的：批量操作那条工具条里，「应用」和「移入回收站」共用同一个
     表单，确认只能属于其中一个按钮。早先只接了 form 上的，结果挂在按钮上的
     那句确认从不弹出——一个没有任何提示的批量删除。 */
  function setupConfirm(form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }

  function setupConfirmButton(button) {
    button.addEventListener("click", function (event) {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  }

  /* ------------------------------------------------------------- 接线 */
  function wire(scope) {
    const root = scope || document;
    root.querySelectorAll("[data-preview]").forEach(setupPreview);
    root.querySelectorAll("textarea[data-images]").forEach(setupImageDrop);
    root.querySelectorAll("[data-repeater]").forEach(setupRepeater);
    root.querySelectorAll("[data-type-switch]").forEach(setupTypeSwitch);
    root.querySelectorAll("form[data-confirm]").forEach(setupConfirm);
    root.querySelectorAll("button[data-confirm]").forEach(setupConfirmButton);
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire(document);
    updateThemeButton();
    const toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) toggle.addEventListener("click", cycleTheme);
  });
})();

/* 导入页的小交互。写在主 IIFE 之外是因为它们只在这一页用到，
 * 彼此也没有共享状态。 */
(function () {
  "use strict";

  /* 拖放上传：只换个样式并把文件塞进 <input>，提交仍然走普通表单。
   * 不自己发 fetch，这样「没开 JS」时拖放失效但选文件上传照常可用。 */
  function setupDropzone(zone) {
    const input = zone.querySelector("[data-file-input]");
    const nameLabel = zone.querySelector("[data-file-name]");
    if (!input) return;

    function showName() {
      if (!nameLabel) return;
      const file = input.files && input.files[0];
      nameLabel.textContent = file ? file.name : "还没有选择文件";
      nameLabel.style.color = file ? "" : "var(--text-faint)";
    }

    input.addEventListener("change", showName);
    ["dragenter", "dragover"].forEach((name) =>
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.add("over");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      zone.addEventListener(name, () => zone.classList.remove("over"))
    );
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      const files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) {
        input.files = files;
        showName();
      }
    });
    showName();
  }

  /* 换冲突策略时重新加载预览页。
   * 策略会改变每一行的「将要做什么」，必须由服务端重算，不能只改个标签。 */
  function setupReload(select) {
    select.addEventListener("change", function () {
      window.location.href = select.dataset.reloadUrl + encodeURIComponent(select.value);
    });
  }

  /* 勾选工具条。导入向导（name="selected"）与题目挑选（name="question_id"）
     用的是同一套按钮，所以这里按字段名参数化，而不是写两遍。

     已禁用的复选框要跳过：「已在卷中」的那些点「全选」不该把它们也勾上——
     浏览器不会提交 disabled 的字段，于是计数会和实际提交的数量对不上。 */
  function setupBulkSelect(field, counterAttribute) {
    const boxes = () =>
      [...document.querySelectorAll('input[name="' + field + '"]')].filter((b) => !b.disabled);
    const counter = document.querySelector("[" + counterAttribute + "]");
    const plain = counterAttribute === "data-select-count";

    function refresh() {
      if (!counter) return;
      const picked = boxes().filter((b) => b.checked).length;
      counter.textContent = plain
        ? picked
          ? "已选 " + picked + " 道"
          : "一道都没选"
        : String(picked);
    }

    const actions = {
      "data-select-all": () => boxes().forEach((b) => (b.checked = true)),
      "data-select-none": () => boxes().forEach((b) => (b.checked = false)),
      "data-select-new": () =>
        boxes().forEach((b) => (b.checked = b.dataset.new === "1")),
    };
    Object.entries(actions).forEach(([attribute, apply]) => {
      document.querySelectorAll("[" + attribute + "]").forEach((button) => {
        button.addEventListener("click", function () {
          apply();
          refresh();
        });
      });
    });
    boxes().forEach((box) => box.addEventListener("change", refresh));
    refresh();
  }

  /* ------------------------------------------- 组卷蓝图：增删大题行 */

  /* 蓝图的字段名带行下标（section_title_0、section_count_0…）。
     克隆一行时必须把整行的 name 都换成新下标，否则两行同名字段会在提交时
     互相覆盖——页面上完全看不出异常，只是有一个大题莫名消失了。

     删除行留下的下标空号不用管：服务端按下标排序取行，空号不影响顺序。
     这样就省掉了「删一行就把后面全部重新编号」那段 JS，而那段一旦出错，
     症状同样是「某个大题没了」。 */
  function setupSectionRows() {
    const list = document.getElementById("section-rows");
    if (!list) return;
    let nextIndex = 0;
    list.querySelectorAll("[name]").forEach((field) => {
      const found = /_(\d+)$/.exec(field.name);
      if (found) nextIndex = Math.max(nextIndex, Number(found[1]) + 1);
    });

    const addButton = document.getElementById("add-section");
    if (addButton) {
      addButton.addEventListener("click", function () {
        const last = list.lastElementChild;
        if (!last) return;
        const clone = last.cloneNode(true);
        const index = nextIndex++;
        clone.querySelectorAll("[name]").forEach((field) => {
          field.name = field.name.replace(/_\d+$/, "_" + index);
          if (field.type === "checkbox" || field.type === "radio") {
            field.checked = false;
          } else if (field.tagName === "SELECT") {
            Array.from(field.options).forEach((option) => (option.selected = false));
          } else {
            field.value = "";
          }
        });
        list.appendChild(clone);
        const focusTarget = clone.querySelector('input[type="text"]');
        if (focusTarget) focusTarget.focus();
      });
    }

    list.addEventListener("click", function (event) {
      const button = event.target.closest("[data-remove-section]");
      if (!button) return;
      const rows = list.querySelectorAll("[data-section-row]");
      // 一个大题都不剩的蓝图没有意义，而且空表单的报错对用户没有帮助
      if (rows.length <= 1) return;
      const row = button.closest("[data-section-row]");
      if (row) row.remove();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-dropzone]").forEach(setupDropzone);
    document.querySelectorAll("[data-reload-on-change]").forEach(setupReload);
    if (document.querySelector('input[name="selected"]')) {
      setupBulkSelect("selected", "data-selected-count");
    }
    if (document.querySelector('input[name="question_id"]')) {
      setupBulkSelect("question_id", "data-select-count");
    }
    setupSectionRows();
  });
})();
