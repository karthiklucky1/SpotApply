import React from "react";

// The reel's job seeker, drawn in code: a flat illustration (clearly not a
// real person), with the expressions the reaction cuts need. One face rig,
// several looks (CAST); Reel60's `character` prop picks one for every scene.
// The "photo" look is loosely styled after the owner's own photo, at the
// owner's request; no photo is traced or embedded.

export type Expr =
  | "neutral"
  | "tired"
  | "annoyed"
  | "deadpan"
  | "cringe"
  | "curious"
  | "impressed"
  | "happy"
  | "sideeye"
  | "relieved";

type Look = { lid: number; brows: [number, number, number, number]; mouth: string; look: [number, number]; bags?: boolean; blush?: boolean; arcs?: boolean };

// brows: [left inner, left outer, right inner, right outer] vertical offsets
// (negative = raised). lid: 0 open .. 1 closed.
const LOOKS: Record<Expr, Look> = {
  neutral: { lid: 0.12, brows: [0, 0, 0, 0], mouth: "smileSmall", look: [0, 0.2] },
  tired: { lid: 0.5, brows: [-2, 6, -2, 6], mouth: "flat", look: [0, 0.45], bags: true },
  annoyed: { lid: 0.42, brows: [7, -2, 7, -2], mouth: "tight", look: [0.55, 0.25], bags: true },
  deadpan: { lid: 0.4, brows: [1, 1, 1, 1], mouth: "flat", look: [0, 0], bags: true },
  cringe: { lid: 0.55, brows: [-9, 3, -9, 3], mouth: "grimace", look: [-0.35, 0.1] },
  curious: { lid: 0, brows: [-8, -6, -8, -6], mouth: "oSmall", look: [-0.45, 0.55] },
  impressed: { lid: 0, brows: [-11, -8, -11, -8], mouth: "openSmile", look: [0, 0.35] },
  happy: { lid: 0, brows: [-7, -5, -7, -5], mouth: "bigSmile", look: [0, 0], blush: true, arcs: true },
  sideeye: { lid: 0.36, brows: [2, 2, -10, -7], mouth: "smirk", look: [0.95, 0.05] },
  relieved: { lid: 0.42, brows: [-4, -2, -4, -2], mouth: "smile", look: [0, 0.1], blush: true },
};

const INK = "#1B1B2A";

// ── the cast: one face rig, several looks ───────────────────────────────────
export type HairStyle = "bun" | "shag" | "curly" | "fade" | "long" | "hijab";
export type Cast = {
  readonly label: string;
  readonly skin: string;
  readonly skinShade: string;
  readonly hair: string; // hair colour (the scarf colour for "hijab")
  readonly brow?: string; // defaults to `hair`
  readonly hairStyle: HairStyle;
  readonly glasses: "round" | "square" | "none";
  readonly top: "hoodie" | "collar" | "tee" | "cardigan";
  readonly topColor: string;
  readonly topDark: string;
  readonly facial?: "mustache";
  readonly accent?: string; // hair clip / scarf pin
};

export const CAST = {
  maya: { label: "Maya (current)", skin: "#DFA27A", skinShade: "#C98B63", hair: "#2A1C19", hairStyle: "bun", glasses: "round", top: "hoodie", topColor: "#2E6FA7", topDark: "#245A88", accent: "#7DD3FC" },
  photo: { label: "Shaggy hair, mustache, black shirt", skin: "#C68B66", skinShade: "#A8714F", hair: "#2B201E", hairStyle: "shag", glasses: "none", top: "collar", topColor: "#1F1E26", topDark: "#4A4956", facial: "mustache" },
  curly: { label: "Curly hair, tee", skin: "#8E5B3E", skinShade: "#74472F", hair: "#1E1512", hairStyle: "curly", glasses: "none", top: "tee", topColor: "#0E9F8E", topDark: "#0B7F72" },
  fade: { label: "Short hair, square glasses", skin: "#F0C6A6", skinShade: "#D9A584", hair: "#6B4A2E", hairStyle: "fade", glasses: "square", top: "hoodie", topColor: "#5B6B7D", topDark: "#4A586A" },
  hijab: { label: "Hijab, cardigan", skin: "#D59A72", skinShade: "#BC8059", hair: "#C46B85", brow: "#2A1C19", hairStyle: "hijab", glasses: "none", top: "cardigan", topColor: "#E3D6BF", topDark: "#C9B898", accent: "#F2C14E" },
  long: { label: "Long hair, tee", skin: "#F3CFAE", skinShade: "#DDB08C", hair: "#1C1A1F", hairStyle: "long", glasses: "round", top: "tee", topColor: "#F2C14E", topDark: "#D9A63A" },
} as const satisfies Record<string, Cast>;
export type CastId = keyof typeof CAST;

