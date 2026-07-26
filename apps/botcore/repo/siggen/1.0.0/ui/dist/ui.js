import { jsx, jsxs, Fragment } from "react/jsx-runtime";
import { forwardRef, createElement, useState, useCallback, useEffect, useRef } from "react";
import { useWsClient } from "@rlx/ui";
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
var defaultAttributes = {
  xmlns: "http://www.w3.org/2000/svg",
  width: 24,
  height: 24,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round"
};
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
const toKebabCase = (string) => string.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase().trim();
const createLucideIcon = (iconName, iconNode) => {
  const Component = forwardRef(
    ({
      color = "currentColor",
      size = 24,
      strokeWidth = 2,
      absoluteStrokeWidth,
      className = "",
      children,
      ...rest
    }, ref) => {
      return createElement(
        "svg",
        {
          ref,
          ...defaultAttributes,
          width: size,
          height: size,
          stroke: color,
          strokeWidth: absoluteStrokeWidth ? Number(strokeWidth) * 24 / Number(size) : strokeWidth,
          className: ["lucide", `lucide-${toKebabCase(iconName)}`, className].join(" "),
          ...rest
        },
        [
          ...iconNode.map(([tag, attrs]) => createElement(tag, attrs)),
          ...Array.isArray(children) ? children : [children]
        ]
      );
    }
  );
  Component.displayName = `${iconName}`;
  return Component;
};
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
const Play = createLucideIcon("Play", [
  ["polygon", { points: "5 3 19 12 5 21 5 3", key: "191637" }]
]);
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
const Plus = createLucideIcon("Plus", [
  ["path", { d: "M5 12h14", key: "1ays0h" }],
  ["path", { d: "M12 5v14", key: "s699le" }]
]);
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
const Square = createLucideIcon("Square", [
  ["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2", key: "afitv7" }]
]);
/**
 * @license lucide-react v0.344.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */
