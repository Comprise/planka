// источник: @mermaid-js/mermaid-zenuml 1.0.1, dist/mermaid-zenuml.js, строки 6517–6524 (код @headlessui/react utils/render: регулярка за «)» заголовка for)
  function F92(...e10) {
    if (e10.length === 0) return {};
    if (e10.length === 1) return e10[0];
    let t10 = {}, n10 = {};
    for (let s10 of e10) for (let a10 in s10) a10.startsWith("on") && typeof s10[a10] == "function" ? (n10[a10] != null || (n10[a10] = []), n10[a10].push(s10[a10])) : t10[a10] = s10[a10];
    if (t10.disabled || t10["aria-disabled"]) for (let s10 in n10) /^(on(?:Click|Pointer|Mouse|Key)(?:Down|Up|Press)?)$/.test(s10) && (n10[s10] = [(a10) => {
      var o10;
      return (o10 = a10?.preventDefault) == null ? void 0 : o10.call(a10);