/** The reel's character; one provider at the top picks it for every scene. */
export const CastContext = React.createContext<Cast>(CAST.maya);

/** Hair drawn behind the head (before the ears and face). */
const HairBack: React.FC<{ c: Cast }> = ({ c }) => {
  switch (c.hairStyle) {
    case "bun":
      return (
        <g>
          <ellipse cx={150} cy={138} rx={88} ry={94} fill={c.hair} />
          <circle cx={168} cy={48} r={31} fill={c.hair} />
          <rect x={150} y={70} width={30} height={9} rx={4} fill={c.accent ?? "#7DD3FC"} transform="rotate(-12 165 74)" />
        </g>
      );
    case "shag":
      // Layered, past the ears, flicking out at the neck.
      return (
        <path
          d="M150 30 Q204 24 230 58 Q252 88 242 130 Q248 160 238 184 Q244 200 256 210 Q236 216 226 204 Q228 218 236 230 Q214 228 206 212 L196 198 L104 198 L94 212 Q86 228 64 230 Q72 218 74 204 Q64 216 44 210 Q56 200 62 184 Q52 160 58 130 Q48 88 70 58 Q96 24 150 30 Z"
          fill={c.hair}
        />
      );
    case "curly": {
      const curls: [number, number, number][] = [];
      for (let i = 0; i <= 12; i++) {
        const a = Math.PI * (0.95 + (1.1 * i) / 12);
        curls.push([150 + 84 * Math.cos(a), 116 + 80 * Math.sin(a), 27]);
      }
      return (
        <g fill={c.hair}>
          <ellipse cx={150} cy={118} rx={88} ry={80} />
          {curls.map(([x, y, r], i) => (
            <circle key={i} cx={x} cy={y} r={r} />
          ))}
        </g>
      );
    }
    case "fade":
      return <ellipse cx={150} cy={116} rx={78} ry={66} fill={c.hair} />;
    case "long":
      return <path d="M150 40 Q234 40 236 130 L244 262 Q222 276 200 262 L196 190 L104 190 L100 262 Q78 276 56 262 L64 130 Q66 40 150 40 Z" fill={c.hair} />;
    case "hijab":
      return (
        <g>
          <path d="M56 172 Q48 36 150 32 Q252 36 244 172 L250 264 Q150 304 50 264 Z" fill={c.hair} />
          <path d="M62 232 Q150 290 238 232" stroke="rgba(0,0,0,0.14)" strokeWidth={5} fill="none" strokeLinecap="round" />
        </g>
      );
  }
};

/** Hair drawn over the forehead (after the face, before the eyes). */
const HairFront: React.FC<{ c: Cast }> = ({ c }) => {
  switch (c.hairStyle) {
    case "bun":
      return <path d="M78 140 Q78 66 150 60 Q222 64 222 138 Q206 104 172 94 Q150 118 108 112 Q92 122 78 140 Z" fill={c.hair} />;
    case "shag":
      // A long, uneven fringe: the tips over the brows stay short so the
      // brows still read when they lift.
      return (
        <g fill={c.hair}>
          <path d="M72 152 Q62 94 90 66 Q118 42 150 46 Q192 42 214 66 Q240 96 228 152 Q224 128 214 118 Q210 106 202 100 Q196 94 188 102 Q182 90 172 98 Q166 90 160 102 Q154 110 147 117 Q144 108 139 101 Q133 90 127 98 Q118 90 112 102 Q104 94 98 100 Q90 106 86 118 Q76 128 72 152 Z" />
          <path d="M98 50 Q88 38 76 40 Q88 45 90 56 Z" />
          <path d="M134 36 Q128 23 115 23 Q127 29 126 40 Z" />
          <path d="M168 33 Q177 21 190 21 Q179 27 178 38 Z" />
          <path d="M208 48 Q220 37 233 41 Q221 45 216 56 Z" />
          <path d="M120 92 Q134 70 160 64 M176 90 Q190 74 206 76" stroke="rgba(255,255,255,0.12)" strokeWidth={3} fill="none" strokeLinecap="round" />
        </g>
      );
    case "curly": {
      const front: [number, number][] = [];
      for (let i = 0; i <= 8; i++) {
        const x = 84 + (132 * i) / 8;
        front.push([x, 72 + ((x - 150) / 66) ** 2 * 32]);
      }
      return (
        <g fill={c.hair}>
          {front.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={17} />
          ))}
        </g>
      );
    }
    case "fade":
      return (
        <g fill={c.hair}>
          <path d="M79 132 Q72 58 150 50 Q228 58 221 132 Q214 98 196 88 Q150 76 104 88 Q86 98 79 132 Z" />
          <path d="M79 132 Q79 142 82 148 L88 146 Q88 132 92 118 Z" opacity={0.4} />
          <path d="M221 132 Q221 142 218 148 L212 146 Q212 132 208 118 Z" opacity={0.4} />
        </g>
      );
    case "long":
      return (
        <g fill={c.hair}>
          <path d="M76 152 Q68 68 150 54 Q230 58 226 150 Q220 100 196 84 Q168 98 130 102 Q98 110 86 128 Q80 138 76 152 Z" />
        </g>
      );
    case "hijab":
      return (
        <g>
          <path d="M72 158 Q70 58 150 54 Q230 58 228 158 Q218 98 150 88 Q82 98 72 158 Z" fill={c.hair} />
          <path d="M84 120 Q150 70 216 120" stroke="rgba(255,255,255,0.18)" strokeWidth={4} fill="none" strokeLinecap="round" />
          <circle cx={226} cy={206} r={6} fill={c.accent ?? "#F2C14E"} />
        </g>
      );
  }
};

