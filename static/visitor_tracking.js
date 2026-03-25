(function () {
  const COOKIE_NAME = "rb_vid";
  const STORAGE_KEY = "rb_tracking_v1";
  const COOKIE_DAYS = 180;
  const UTM_KEYS = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
  ];

  function clean(value, limit) {
    if (value === null || value === undefined) return "";
    return String(value).trim().slice(0, limit || 240);
  }

  function readCookie(name) {
    const parts = document.cookie ? document.cookie.split(";") : [];
    const prefix = `${name}=`;
    for (const raw of parts) {
      const value = raw.trim();
      if (value.startsWith(prefix)) {
        return decodeURIComponent(value.slice(prefix.length));
      }
    }
    return "";
  }

  function setCookie(name, value, days) {
    const maxAge = Math.max(1, Number(days || COOKIE_DAYS)) * 24 * 60 * 60;
    document.cookie = `${name}=${encodeURIComponent(value)}; Max-Age=${maxAge}; Path=/; SameSite=Lax`;
  }

  function readStorage() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeStorage(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } catch (_) {
      // ignore storage failures
    }
  }

  function extractUtmFromUrl(urlLike) {
    const out = {};
    try {
      const url = new URL(urlLike, window.location.origin);
      UTM_KEYS.forEach((key) => {
        const val = clean(url.searchParams.get(key), 240);
        if (val) out[key] = val;
      });
    } catch (_) {
      // ignore malformed URL
    }
    return out;
  }

  function normalizeUtm(input) {
    const out = {};
    if (!input || typeof input !== "object") return out;
    UTM_KEYS.forEach((key) => {
      const val = clean(input[key], 240);
      if (val) out[key] = val;
    });
    return out;
  }

  function ensureVisitorId(cookieValue, storedValue) {
    const cookieId = clean(cookieValue, 64).replace(/[^A-Za-z0-9_-]/g, "");
    if (cookieId.length >= 8) return cookieId;

    const localId = clean(storedValue, 64).replace(/[^A-Za-z0-9_-]/g, "");
    if (localId.length >= 8) return localId;

    return `v_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`;
  }

  const stored = readStorage();
  const visitorId = ensureVisitorId(readCookie(COOKIE_NAME), stored.visitor_id);
  if (!readCookie(COOKIE_NAME)) {
    setCookie(COOKIE_NAME, visitorId, COOKIE_DAYS);
  }

  const storedUtm = normalizeUtm(stored.utm);
  const pageUtm = extractUtmFromUrl(window.location.href);
  const mergedUtm = Object.assign({}, storedUtm, pageUtm);

  const landing = clean(stored.landing, 800) || clean(window.location.href.split("#")[0], 800);
  const referrer = clean(document.referrer, 800) || clean(stored.referrer, 800);

  const state = {
    visitor_id: visitorId,
    landing,
    referrer,
    utm: mergedUtm,
    last_seen: new Date().toISOString(),
  };
  writeStorage(state);

  window.RB_TRACKING = {
    getContext: function () {
      const runtimeReferrer = clean(document.referrer, 800) || state.referrer;
      return {
        visitor_id: state.visitor_id,
        visitor_landing: state.landing,
        visitor_referrer: runtimeReferrer,
        visitor_utm: state.utm,
      };
    },
  };
})();
