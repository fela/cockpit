// The local cookie travels across ports; this credential stays in this page's origin storage.
(() => {
  const pageSecret = () => {
    try { return localStorage.getItem("cockpit.local-secret"); } catch { return null; }
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const sameOrigin = url.origin === location.origin;
    if (sameOrigin) {
      const secret = pageSecret();
      if (secret) {
        const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
        headers.set("X-Cockpit-Local", secret);
        options = { ...options, headers };
      }
    }
    const response = await originalFetch(input, options);
    if (sameOrigin && response.status === 401 && response.headers.get("X-Cockpit-Auth") === "local") {
      try { localStorage.removeItem("cockpit.local-secret"); } catch {}
      location.replace("/auth/local");
    }
    return response;
  };
  // Sign-out is the only navigation form; emergency's composer already submits through fetch.
  addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.getAttribute("action") !== "/auth/sign-out" || !pageSecret()) return;
    event.preventDefault();
    const response = await fetch("/auth/sign-out", { method: "POST" });
    if (response.ok) {
      try { localStorage.removeItem("cockpit.local-secret"); } catch {}
      location.replace("/auth/local");
    }
  });
})();