const Top: React.FC<{ c: Cast }> = ({ c }) => {
  const shoulders = <path d="M28 300 Q34 252 92 238 L208 238 Q266 252 272 300 Z" fill={c.topColor} />;
  switch (c.top) {
    case "hoodie":
      return (
        <g>
          {shoulders}
          <path d="M100 238 Q150 282 200 238 Q190 258 150 266 Q110 258 100 238 Z" fill={c.topDark} />
          <path d="M128 252 L124 286 M172 252 L176 286" stroke="#E8EEF5" strokeWidth={4} strokeLinecap="round" />
          <circle cx={124} cy={289} r={4} fill="#E8EEF5" />
          <circle cx={176} cy={289} r={4} fill="#E8EEF5" />
        </g>
      );
    case "collar":
      // Open collar, top button undone.
      return (
        <g>
          {shoulders}
          <path d="M124 234 L150 272 L176 234 Z" fill={c.skinShade} />
          <path d="M150 272 L150 300" stroke={c.topDark} strokeWidth={3} />
          <circle cx={150} cy={288} r={3.5} fill={c.topDark} />
          <path d="M116 234 L150 272 L128 284 L100 248 Z" fill={c.topColor} stroke={c.topDark} strokeWidth={3} strokeLinejoin="round" />
          <path d="M184 234 L150 272 L172 284 L200 248 Z" fill={c.topColor} stroke={c.topDark} strokeWidth={3} strokeLinejoin="round" />
        </g>
      );
    case "tee":
      return (
        <g>
          {shoulders}
          <path d="M112 236 Q150 270 188 236 Z" fill={c.skinShade} />
          <path d="M110 238 Q150 272 190 238" stroke={c.topDark} strokeWidth={8} fill="none" strokeLinecap="round" />
        </g>
      );
    case "cardigan":
      return (
        <g>
          {shoulders}
          <path d="M114 238 L140 300 L160 300 L186 238 Z" fill="#2F4A6B" />
          <path d="M124 238 Q150 260 176 238 Z" fill={c.skinShade} />
          <path d="M114 238 L140 300 M186 238 L160 300" stroke={c.topDark} strokeWidth={5} strokeLinecap="round" />
          <circle cx={136} cy={276} r={3.5} fill={c.topDark} />
          <circle cx={131} cy={262} r={3.5} fill={c.topDark} />
        </g>
      );
  }
};

const Glasses: React.FC<{ kind: Cast["glasses"] }> = ({ kind }) => {
  if (kind === "none") return null;
  return (
    <g stroke={INK} strokeWidth={4} fill="rgba(255,255,255,0.06)">
      {kind === "round" ? (
        <>
          <circle cx={122} cy={151} r={25} />
          <circle cx={178} cy={151} r={25} />
        </>
      ) : (
        <>
          <rect x={96} y={131} width={52} height={40} rx={10} />
          <rect x={152} y={131} width={52} height={40} rx={10} />
        </>
      )}
      <path d="M147 149 Q150 145 153 149" fill="none" />
      <path d="M97 148 L84 144 M203 148 L216 144" fill="none" />
    </g>
  );
};

