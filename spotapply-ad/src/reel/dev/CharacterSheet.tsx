import React from "react";
import { AbsoluteFill } from "remotion";
import { FONTS } from "../../brand";
import { type Expr, Person } from "../Character";

// Dev sheet: every expression side by side (Remotion Studio: Reel dev/Character).
const ALL: Expr[] = ["neutral", "tired", "annoyed", "deadpan", "cringe", "curious", "impressed", "happy", "sideeye", "relieved"];

export const CharacterSheet: React.FC = () => (
  <AbsoluteFill style={{ background: "#E9F2FA", flexDirection: "row", flexWrap: "wrap", padding: 30, gap: 20, fontFamily: FONTS.sans }}>
    {ALL.map((e, i) => (
      <div key={e} style={{ width: 320, display: "flex", flexDirection: "column", alignItems: "center" }}>
        <Person expr={e} size={300} id={`s${i}`} glow={i === 1 ? "#7DD3FC" : undefined} />
        <div style={{ fontSize: 26, fontWeight: 700 }}>{e}</div>
      </div>
    ))}
  </AbsoluteFill>
);
