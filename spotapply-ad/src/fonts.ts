import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

// Bundled locally from @fontsource (SIL OFL 1.1, licences in public/fonts/),
// so renders never depend on a font CDN being reachable.
const OUTFIT_WEIGHTS = ["400", "500", "600", "700", "800"] as const;

export const fontsReady = Promise.all([
  ...OUTFIT_WEIGHTS.map((weight) =>
    loadFont({
      family: "Outfit",
      url: staticFile(`fonts/outfit-latin-${weight}-normal.woff2`),
      weight,
      display: "block",
    }),
  ),
  loadFont({
    family: "Instrument Serif",
    url: staticFile("fonts/instrument-serif-latin-400-italic.woff2"),
    weight: "400",
    style: "italic",
    display: "block",
  }),
]);
