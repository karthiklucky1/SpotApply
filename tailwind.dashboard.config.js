/** Dashboard stylesheet build (app/static/tailwind.css — COMMITTED).
 *  The previous file was hand-compiled and stale: JS-built badge/button
 *  classes were silently unstyled. This scans the full template (Tailwind's
 *  scanner picks up class names inside JS template literals too — the
 *  dashboard composes no dynamic `bg-${color}` classes, verified) AND the
 *  board partials it renders: the job detail's action row lives in
 *  _board_macros.html, and while only dashboard.html was scanned its
 *  `lg:ml-auto` never compiled, so the actions ran into Priority/Hiring Intent.
 *  After editing classes in any of these run: npm run build */
module.exports = {
  content: [
    "./app/templates/dashboard.html",
    "./app/templates/_board_macros.html",
    "./app/templates/_board_pane.html",
  ],
};
