import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../../anim";
import { COLORS, FONTS } from "../../brand";
import { CheckIcon, DocIcon } from "../../components/Icons";
import { blinkAt, type Expr, Person } from "../Character";
import { CARD } from "../layout";
import { Room } from "../Room";
import { DemoTag, Headline, ScreenCard, Sticker } from "../ui";

// 0-11.8 s. ILLUSTRATED: the everyday job hunt, before SpotApply. Everything
// here is drawn (a flat character, fictional companies), and tagged so.

const NightBackdrop: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: "linear-gradient(180deg, #0E2338 0%, #163552 100%)" }}>
      {Array.from({ length: 9 }, (_, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: 60 + i * 120,
            top: 150 + (i % 3) * 30 + 8 * Math.sin(frame / 14 + i),
            width: 70 + (i % 4) * 22,
            height: 70 + (i % 4) * 22,
            borderRadius: "50%",
            background: "#FFD58A",
            opacity: 0.08 + 0.04 * Math.sin(frame / 9 + i * 2),
            filter: "blur(6px)",
          }}
        />
      ))}
    </AbsoluteFill>
  );
};

/** A reaction cut: the face, full screen, with a zoom punch at `punchAt`. */
const FaceCut: React.FC<{ readonly expr: Expr; readonly punchAt: number; readonly id: string; readonly bg?: string }> = ({
  expr,
  punchAt,
  id,
  bg = "linear-gradient(180deg, #102840 0%, #1A3E5E 100%)",
}) => {
  const frame = useCurrentFrame();
  const punch = prog(frame, punchAt, 3, ease.out);
  return (
    <AbsoluteFill style={{ background: bg, overflow: "hidden" }}>
      <div style={{ position: "absolute", left: 540 - 560, top: 980 - 600, width: 1120, height: 1120, scale: String(1 + 0.16 * punch) }}>
        <Person expr={expr} size={1120} blink={blinkAt(frame, 7) * (expr === "deadpan" ? 0.6 : 1)} glow="#7DD3FC" id={id} />
      </div>
    </AbsoluteFill>
  );
};

// ── 0-3.4 s: job hunting is a full-time job ─────────────────────────────────
export const HookScene: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill>
      <Room mode="night" expr="tired" zoom={lerp(1.0, 1.07, prog(frame, 0, 100, ease.inOut))} focusY={900} headBob={Math.sin(frame / 11) * 3} id="hook" />
      <Headline lines={["Job hunting", "= a *full-time job*"]} tones={["white", "white"]} at={6} />
      <Sticker text="(unpaid)" at={44} x={560} y={262 + 62 * 1.42 + 96} size={36} tone="ink" rotate={7} />
      <DemoTag opacity={1} dark text="Illustration" />
    </AbsoluteFill>
  );
};

// ── 3.4-5.1 s: twelve tabs open ──────────────────────────────────────────────
const TAB_TITLES = [
  "Careers | Northpoint Labs",
  "Jobs at Cadence Robotics",
  "Apply: Meridian Freight",
  "Senior Backend Engineer",
  "Harbor Analytics Careers",
  "Open roles | Vector Health",
  "Orchard Systems Jobs",
  "Job Application Form",
  "Software Engineer, Remote",
  "Backend roles, Austin",
  "Create an account to apply",
  "Upload your resume",
];
const TAB_AT = [0, 5, 9, 13, 16, 19, 22, 25, 27, 29, 31, 33].map((d) => 6 + d);
const FAV = ["#E4795E", "#5FA8D3", "#F2C14E", "#7E6CC8", "#4CAF8C", "#E06C9F"];

