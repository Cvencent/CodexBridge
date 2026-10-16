/*
 * Codex Bridge desktop compatibility shim.
 *
 * Older rollout histories can contain text inputs without the
 * `text_elements` array. The desktop renderer assumes that array exists and
 * crashes while opening those conversations. This shim only normalizes the
 * in-memory app-server/IPC payloads; it does not rewrite rollout files.
 */
(() => {
  const INSTALL_KEY = "__codexBridgeDesktopCompatV1";
  if (window[INSTALL_KEY]) return;

  const MAX_DEPTH = 80;

  function normalizeTextElements(value, seen, depth) {
    if (!value || typeof value !== "object" || depth > MAX_DEPTH) return value;
    if (seen.has(value)) return value;
    seen.add(value);

    if (Array.isArray(value)) {
      for (const item of value) normalizeTextElements(item, seen, depth + 1);
      return value;
    }

    if (value.type === "text" && !Array.isArray(value.text_elements)) {
      try {
        Object.defineProperty(value, "text_elements", {
          configurable: true,
          enumerable: true,
          value: [],
          writable: true,
        });
      } catch {
        // Frozen payloads are left untouched.
      }
    }

    for (const key of Object.keys(value)) {
      normalizeTextElements(value[key], seen, depth + 1);
    }
    return value;
  }

  function normalizePayload(value) {
    try {
      normalizeTextElements(value, new WeakSet(), 0);
    } catch {
      // Never let a compatibility normalization failure break the host page.
    }
  }

  function installMessageHook() {
    window.addEventListener(
      "message",
      (event) => {
        if (event && typeof event === "object" && "data" in event) {
          normalizePayload(event.data);
        }
      },
      true,
    );
  }

  function installClientHook() {
    const clientClass = window.__codexPlusAppServerClientClass;
    if (!clientClass || !clientClass.prototype) return false;
    const prototype = clientClass.prototype;
    if (!prototype.__codexBridgeDesktopCompatSendRequest) {
      const original = prototype.sendRequest;
      if (typeof original !== "function") return false;
      prototype.sendRequest = async function (method, params, options) {
        const result = await original.call(this, method, params, options);
        if (typeof method === "string" && method.startsWith("thread/")) {
          normalizePayload(result);
        }
        return result;
      };
      prototype.__codexBridgeDesktopCompatSendRequest = { version: 1 };
    }
    return true;
  }

  installMessageHook();
  installClientHook();

  // Codex++ may inject before the app-server client class is exposed.
  let attempts = 0;
  const timer = window.setInterval(() => {
    attempts += 1;
    if (installClientHook() || attempts >= 300) {
      window.clearInterval(timer);
    }
  }, 100);

  window[INSTALL_KEY] = { version: 1 };
})();