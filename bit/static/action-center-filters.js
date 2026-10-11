/* Checkbox dropdowns backed by native multiple selects for the daily filters. */
(() => {
  const fields = new Map();
  function render(key) {
    const field = fields.get(key);
    if (!field) return;
    const {select, summary, list, search, all} = field;
    const options = Array.from(select.options).filter(option => option.value);
    const selected = options.filter(option => option.selected);
    summary.textContent = selected.length ? selected.map(option => option.textContent).join('、') : all;
    summary.title = summary.textContent;
    list.replaceChildren();
    const query = search.value.trim().toLocaleLowerCase();
    const visible = options.filter(option => option.textContent.toLocaleLowerCase().includes(query));
    for (const option of visible) {
      const label = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = option.value; checkbox.checked = option.selected;
      checkbox.addEventListener('change', () => {
        option.selected = checkbox.checked;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        render(key);
        Array.from(list.querySelectorAll('input')).find(input => input.value === option.value)?.focus({preventScroll: true});
      });
      label.append(checkbox, document.createTextNode(option.textContent));
      list.append(label);
    }
    if (!visible.length) {
      const empty = document.createElement('span'); empty.className = 'hint';
      empty.textContent = options.length ? '没有匹配的选项' : '当前没有可选项'; list.append(empty);
    }
  }
  for (const key of ['salesperson', 'group_name']) {
    const select = document.getElementById(key);
    if (!select) continue;
    const all = key === 'salesperson' ? '全部业务员' : '全部店铺组';
    const details = document.createElement('details');
    details.className = 'today-multiselect'; details.dataset.filter = key;
    const summary = document.createElement('summary');
    summary.setAttribute('aria-label', select.getAttribute('aria-label'));
    const menu = document.createElement('div'); menu.className = 'today-filter-menu';
    const search = document.createElement('input'); search.type = 'search';
    search.placeholder = key === 'salesperson' ? '搜索业务员' : '搜索店铺组';
    search.setAttribute('aria-label', search.placeholder);
    const clear = document.createElement('button');
    clear.type = 'button'; clear.className = 'button'; clear.textContent = all;
    clear.addEventListener('click', () => {
      for (const option of select.options) option.selected = false;
      select.dispatchEvent(new Event('change', {bubbles: true})); render(key);
    });
    const list = document.createElement('div'); list.className = 'today-filter-options';
    menu.append(search, clear, list); details.append(summary, menu); select.after(details); select.hidden = true;
    fields.set(key, {select, details, summary, search, list, all});
    search.addEventListener('input', () => render(key));
    details.addEventListener('toggle', () => {
      if (details.open) for (const [other, field] of fields) if (other !== key) field.details.open = false;
    });
    details.addEventListener('keydown', event => {
      if (event.key === 'Escape') {details.open = false; summary.focus();}
    });
    render(key);
  }
  document.addEventListener('click', event => {
    for (const field of fields.values()) if (!field.details.contains(event.target)) field.details.open = false;
  });
  window.TodayMultiSelect = {render};
})();