const Mouth: React.FC<{ kind: string }> = ({ kind }) => {
  switch (kind) {
    case "flat":
      return <path d="M131 201 Q150 202.5 169 201" stroke={INK} strokeWidth={4.5} strokeLinecap="round" fill="none" />;
    case "tight":
      return <path d="M136 202 L164 199.5" stroke={INK} strokeWidth={4.5} strokeLinecap="round" fill="none" />;
    case "smileSmall":
      return <path d="M133 197 Q150 208 167 197" stroke={INK} strokeWidth={4.5} strokeLinecap="round" fill="none" />;
    case "smile":
      return <path d="M129 195 Q150 214 171 195" stroke={INK} strokeWidth={4.5} strokeLinecap="round" fill="none" />;
    case "smirk":
      return <path d="M134 203 Q153 205 168 194" stroke={INK} strokeWidth={4.5} strokeLinecap="round" fill="none" />;
    case "oSmall":
      return <ellipse cx={150} cy={203} rx={7} ry={9} fill="#5B1F2A" />;
    case "openSmile":
      return (
        <g>
          <path d="M127 194 Q150 224 173 194 Z" fill="#5B1F2A" />
          <path d="M131 195.5 Q150 201 169 195.5 L168 194 Q150 196 132 194 Z" fill="#FFFFFF" />
        </g>
      );
    case "bigSmile":
      return (
        <g>
          <path d="M123 191 Q150 232 177 191 Q150 199 123 191 Z" fill="#5B1F2A" />
          <path d="M127 193 Q150 199 173 193 Q171 199 168 201 Q150 205 132 201 Q129 198 127 193 Z" fill="#FFFFFF" />
          <path d="M138 214 Q150 222 162 214 Q150 210 138 214 Z" fill="#E07A86" />
        </g>
      );
    case "grimace":
      return (
        <g>
          <rect x={127} y={192} width={46} height={17} rx={7} fill="#FFFFFF" stroke={INK} strokeWidth={3.5} />
          {[138, 150, 162].map((x) => (
            <line key={x} x1={x} y1={193} x2={x} y2={208} stroke={INK} strokeWidth={2} />
          ))}
          <line x1={128} y1={200.5} x2={172} y2={200.5} stroke={INK} strokeWidth={2} />
        </g>
      );
    default:
      return null;
  }
};

const Eye: React.FC<{ cx: number; lid: number; look: [number, number]; arcs?: boolean; id: string; skin: string }> = ({ cx, lid, look, arcs, id, skin }) => {
  const cy = 150;
  if (arcs) {
    return <path d={`M${cx - 14} ${cy + 3} Q${cx} ${cy - 13} ${cx + 14} ${cy + 3}`} stroke={INK} strokeWidth={5} strokeLinecap="round" fill="none" />;
  }
  const rx = 15;
  const ry = 17;
  const lidY = cy - ry + lid * 2 * ry;
  return (
    <g>
      <defs>
        <clipPath id={`eye-${id}`}>
          <ellipse cx={cx} cy={cy} rx={rx} ry={ry} />
        </clipPath>
      </defs>
      <ellipse cx={cx} cy={cy} rx={rx} ry={ry} fill="#FFFFFF" />
      <g clipPath={`url(#eye-${id})`}>
        <circle cx={cx + look[0] * 5.5} cy={cy + 2 + look[1] * 5} r={9.5} fill="#2B1E1A" />
        <circle cx={cx + look[0] * 5.5 + 3.5} cy={cy - 1.5 + look[1] * 5} r={3.2} fill="#FFFFFF" />
        <rect x={cx - rx - 2} y={cy - ry - 4} width={2 * rx + 4} height={Math.max(0, lidY - (cy - ry - 4))} fill={skin} />
      </g>
      {/* lash line follows the lid */}
      <path
        d={`M${cx - rx - 1} ${Math.min(lidY, cy + ry) + 1} Q${cx} ${Math.min(lidY, cy + ry) - 3 + lid * 3} ${cx + rx + 1} ${Math.min(lidY, cy + ry) + 1}`}
        stroke={INK}
        strokeWidth={4}
        strokeLinecap="round"
        fill="none"
      />
    </g>
  );
};

