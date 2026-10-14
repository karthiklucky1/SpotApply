/** Dashboard stylesheet build (app/static/tailwind.css — COMMITTED).
 *  The previous file was hand-compiled and stale: JS-built badge/button
 *  classes were silently unstyled. This scans the full template (Tailwind's
 *  scanner picks up class names inside JS template literals too — the
 *  dashboard composes no dynamic `bg-${color}` classes, verified).
 *  The board panes were moved into _board_pane.html / _board_macros.html
 *  (included by dashboard.html, and served alone by GET /api/board/pane), so
 *  they are scanned too: scanning dashboard.html alone dropped every class
 *  only a card uses (line-clamp-2, opacity-60, text-[8.5px], ...).
 *  After editing classes in dashboard.html or _board_*.html run: npm run build */
module.exports = {
  content: ["./app/templates/dashboard.html", "./app/templates/_board_*.html"],
};