export const TabsScene: React.FC = () => {
  const frame = useCurrentFrame();
  const n = TAB_AT.filter((t) => frame >= t).length;
  const stripW = CARD.w - 40;
  const tabW = Math.min(230, stripW / Math.max(1, n));
  return (
    <AbsoluteFill>
      <NightBackdrop />
      <ScreenCard kind="laptop" label="careers page (tab 1 of many)">
        <div style={{ position: "absolute", inset: 0, background: "#F3F6FA" }}>
          {/* tab strip */}
          <div style={{ position: "absolute", left: 20, top: 12, width: stripW, height: 52, display: "flex", gap: 0 }}>
            {TAB_TITLES.slice(0, n).map((t, i) => {
              const pop = prog(frame, TAB_AT[i], 5, ease.out);
              return (
                <div
                  key={i}
                  style={{
                    width: tabW,
                    flex: "none",
                    height: 50,
                    boxSizing: "border-box",
                    borderRadius: "12px 12px 0 0",
                    background: i === n - 1 ? "#FFFFFF" : "#E3EAF2",
                    borderRight: "1px solid #D2DCE7",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: tabW > 70 ? "0 12px" : "0 8px",
                    overflow: "hidden",
                    scale: String(lerp(0.6, 1, pop)),
                    opacity: pop,
                    fontFamily: FONTS.sans,
                    fontSize: 18,
                    fontWeight: 600,
                    color: COLORS.inkSoft,
                    whiteSpace: "nowrap",
                  }}
                >
                  <span style={{ width: 20, height: 20, flex: "none", borderRadius: 5, background: FAV[i % FAV.length] }} />
                  {tabW > 80 ? <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{t}</span> : null}
                </div>
              );
            })}
          </div>
          {/* a careers page */}
          <div style={{ position: "absolute", left: 0, top: 62, right: 0, bottom: 0, background: "#FFFFFF", padding: "40px 48px", fontFamily: FONTS.sans }}>
            <div style={{ fontSize: 40, fontWeight: 800, color: COLORS.ink }}>{TAB_TITLES[Math.max(0, n - 1)]}</div>
            {[0.92, 0.7, 0.84, 0.55, 0.9, 0.66, 0.78].map((w, i) => (
              <div key={i} style={{ height: 18, width: `${w * 100}%`, borderRadius: 9, background: "#E8EEF5", marginTop: 26 }} />
            ))}
            <div style={{ marginTop: 40, width: 260, height: 64, borderRadius: 14, background: COLORS.accent, opacity: 0.85 }} />
          </div>
        </div>
      </ScreenCard>
      <Sticker text={`${n} tab${n === 1 ? "" : "s"} open`} at={6} x={70} y={290} size={70} tone="white" rotate={-2} />
      {n >= 12 ? <Sticker text="and counting" at={TAB_AT[11] + 2} x={470} y={404} size={34} tone="ink" rotate={5} /> : null}
      <DemoTag opacity={1} dark text="Illustration" />
    </AbsoluteFill>
  );
};

// ── 5.1-7.8 s: rewriting the same resume, again ─────────────────────────────
const FILES = [
  ["Resume.pdf", "Mon 9:02 PM"],
  ["Resume_final.pdf", "Tue 11:40 PM"],
  ["Resume_final_v2.pdf", "Wed 12:15 AM"],
  ["Resume_FINAL_final.pdf", "Thu 1:03 AM"],
  ["Resume_use_THIS_one.pdf", "Today 1:46 AM"],
] as const;
const FILE_AT = [7, 16, 24, 31, 38];

export const RewriteScene: React.FC = () => {
  const frame = useCurrentFrame();
  const cut = frame >= 57; // the reaction
  const last = FILES[4][0];
  const typedN = Math.floor(Math.max(0, frame - 43) * 1.1);
  return (
    <AbsoluteFill>
      {!cut ? (
        <>
          <NightBackdrop />
          <ScreenCard kind="laptop" label="Documents / Job search">
            <div style={{ position: "absolute", inset: 0, background: "#FFFFFF", fontFamily: FONTS.sans }}>
              <div style={{ display: "flex", padding: "26px 36px 14px", fontSize: 20, fontWeight: 700, color: COLORS.muted, borderBottom: "1px solid #E3EAF2" }}>
                <span style={{ flex: 1 }}>Name</span>
                <span style={{ width: 220 }}>Edited</span>
              </div>
              {FILES.map(([name, when], i) => {
                const p = prog(frame, FILE_AT[i], 6, ease.out);
                const isLast = i === FILES.length - 1;
                const shown = isLast ? last.slice(0, Math.min(last.length, typedN)) : name;
                return (
                  <div
                    key={name}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 18,
                      padding: "22px 36px",
                      borderBottom: "1px solid #EEF2F7",
                      background: isLast ? "#E0F2FE" : "transparent",
                      opacity: p,
                      translate: `0px ${(1 - p) * 20}px`,
                      fontSize: 30,
                      fontWeight: 600,
                      color: COLORS.ink,
                    }}
                  >
                    <DocIcon size={38} color="#E4572E" />
                    <span style={{ flex: 1, whiteSpace: "nowrap" }}>
                      {shown}
                      {isLast && shown.length < last.length ? <span style={{ display: "inline-block", width: 3, height: 32, background: COLORS.accent, marginLeft: 2, verticalAlign: "middle" }} /> : null}
                    </span>
                    <span style={{ width: 220, fontSize: 22, color: COLORS.muted, fontWeight: 500 }}>{when}</span>
                  </div>
                );
              })}
            </div>
          </ScreenCard>
        </>
      ) : (
        <FaceCut expr="cringe" punchAt={57} id="cut-cringe" />
      )}
      <Headline lines={["Rewriting the same resume.", "*Again.*"]} tones={["white", "white"]} at={4} size={56} />
      <DemoTag opacity={1} dark text="Illustration" />
    </AbsoluteFill>
  );
};

