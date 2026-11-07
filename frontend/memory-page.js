// 页面负责编辑和按钮操作；真正保存哪条、当前应用哪条，都由后端决定。
export function createMemoryPage({ api, mutate, toast, leave, onChange }) {
  const $ = (id) => document.getElementById(id);
  let catalog = { memories: [], active_memory_id: null, revision: 0, writable: false };
  let selected = null, base = null, editRevision = 0, busy = false, loading = false;
  // base 是打开编辑器时的原内容；对比输入框，判断有没有未保存的修改。
  const dirty = () => Boolean(base && ($("memory-name").value !== base.name || $("memory-content").value !== base.content));

  function error(message = "") {
    $("memory-error").textContent = message;
    $("memory-error").hidden = !message;
  }

  function controls() {
    const disabled = busy || !catalog.writable;
    $("memory-new").disabled = disabled;
    $("memory-reload").disabled = busy;
    $("memory-name").disabled = disabled;
    $("memory-content").disabled = disabled;
    $("memory-save").disabled = disabled || !base || !dirty();
    $("memory-apply").disabled = disabled || !base || dirty() || selected === catalog.active_memory_id;
    $("memory-delete").disabled = disabled || !base;
    $("memory-clear").disabled = disabled || !catalog.active_memory_id;
    $("memory-editor-state").textContent = !catalog.writable ? "当前为只读浏览模式。"
      : dirty() ? "有未保存修改；保存后才能应用。"
      : selected === catalog.active_memory_id ? "当前正在使用；保存修改后，下一次模型请求使用新内容。" : "已保存，尚未应用。";
    $("memory-page").setAttribute("aria-busy", String(busy || loading));
  }

  function renderList() {
    const active = catalog.memories.find((item) => item.id === catalog.active_memory_id);
    $("memory-active-status").textContent = active ? `当前应用：${active.name}` : "当前未应用任何记忆";
    const list = $("memory-list");
    list.replaceChildren();
    for (const item of catalog.memories) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "memory-list-item";
      button.dataset.memoryId = item.id;
      button.setAttribute("aria-current", String(item.id === selected));
      button.disabled = busy;
      const title = document.createElement("strong");
      title.textContent = item.name;
      const note = document.createElement("small");
      note.textContent = item.id === catalog.active_memory_id ? "正在使用" : "未应用";
      button.append(title, note);
      button.addEventListener("click", () => { if (canLeave()) choose(item.id); });
      list.append(button);
    }
    if (!catalog.memories.length) list.textContent = "暂无记忆，点击新建开始。";
  }

  function choose(id) {
    // 这里只把内容放进编辑器，不调用“应用”接口。
    base = catalog.memories.find((item) => item.id === id) || null;
    selected = base?.id || null;
    editRevision = catalog.revision;
    $("memory-form").hidden = !base;
    $("memory-empty").hidden = Boolean(base);
    $("memory-name").value = base?.name || "";
    $("memory-content").value = base?.content || "";
    error();
    renderList();
    controls();
  }

  function canLeave() {
    // 换条目或返回聊天前，先保护尚未保存的编辑内容。
    if (busy) { toast("正在保存记忆，请等待操作返回。"); return false; }
    if (!dirty()) return true;
    if (!window.confirm("有未保存的记忆修改，确定放弃这些修改吗？")) return false;
    choose(selected);
    return true;
  }

  async function refresh() {
    if (busy || loading) return;
    loading = true;
    controls();
    try {
      const data = await api("/api/memories");
      // 保存期间或收到较旧的响应时，不用旧数据覆盖当前页面状态。
      if (busy || data.revision < catalog.revision) return;
      const changed = catalog.revision !== data.revision;
      const keepDraft = dirty();
      catalog = data;
      if (!keepDraft) choose(selected || catalog.memories[0]?.id);
      else {
        // 别人改过记忆时，只更新列表，保留正在编辑的草稿和原编辑版本。
        renderList();
        if (changed) error("记忆列表已更新，编辑草稿已保留。保存冲突时请重新加载并核对修改。");
      }
    } catch (cause) { error(cause.message); }
    finally { loading = false; controls(); }
  }

  async function write(path, body, method = "POST", nextSelection = selected) {
    // 等后端确认保存成功后，再更新页面上的内容和“正在使用”标识。
    if (busy || !catalog.writable) return;
    busy = true;
    error();
    renderList();
    controls();
    try {
      const result = await mutate(path, body, method);
      catalog = { ...result, writable: catalog.writable };
      choose(nextSelection === "new" ? result.memory_id : nextSelection);
      toast("记忆设置已保存，从下一次模型请求生效。");
      await onChange();
    } catch (cause) { error(cause.message); }
    finally { busy = false; renderList(); controls(); }
  }

  $("memory-new").addEventListener("click", () => {
    if (canLeave()) write("/api/memories", { name: "未命名记忆", content: "", expected_memory_revision: catalog.revision }, "POST", "new");
  });
  $("memory-reload").addEventListener("click", () => {
    if (canLeave()) { choose(selected); refresh(); }
  });
  $("memory-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!base || !dirty()) return;
    write(`/api/memories/${encodeURIComponent(selected)}`, { name: $("memory-name").value,
      content: $("memory-content").value, expected_memory_revision: editRevision }, "PUT");
  });
  $("memory-apply").addEventListener("click", () => {
    if (base && !dirty()) write("/api/memories/active", { memory_id: selected, expected_memory_revision: catalog.revision });
  });
  $("memory-clear").addEventListener("click", () => {
    if (canLeave()) write("/api/memories/active", { memory_id: null, expected_memory_revision: catalog.revision });
  });
  $("memory-delete").addEventListener("click", () => {
    if (base && window.confirm(`确定删除记忆「${base.name}」吗？删除正在使用的记忆会取消应用。`)) {
      write(`/api/memories/${encodeURIComponent(selected)}`, { expected_memory_revision: catalog.revision }, "DELETE", null);
    }
  });
  $("memory-name").addEventListener("input", controls);
  $("memory-content").addEventListener("input", controls);
  $("memory-back").addEventListener("click", () => leave());
  window.addEventListener("beforeunload", (event) => {
    if (dirty() || busy) { event.preventDefault(); event.returnValue = ""; }
  });
  return { refresh, canLeave };
}
