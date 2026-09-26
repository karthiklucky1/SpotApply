/** Public pages stylesheet (app/static/tailwind-public.css — COMMITTED).
 *  pricing, privacy, terms and auth (login/signup) loaded the Tailwind PLAY
 *  CDN: a development tool that compiles in the browser, is render-blocking,
 *  third-party, and prints a production warning (audit 2026-09-25, finding 13).
 *  None of these four composes class names dynamically in JS (checked), so the
 *  scanner sees every class. After editing classes in them run: npm run build */
module.exports = {
  content: [
    "./app/templates/pricing.html",
    "./app/templates/privacy.html",
    "./app/templates/terms.html",
    "./app/templates/auth.html",
  ],
};