const X = createLucideIcon("X", [
  ["path", { d: "M18 6 6 18", key: "1bl5f8" }],
  ["path", { d: "m6 6 12 12", key: "d8bk6v" }]
]);
const SHAPES = ["sine", "square", "triangle", "sawtooth", "noise", "dc", "clock"];
const field = "rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200";
const btn = "inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40";
function Spark({ ws, topic, digital }) {
  const ref = useRef(null);
  const buf = useRef([]);
  useEffect(() => {
    const off = ws.subscribe(topic, (f) => {
      if (f.method !== "message") return;
      const v = f.payload?.value;
      if (typeof v === "number") {
        buf.current.push(v);
        if (buf.current.length > 120) buf.current.shift();
      }
    });
    let raf = 0;
    const draw = () => {
      raf = requestAnimationFrame(draw);
      const cv = ref.current;
      if (!cv) return;
      const ctx = cv.getContext("2d");
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
      if (cv.width !== W * dpr || cv.height !== H * dpr) {
        cv.width = W * dpr;
        cv.height = H * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = "#020617";
      ctx.fillRect(0, 0, W, H);
      const b = buf.current;
      if (b.length < 2) return;
      let lo = Math.min(...b), hi = Math.max(...b);
      if (hi - lo < 1e-6) {
        hi += 1;
        lo -= 1;
      }
      ctx.strokeStyle = digital ? "#e879f9" : "#38bdf8";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      b.forEach((v, i) => {
        const x = i / (b.length - 1) * W, y = H - 2 - (v - lo) / (hi - lo) * (H - 4);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
    };
    raf = requestAnimationFrame(draw);
    return () => {
      off();
      cancelAnimationFrame(raf);
    };
  }, [ws, topic, digital]);
  return /* @__PURE__ */ jsx("canvas", { ref, className: "h-8 w-24 rounded border border-slate-700" });
}
function SiggenView({ proxy }) {
  const ws = useWsClient();
  const proxyId = proxy.id ?? proxy.name ?? "";
  const stateTopic = `/siggen/${proxyId}/state`;
  const controlTopic = `/siggen/${proxyId}/control`;
  const [state, setState] = useState(null);
  const send = useCallback((action, args = {}) => {
    ws.publish(controlTopic, { action, ...args });
  }, [ws, controlTopic]);
  useEffect(() => {
    if (!proxyId) return;
    return ws.subscribe(stateTopic, (f) => {
      if (f.method === "message") setState(f.payload);
    });
  }, [proxyId, stateTopic, ws]);
  if (!state) return /* @__PURE__ */ jsx("div", { className: "p-3 text-xs text-slate-400", children: "Connecting to generator…" });
  const running = state.running;
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2 p-2 text-slate-200", children: [
    /* @__PURE__ */ jsxs("div", { className: "flex flex-wrap items-center gap-2", children: [
      /* @__PURE__ */ jsxs("button", { className: `${btn} ${running ? "bg-emerald-600" : "bg-slate-700"}`, onClick: () => send("set_running", { running: !running }), children: [
        running ? /* @__PURE__ */ jsx(Square, { size: 13 }) : /* @__PURE__ */ jsx(Play, { size: 13 }),
        " ",
        running ? "Running" : "Stopped"
      ] }),
      /* @__PURE__ */ jsx("label", { className: "text-xs text-slate-400", children: "Sample rate" }),
      /* @__PURE__ */ jsx(
        "input",
        {
          className: `${field} w-20`,
          type: "number",
          value: state.sample_hz,
          min: 1,
          max: 500,
          onChange: (e) => send("set_sample_rate", { sample_hz: Number(e.target.value) || 100 })
        }
      ),
      /* @__PURE__ */ jsx("span", { className: "text-xs text-slate-500", children: "Hz" }),
      /* @__PURE__ */ jsxs("button", { className: `${btn} bg-slate-700 ml-auto`, onClick: () => send("add_wave", { shape: "sine", freq_hz: 1 }), children: [
        /* @__PURE__ */ jsx(Plus, { size: 13 }),
        " Add wave"
      ] })
    ] }),
    state.waves.length === 0 && /* @__PURE__ */ jsx("div", { className: "text-xs text-slate-500", children: "No waves — add one to start generating." }),
    state.waves.map((w) => /* @__PURE__ */ jsxs("div", { className: "flex flex-wrap items-center gap-1.5 rounded bg-slate-900/60 px-2 py-1", children: [
      /* @__PURE__ */ jsx("input", { className: `${field} w-24`, value: w.label, placeholder: w.id, onChange: (e) => send("set_wave", { id: w.id, label: e.target.value }) }),
      /* @__PURE__ */ jsx("select", { className: field, value: w.shape, onChange: (e) => send("set_wave", { id: w.id, shape: e.target.value }), children: SHAPES.map((s) => /* @__PURE__ */ jsx("option", { value: s, children: s }, s)) }),
      /* @__PURE__ */ jsx("label", { className: "text-[10px] text-slate-500", children: "Hz" }),
      /* @__PURE__ */ jsx("input", { className: `${field} w-16`, type: "number", step: "0.1", value: w.freq_hz, onChange: (e) => send("set_wave", { id: w.id, freq_hz: Number(e.target.value) }) }),
      w.shape !== "clock" && w.shape !== "dc" && /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx("label", { className: "text-[10px] text-slate-500", children: "amp" }),
        /* @__PURE__ */ jsx("input", { className: `${field} w-14`, type: "number", step: "0.1", value: w.amplitude, onChange: (e) => send("set_wave", { id: w.id, amplitude: Number(e.target.value) }) })
      ] }),
      w.shape !== "clock" && /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx("label", { className: "text-[10px] text-slate-500", children: "off" }),
        /* @__PURE__ */ jsx("input", { className: `${field} w-14`, type: "number", step: "0.1", value: w.offset, onChange: (e) => send("set_wave", { id: w.id, offset: Number(e.target.value) }) })
      ] }),
      (w.shape === "square" || w.shape === "clock") && /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx("label", { className: "text-[10px] text-slate-500", children: "duty" }),
        /* @__PURE__ */ jsx("input", { className: `${field} w-14`, type: "number", step: "0.05", min: 0, max: 1, value: w.duty, onChange: (e) => send("set_wave", { id: w.id, duty: Number(e.target.value) }) })
      ] }),
      /* @__PURE__ */ jsx(Spark, { ws, topic: `/siggen/${proxyId}/ch/${w.id}`, digital: w.shape === "clock" }),
      /* @__PURE__ */ jsxs("label", { className: "flex items-center gap-1 text-[10px] text-slate-500", children: [
        /* @__PURE__ */ jsx("input", { type: "checkbox", checked: w.enabled, onChange: (e) => send("set_wave", { id: w.id, enabled: e.target.checked }) }),
        " on"
      ] }),
      /* @__PURE__ */ jsx("button", { className: "rounded p-1 text-slate-400 hover:text-rose-300", onClick: () => send("remove_wave", { id: w.id }), children: /* @__PURE__ */ jsx(X, { size: 13 }) })
    ] }, w.id))
  ] });
}
export {
  SiggenView as default
};
