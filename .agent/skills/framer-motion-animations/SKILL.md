---
name: framer-motion-animations
description: "Applies high-fidelity fluid animations, scroll-linked transitions, layout morphing, and staggered micro-interactions using Framer Motion and Tailwind CSS."
---

# Framer Motion Animations Skill

## Goal
To implement smooth, high-fidelity, and performance-optimized animations in web interfaces using React/Next.js, Framer Motion, and Tailwind CSS. This skill avoids simple or rigid CSS transitions and ensures premium user experiences through micro-interactions, layout transitions, and page entrance/exit animations.

## Instructions

### 1. Avoid Rigid CSS Transitions
- Do not use standard CSS transitions (`transition: all 0.3s`) for complex layout morphing or state changes.
- Utilize Framer Motion's `layout` and `layoutId` props to morph shapes and containers fluidly.

### 2. Enter and Exit Transitions (`AnimatePresence`)
- Always wrap conditional components (e.g., modals, slide-overs, dropdowns) with `<AnimatePresence>` to enable exit animations.
- Set the `exit` prop on `motion` elements explicitly so they animate out before being unmounted from the DOM.
- Example: `<AnimatePresence mode="wait">` to orchestrate multi-step form step changes.

### 3. Performance & GPU Acceleration
- Prioritize animating transform properties (like `x`, `y`, `scale`, `rotate`) and `opacity` over layout-triggering properties (like `width`, `height`, `top`, `left`). Animating layout properties triggers browser reflow and hurts frame rate.
- If morphing size is necessary, use `layout` prop to let Framer Motion handle scale correction for child elements.
- Use the `will-change` CSS property/Tailwind utility or set `transformTemplate` / `translateZ(0)` to promote elements to their own GPU compositor layer.

### 4. Design System Tokens Alignment
- Never hardcode arbitrary spring values or duration values. Use consistent spring configurations:
  - **Stiff/Snappy (buttons, hover states):** `type: "spring", stiffness: 400, damping: 30`
  - **Fluid/Natural (modals, panel slides):** `type: "spring", stiffness: 300, damping: 35`
  - **Slow/Deliberate (page transitions):** `type: "spring", stiffness: 150, damping: 20`
- Align custom motion styles with Tailwind CSS tailwind-tailored spacing and color palettes.

### 5. Staggered Animations
- Use variants to orchestrate child animations. The parent element should define `transition: { staggerChildren: 0.05 }`, and children should define standard enter/exit variants to create natural flowing sequences.

---

## Examples

### Dynamic Staggered Menu Card List
```tsx
import { motion } from "framer-motion";

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { staggerChildren: 0.08, delayChildren: 0.2 }
  }
};

const itemVariants = {
  hidden: { y: 20, opacity: 0 },
  visible: {
    y: 0,
    opacity: 1,
    transition: { type: "spring", stiffness: 300, damping: 24 }
  }
};

export default function JobCardList({ items }) {
  return (
    <motion.div
      variants={containerVariants}
      initial="hidden"
      animate="visible"
      className="grid grid-cols-1 md:grid-cols-2 gap-4"
    >
      {items.map((item) => (
        <motion.div
          key={item.id}
          variants={itemVariants}
          whileHover={{ scale: 1.02, y: -4 }}
          whileTap={{ scale: 0.98 }}
          className="p-6 bg-slate-900 border border-slate-800 rounded-2xl shadow-xl cursor-pointer"
        >
          <h3 className="text-xl font-bold text-white">{item.title}</h3>
          <p className="text-slate-400 mt-2">{item.company}</p>
        </motion.div>
      ))}
    </motion.div>
  );
}
```

### Layout Morphing with layoutId (Tabs Underline)
```tsx
import { useState } from "react";
import { motion } from "framer-motion";

export default function TabBar() {
  const [activeTab, setActiveTab] = useState("jobs");
  const tabs = [
    { id: "jobs", label: "Matched Jobs" },
    { id: "applied", label: "Applied" },
    { id: "settings", label: "Preferences" }
  ];

  return (
    <div className="flex gap-2 bg-slate-950 p-2 rounded-xl border border-slate-850">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => setActiveTab(tab.id)}
          className={`relative px-4 py-2 text-sm font-semibold rounded-lg transition-colors ${
            activeTab === tab.id ? "text-white" : "text-slate-400 hover:text-slate-200"
          }`}
        >
          {activeTab === tab.id && (
            <motion.div
              layoutId="active-tab-glow"
              className="absolute inset-0 bg-indigo-500/20 border border-indigo-500/40 rounded-lg"
              transition={{ type: "spring", stiffness: 380, damping: 30 }}
            />
          )}
          <span className="relative z-10">{tab.label}</span>
        </button>
      ))}
    </div>
  );
}
```

---

## Constraints
- **Performance Budget:** Do not use complex scroll animations (e.g. tracking mouse/scroll positions with expensive React renders) without optimization hooks like `useScroll` and `useTransform` which animate directly on the CSS level without React re-render cycles.
- **Accessibility:** Respect system settings for reduced motion by checking `window.matchMedia('(prefers-reduced-motion: reduce)')` or wrap animations in standard media queries/utility hooks.
- **SSR Hydration:** When using exit animations, ensure initial renders match server states to avoid SSR hydration mismatches.