/** Head and shoulders in a 300 x 300 box. `blink` 0..1 closes the eyes. */
export const Person: React.FC<{
  readonly expr: Expr;
  readonly blink?: number;
  readonly size: number;
  readonly glow?: string; // screen light on the face (night)
  readonly tilt?: number;
  readonly style?: React.CSSProperties;
  readonly id?: string;
  readonly cast?: Cast; // overrides the CastContext
}> = ({ expr, blink = 0, size, glow, tilt = 0, style, id = "p", cast }) => {
  const ctx = React.useContext(CastContext);
  const c = cast ?? ctx;
  const SKIN = c.skin;
  const SKIN_SHADE = c.skinShade;
  const BROW = c.brow ?? c.hair;
  const L = LOOKS[expr];
  const lid = Math.min(1, L.lid + (1 - L.lid) * blink);
  const [li, lo, ri, ro] = L.brows;
  return (
    <svg width={size} height={size} viewBox="0 0 300 300" style={{ display: "block", overflow: "visible", ...style }}>
      <defs>
        <clipPath id={`face-${id}`}>
          <ellipse cx={150} cy={145} rx={72} ry={82} />
        </clipPath>
        <radialGradient id={`glow-${id}`} cx="50%" cy="78%" r="75%">
          <stop offset="0%" stopColor={glow ?? "transparent"} stopOpacity={0.26} />
          <stop offset="100%" stopColor={glow ?? "transparent"} stopOpacity={0} />
        </radialGradient>
      </defs>
      {/* shoulders + top */}
      <rect x={130} y={208} width={40} height={40} rx={10} fill={SKIN_SHADE} />
      <Top c={c} />
      <g transform={`rotate(${tilt} 150 200)`}>
        <HairBack c={c} />
        {/* ears + face */}
        {c.hairStyle === "hijab" ? null : (
          <>
            <ellipse cx={79} cy={154} rx={11} ry={16} fill={SKIN_SHADE} />
            <ellipse cx={221} cy={154} rx={11} ry={16} fill={SKIN_SHADE} />
          </>
        )}
        <ellipse cx={150} cy={145} rx={72} ry={82} fill={SKIN} />
        <path d="M86 170 Q150 236 214 170 Q205 214 150 227 Q95 214 86 170 Z" fill={SKIN_SHADE} opacity={0.35} />
        {c.facial === "mustache" ? (
          <g fill={c.hair}>
            <path d="M125 195 Q130 183 150 184 Q170 183 175 195 Q167 189 150 190 Q133 189 125 195 Z" opacity={0.9} />
            <path d="M138 216 Q150 226 162 216 Q156 230 150 231 Q144 230 138 216 Z" opacity={0.55} />
            <path d="M94 186 Q98 222 150 230 Q202 222 206 186 Q198 214 150 224 Q102 214 94 186 Z" opacity={0.14} />
          </g>
        ) : null}
        <HairFront c={c} />
        {L.bags ? (
          <g stroke="#9C5A6E" strokeOpacity={0.45} strokeWidth={3} fill="none" strokeLinecap="round">
            <path d="M110 172 Q122 178 134 172" />
            <path d="M166 172 Q178 178 190 172" />
          </g>
        ) : null}
        {L.blush ? (
          <g fill="#F08A8A" opacity={0.42}>
            <ellipse cx={106} cy={184} rx={14} ry={8} />
            <ellipse cx={194} cy={184} rx={14} ry={8} />
          </g>
        ) : null}
        <Eye cx={122} lid={lid} look={L.look} arcs={L.arcs} id={`${id}l`} skin={SKIN} />
        <Eye cx={178} lid={lid} look={L.look} arcs={L.arcs} id={`${id}r`} skin={SKIN} />
        {/* brows */}
        <path d={`M105 ${124 + lo} Q120 ${118 + (li + lo) / 2} 136 ${123 + li}`} stroke={BROW} strokeWidth={7} strokeLinecap="round" fill="none" />
        <path d={`M164 ${123 + ri} Q180 ${118 + (ri + ro) / 2} 195 ${124 + ro}`} stroke={BROW} strokeWidth={7} strokeLinecap="round" fill="none" />
        <Glasses kind={c.glasses} />
        {glow && c.glasses !== "none" ? (
          <g opacity={0.45}>
            <path d="M106 140 L120 131 M162 140 L176 131" stroke="#E0F2FE" strokeWidth={4} strokeLinecap="round" />
          </g>
        ) : null}
        <path d="M146 166 Q151 177 156 172" stroke={SKIN_SHADE} strokeWidth={4} strokeLinecap="round" fill="none" />
        <Mouth kind={L.mouth} />
        {glow ? <ellipse cx={150} cy={160} rx={86} ry={100} fill={`url(#glow-${id})`} clipPath={`url(#face-${id})`} /> : null}
      </g>
    </svg>
  );
};

/** A natural blink: closed for ~3 frames every few seconds (deterministic). */
export const blinkAt = (frame: number, offset = 0) => {
  const period = 97;
  const p = (frame + offset) % period;
  return p < 2 ? p / 2 : p < 4 ? 1 : p < 6 ? 1 - (p - 4) / 2 : 0;
};
