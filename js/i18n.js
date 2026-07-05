/**
 * i18n.js — Client-side i18n cho dashboard.html / trading-landing (static site, Vercel).
 *
 * KHÔNG cần backend routing (không có /en/, /vi/ prefix trên URL) vì đây là site tĩnh.
 * Ngôn ngữ được lưu trong localStorage, áp dụng ngay khi trang load (tránh "nháy" chữ VI trước EN).
 *
 * CÁCH DÙNG TRONG HTML:
 *   <span data-i18n="dashboard.win_rate"></span>
 *   <input data-i18n-placeholder="post.title_placeholder">
 *   <img data-i18n-alt="nav.logo_alt">
 *
 * NHÚNG VÀO dashboard.html / index.html (trading-landing):
 *   <script src="/js/i18n.js" defer></script>
 */

(function () {
  const STORAGE_KEY = "thienminhgold_lang";
  const DEFAULT_LANG = "vi";
  const SUPPORTED_LANGS = ["vi", "en"];

  let translations = {};
  let currentLang = DEFAULT_LANG;

  function getStoredLang() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED_LANGS.includes(stored)) return stored;

    // Fallback: đoán theo ngôn ngữ trình duyệt nếu chưa từng chọn
    const browserLang = navigator.language.slice(0, 2);
    return SUPPORTED_LANGS.includes(browserLang) ? browserLang : DEFAULT_LANG;
  }

  async function loadTranslations(lang) {
    try {
      const res = await fetch(`/locales/${lang}.json`, { cache: "no-cache" });
      if (!res.ok) throw new Error(`Không tải được locales/${lang}.json`);
      return await res.json();
    } catch (err) {
      console.error("[i18n] Lỗi tải file dịch:", err);
      return {};
    }
  }

  // Truy cập key dạng "dashboard.win_rate" trong object lồng nhau
  function resolveKey(obj, path) {
    return path.split(".").reduce((acc, part) => (acc && acc[part] !== undefined ? acc[part] : null), obj);
  }

  function applyTranslations() {
    document.documentElement.setAttribute("lang", currentLang);

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      const value = resolveKey(translations, key);
      if (value !== null) el.textContent = value;
    });

    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      const value = resolveKey(translations, key);
      if (value !== null) el.setAttribute("placeholder", value);
    });

    document.querySelectorAll("[data-i18n-alt]").forEach((el) => {
      const key = el.getAttribute("data-i18n-alt");
      const value = resolveKey(translations, key);
      if (value !== null) el.setAttribute("alt", value);
    });

    // Cập nhật trạng thái active trên nút chọn cờ (nếu có switcher trên trang)
    document.querySelectorAll("[data-lang-option]").forEach((el) => {
      el.classList.toggle("active", el.getAttribute("data-lang-option") === currentLang);
    });
  }

  async function setLanguage(lang) {
    if (!SUPPORTED_LANGS.includes(lang)) return;
    currentLang = lang;
    localStorage.setItem(STORAGE_KEY, lang);
    translations = await loadTranslations(lang);
    applyTranslations();
  }

  // Khởi tạo ngay khi DOM sẵn sàng
  document.addEventListener("DOMContentLoaded", async () => {
    currentLang = getStoredLang();
    translations = await loadTranslations(currentLang);
    applyTranslations();

    // Gắn sự kiện cho các nút chọn ngôn ngữ (xem language-switcher.html)
    document.querySelectorAll("[data-lang-option]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.preventDefault();
        setLanguage(el.getAttribute("data-lang-option"));
      });
    });
  });

  // Expose ra window để gọi thủ công nếu cần (ví dụ từ dropdown custom)
  window.ThienMinhGoldI18n = { setLanguage, getCurrentLang: () => currentLang };
})();