// ── 7.8-11.8 s: uploaded your resume? now type it again ─────────────────────
const Field: React.FC<{ readonly label: string; readonly value?: string; readonly caret?: boolean; readonly w?: number | string; readonly h?: number }> = ({
  label,
  value = "",
  caret,
  w = "100%",
  h = 62,
}) => (
  <div style={{ width: w }}>
    <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.inkSoft, marginBottom: 8 }}>
      {label} <span style={{ color: "#C81E1E" }}>*</span>
    </div>
    <div
      style={{
        height: h,
        boxSizing: "border-box",
        borderRadius: 10,
        border: `2px solid ${caret ? COLORS.accent : "#CBD5E1"}`,
        display: "flex",
        alignItems: h > 70 ? "flex-start" : "center",
        padding: h > 70 ? "14px 16px" : "0 16px",
        fontSize: 26,
        fontWeight: 500,
        color: COLORS.ink,
      }}
    >
      {value}
      {caret ? <span style={{ width: 3, height: 32, background: COLORS.accent, marginLeft: 2 }} /> : null}
    </div>
  </div>
);

export const RetypeScene: React.FC = () => {
  const frame = useCurrentFrame();
  const cut = frame >= 72; // the stare
  const company = "Brightwave Logistics";
  const typedN = Math.floor(Math.max(0, frame - 28) * 0.62);
  const caretOn = Math.floor(frame / 8) % 2 === 0 || typedN < company.length;
  return (
    <AbsoluteFill>
      {!cut ? (
        <>
          <NightBackdrop />
          <ScreenCard kind="laptop" label="Cadence Robotics · Careers">
            <div style={{ position: "absolute", inset: 0, background: "#FFFFFF", padding: "30px 40px", fontFamily: FONTS.sans, display: "flex", flexDirection: "column", gap: 20 }}>
              <div>
                <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.inkSoft, marginBottom: 8 }}>Resume/CV *</div>
                <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px", borderRadius: 12, border: "2px dashed #94A3B8", fontSize: 25, fontWeight: 600, color: COLORS.ink, opacity: prog(frame, 2, 8) }}>
                  <DocIcon size={30} color={COLORS.accent} /> Maya_Chen_Resume.pdf
                  <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8, color: COLORS.ok, fontSize: 22, fontWeight: 700 }}>
                    <CheckIcon size={24} color={COLORS.ok} stroke={3.2} /> Uploaded
                  </span>
                </div>
              </div>
              <div style={{ borderTop: "2px solid #EEF2F7", paddingTop: 16 }}>
                <div style={{ fontSize: 30, fontWeight: 800, color: COLORS.ink }}>Work experience</div>
                <div style={{ fontSize: 21, color: COLORS.muted, marginTop: 4 }}>Please enter your work history.</div>
              </div>
              <Field label="Company" value={company.slice(0, Math.min(company.length, typedN))} caret={caretOn} />
              <Field label="Job title" />
              <div style={{ display: "flex", gap: 20 }}>
                <Field label="Start date" w="50%" />
                <Field label="End date" w="50%" />
              </div>
              <Field label="Describe your role" h={110} />
            </div>
          </ScreenCard>
        </>
      ) : (
        <FaceCut expr="deadpan" punchAt={80} id="cut-deadpan" />
      )}
      <Headline lines={["Uploaded your resume?", "Now *type it again.*"]} tones={["white", "white"]} at={4} size={60} />
      {cut ? <Sticker text="…" at={86} x={790} y={1000} size={64} tone="white" rotate={-6} /> : null}
      <DemoTag opacity={1} dark text="Illustration" />
    </AbsoluteFill>
  );
};
