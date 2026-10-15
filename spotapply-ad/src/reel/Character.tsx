import React from "react";

// The reel's job seeker, drawn in code: a flat illustration (clearly not a
// real person), with the expressions the reaction cuts need. Shares the demo
// candidate's name in the story, never a real photo or likeness.

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

const SKIN = "#DFA27A";
const SKIN_SHADE = "#C98B63";
const HAIR = "#2A1C19";
const HOODIE = "#2E6FA7";
const HOODIE_DARK = "#245A88";
const INK = "#1B1B2A";

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

const Eye: React.FC<{ cx: number; lid: number; look: [number, number]; arcs?: boolean; id: string }> = ({ cx, lid, look, arcs, id }) => {
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
        <rect x={cx - rx - 2} y={cy - ry - 4} width={2 * rx + 4} height={Math.max(0, lidY - (cy - ry - 4))} fill={SKIN} />
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
}> = ({ expr, blink = 0, size, glow, tilt = 0, style, id = "p" }) => {
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
      {/* shoulders + hoodie */}
      <path d="M28 300 Q34 252 92 238 L208 238 Q266 252 272 300 Z" fill={HOODIE} />
      <path d="M100 238 Q150 282 200 238 Q190 258 150 266 Q110 258 100 238 Z" fill={HOODIE_DARK} />
      <path d="M128 252 L124 286 M172 252 L176 286" stroke="#E8EEF5" strokeWidth={4} strokeLinecap="round" />
      <circle cx={124} cy={289} r={4} fill="#E8EEF5" />
      <circle cx={176} cy={289} r={4} fill="#E8EEF5" />
      <rect x={130} y={208} width={40} height={40} rx={10} fill={SKIN_SHADE} />
      <g transform={`rotate(${tilt} 150 200)`}>
        {/* hair behind */}
        <ellipse cx={150} cy={138} rx={88} ry={94} fill={HAIR} />
        <circle cx={168} cy={48} r={31} fill={HAIR} />
        <rect x={150} y={70} width={30} height={9} rx={4} fill="#7DD3FC" transform="rotate(-12 165 74)" />
        {/* ears + face */}
        <ellipse cx={79} cy={154} rx={11} ry={16} fill={SKIN_SHADE} />
        <ellipse cx={221} cy={154} rx={11} ry={16} fill={SKIN_SHADE} />
        <ellipse cx={150} cy={145} rx={72} ry={82} fill={SKIN} />
        <path d="M86 170 Q150 236 214 170 Q205 214 150 227 Q95 214 86 170 Z" fill={SKIN_SHADE} opacity={0.35} />
        {/* fringe */}
        <path d="M78 140 Q78 66 150 60 Q222 64 222 138 Q206 104 172 94 Q150 118 108 112 Q92 122 78 140 Z" fill={HAIR} />
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
        <Eye cx={122} lid={lid} look={L.look} arcs={L.arcs} id={`${id}l`} />
        <Eye cx={178} lid={lid} look={L.look} arcs={L.arcs} id={`${id}r`} />
        {/* brows */}
        <path d={`M105 ${124 + lo} Q120 ${118 + (li + lo) / 2} 136 ${123 + li}`} stroke={HAIR} strokeWidth={7} strokeLinecap="round" fill="none" />
        <path d={`M164 ${123 + ri} Q180 ${118 + (ri + ro) / 2} 195 ${124 + ro}`} stroke={HAIR} strokeWidth={7} strokeLinecap="round" fill="none" />
        {/* glasses */}
        <g stroke={INK} strokeWidth={4} fill="rgba(255,255,255,0.06)">
          <circle cx={122} cy={151} r={25} />
          <circle cx={178} cy={151} r={25} />
          <path d="M147 149 Q150 145 153 149" fill="none" />
          <path d="M97 148 L84 144 M203 148 L216 144" fill="none" />
        </g>
        {glow ? (
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
