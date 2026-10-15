/** Public pages stylesheet (app/static/tailwind-public.css — COMMITTED).
 *  pricing, privacy, terms and auth (login/signup) loaded the Tailwind PLAY
 *  CDN: a development tool that compiles in the browser, is render-blocking,
 *  third-party, and prints a production warning (audit 2026-09-25, finding 13).
 *  extension, recruiter, messages and public_profile followed (2026-10): the
 *  CDN also sent every visitor's IP address to a third party. None of these
 *  pages had an inline `tailwind.config` (nothing to port into theme.extend)
 *  and none composes class names in JS — their scripts only toggle the literal
 *  `hidden` (also in the markup) and the page's own `active` class, and the
 *  HTML they build in template literals spells every class out in full, which
 *  the scanner reads. So no safelist is needed; add one here if a page ever
 *  builds a class like 'bg-' + color. After editing classes in any of them
 *  run: npm run build:css:public */
module.exports = {
  content: [
    "./app/templates/pricing.html",
    "./app/templates/privacy.html",
    "./app/templates/terms.html",
    "./app/templates/auth.html",
    "./app/templates/_public_base.html",
    "./app/templates/refunds.html",
    "./app/templates/cookies.html",
    "./app/templates/404.html",
    "./app/templates/email_unsubscribe.html",
    "./app/templates/extension.html",
    "./app/templates/recruiter.html",
    "./app/templates/messages.html",
    "./app/templates/public_profile.html",
  ],
};
