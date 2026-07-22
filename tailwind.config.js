/** Landing page ONLY. The dashboard and other authed templates stay on the
 *  Tailwind Play CDN because they build class strings dynamically in JS —
 *  compiling those without a careful safelist would silently drop styles.
 *  After editing classes in app/templates/landing.html run: npm run build
 *  (the compiled output app/static/tailwind-landing.css is committed). */
module.exports = {
  content: ["./app/templates/landing.html"],
};
