import React from "react";
import { AbsoluteFill } from "remotion";
import { FONTS } from "../../brand";
import { CAST, type CastId, type Expr, Person } from "../Character";

// Dev sheet: every look in the cast, with the expressions the reel leans on
// (Remotion Studio: Reel-dev/Reel-Cast).
const ORDER: CastId[] = ["maya", "photo", "curly", "fade", "hijab", "long"];
const SMALL: Expr[] = ["annoyed", "impressed", "happy"];

export const CastSheet: React.FC = () => (
  <AbsoluteFill style={{ background: "#08202F", flexDirection: "row", flexWrap: "wrap", padding: 30, gap: 30, fontFamily: FONTS.sans }}>
    {ORDER.map((id, i) => {
      const c = CAST[id];
      return (
        <div key={id} style={{ width: 600, height: 640, background: "#E9F2FA", borderRadius: 28, display: "flex", flexDirection: "column", alignItems: "center", paddingTop: 22 }}>
          <div style={{ fontSize: 34, fontWeight: 800, color: "#08202F" }}>
            {String.fromCharCode(65 + i)} · <span style={{ fontWeight: 600 }}>{c.label}</span>
          </div>
          <Person expr="neutral" size={340} cast={c} id={`c${i}`} style={{ marginTop: 8 }} />
          <div style={{ display: "flex", gap: 14, marginTop: 6 }}>
            {SMALL.map((e, k) => (
              <div key={e} style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <div style={{ width: 170, height: 170, borderRadius: "50%", background: "#FFFFFF", overflow: "hidden", display: "flex", justifyContent: "center", alignItems: "flex-end" }}>
                  <Person expr={e} size={196} cast={c} id={`c${i}-${k}`} style={{ marginBottom: -14 }} />
                </div>
                <div style={{ fontSize: 20, fontWeight: 600, color: "#4A6072", marginTop: 4 }}>{e}</div>
              </div>
            ))}
          </div>
        </div>
      );
    })}
  </AbsoluteFill>
);
