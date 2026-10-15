// Vertical layout for Reels / Shorts. The platforms draw their own UI over the
// video: a header across the top (~200 px), the like/comment/share column on
// the right below ~y 860, and the caption + account row across the bottom
// (from ~y 1480). Everything a viewer must read stays inside these lines.
export const SAFE = {
  left: 70,
  right: 930, // nothing essential right of this below RIGHT_RAIL_FROM
  rightRailFrom: 860,
  top: 230,
  bottom: 1470,
} as const;

// The product "screen card": a phone or laptop screen seen in close-up.
// 960 x 880 (was 860 x 780: +12% wide, +13% tall, owner's request): the
// card's right edge now runs under the platforms' button column below
// y 860, so nothing a viewer must read is framed into its last ~90 px.
export const CARD = { x: 60, y: 488, w: 960, h: 880, bar: 58 } as const;
export const CARD_VIEW = { w: CARD.w, h: CARD.h - CARD.bar } as const; // 960 x 822

// The reaction bubble rides the card's top-right corner (above the rail).
export const PIP = { cx: 966, cy: 488, r: 100 } as const;

// Where the card's call-out stickers sit: inside its bottom edge.
export const STICKER = { x: CARD.x + 40, y: CARD.y + CARD.h - 84 } as const;

// Beat headline (top) and the word-by-word captions (under the card).
export const HEADLINE = { x: 70, y: 262, maxW: 720 } as const;
export const CAPTION = { cx: 520, y: 1390, maxW: 900, size: 68 } as const;

// Source sizes of the recordings in public/reel/footage/ (device pixels).
export const FOOTAGE = {
  phone_match: [1076, 2330],
  phone_gap: [1076, 2330],
  desk_tailor: [2000, 1560],
  desk_dashboard_fill: [2000, 1002],
  desk_extension: [2000, 1280],
} as const;
export type FootageId = keyof typeof FOOTAGE;
