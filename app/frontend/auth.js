(function () {
  const nativeFetch = window.fetch.bind(window);
  const key = "kh_studio_token";
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin) return nativeFetch(input, init);
    const requestInit = Object.assign({}, init || {});
    const headers = new Headers(requestInit.headers || (input instanceof Request ? input.headers : undefined));
    const saved = sessionStorage.getItem(key);
    if (saved) headers.set("X-Studio-Token", saved);
    requestInit.headers = headers;
    let response = await nativeFetch(input, requestInit);
    if (response.status !== 401) return response;
    const entered = prompt("This Studio is protected. Enter the fleet token shown in StudioHub's Remote tab.");
    if (!entered) return response;
    sessionStorage.setItem(key, entered.trim());
    headers.set("X-Studio-Token", entered.trim());
    response = await nativeFetch(input, requestInit);
    if (response.status === 401) sessionStorage.removeItem(key);
    return response;
  };
})();

