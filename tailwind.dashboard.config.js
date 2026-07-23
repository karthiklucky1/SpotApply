/** Dashboard stylesheet build (app/static/tailwind.css — COMMITTED).
 *  The previous file was hand-compiled and stale: JS-built badge/button
 *  classes were silently unstyled. This scans the full template (Tailwind's
 *  scanner picks up class names inside JS template literals too — the
 *  dashboard composes no dynamic `bg-${color}` classes, verified).
 *  After editing classes in dashboard.html run: npm run build */
module.exports = {
  content: ["./app/templates/dashboard.html"],
};
