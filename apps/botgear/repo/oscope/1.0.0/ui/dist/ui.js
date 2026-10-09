// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { createElement, forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApiFetch, useServiceRequest, useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/defaultAttributes.js
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
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/createLucideIcon.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var toKebabCase = (string) => string.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase().trim();
var createLucideIcon = (iconName, iconNode) => {
	const Component = forwardRef(({ color = "currentColor", size = 24, strokeWidth = 2, absoluteStrokeWidth, className = "", children, ...rest }, ref) => {
		return createElement("svg", {
			ref,
			...defaultAttributes,
			width: size,
			height: size,
			stroke: color,
			strokeWidth: absoluteStrokeWidth ? Number(strokeWidth) * 24 / Number(size) : strokeWidth,
			className: [
				"lucide",
				`lucide-${toKebabCase(iconName)}`,
				className
			].join(" "),
			...rest
		}, [...iconNode.map(([tag, attrs]) => createElement(tag, attrs)), ...Array.isArray(children) ? children : [children]]);
	});
	Component.displayName = `${iconName}`;
	return Component;
};
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/crosshair.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Crosshair = createLucideIcon("Crosshair", [
	["circle", {
		cx: "12",
		cy: "12",
		r: "10",
		key: "1mglay"
	}],
	["line", {
		x1: "22",
		x2: "18",
		y1: "12",
		y2: "12",
		key: "l9bcsi"
	}],
	["line", {
		x1: "6",
		x2: "2",
		y1: "12",
		y2: "12",
		key: "13hhkx"
	}],
	["line", {
		x1: "12",
		x2: "12",
		y1: "6",
		y2: "2",
		key: "10w3f3"
	}],
	["line", {
		x1: "12",
		x2: "12",
		y1: "22",
		y2: "18",
		key: "15g9kq"
	}]
]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/download.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Download = createLucideIcon("Download", [
	["path", {
		d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4",
		key: "ih7n3h"
	}],
	["polyline", {
		points: "7 10 12 15 17 10",
		key: "2ggqvy"
	}],
	["line", {
		x1: "12",
		x2: "12",
		y1: "15",
		y2: "3",
		key: "1vk2je"
	}]
]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/play.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Play = createLucideIcon("Play", [["polygon", {
	points: "5 3 19 12 5 21 5 3",
	key: "191637"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/plus.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Plus = createLucideIcon("Plus", [["path", {
	d: "M5 12h14",
	key: "1ays0h"
}], ["path", {
	d: "M12 5v14",
	key: "s699le"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/rotate-ccw.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var RotateCcw = createLucideIcon("RotateCcw", [["path", {
	d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8",
	key: "1357e3"
}], ["path", {
	d: "M3 3v5h5",
	key: "1xhq8a"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/square.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Square = createLucideIcon("Square", [["rect", {
	width: "18",
	height: "18",
	x: "3",
	y: "3",
	rx: "2",
	key: "afitv7"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/trash-2.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Trash2 = createLucideIcon("Trash2", [
	["path", {
		d: "M3 6h18",
		key: "d0wm0j"
	}],
	["path", {
		d: "M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6",
		key: "4alrt4"
	}],
	["path", {
		d: "M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2",
		key: "v07s0e"
	}],
	["line", {
		x1: "10",
		x2: "10",
		y1: "11",
		y2: "17",
		key: "1uufr5"
	}],
	["line", {
		x1: "14",
		x2: "14",
		y1: "11",
		y2: "17",
		key: "xtxkd"
	}]
]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/x.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var X = createLucideIcon("X", [["path", {
	d: "M18 6 6 18",
	key: "1bl5f8"
}], ["path", {
	d: "m6 6 12 12",
	key: "d8bk6v"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/zap.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Zap = createLucideIcon("Zap", [["polygon", {
	points: "13 2 3 14 12 14 11 22 21 10 12 10 13 2",
	key: "45s27k"
}]]);
//#endregion
//#region ../botgear/repo/oscope/1.0.0/ui/View.tsx
var field = "rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200";
var btn = "inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40";
var TB_PRESETS = [
	200,
	500,
	1e3,
	2e3,
	5e3,
	1e4,
	2e4
];
var ANALOG_DIVS = 8;
function pruneBuf(b, cutoff) {
	let drop = 0;
	while (drop < b.t.length - 1 && b.t[drop] < cutoff) drop++;
	if (drop > 0) {
		b.t.splice(0, drop);
		b.v.splice(0, drop);
	}
}
function windowStats(b, t0, t1) {
	if (!b || !b.t.length) return null;
	let min = Infinity, max = -Infinity, sum = 0, n = 0;
	let crossings = 0, firstT = NaN, lastT = NaN, prev = NaN;
	for (let i = 0; i < b.t.length; i++) {
		const t = b.t[i];
		if (t < t0 || t > t1) continue;
		const v = b.v[i];
		if (!Number.isFinite(v)) continue;
		if (v < min) min = v;
		if (v > max) max = v;
		sum += v;
		n++;
		if (isNaN(firstT)) firstT = t;
		lastT = t;
		prev = v;
	}
	if (!n) return null;
	const mean = sum / n;
	prev = NaN;
	for (let i = 0; i < b.t.length; i++) {
		const t = b.t[i];
		if (t < t0 || t > t1) continue;
		const v = b.v[i];
		if (!isNaN(prev) && prev < mean && v >= mean) crossings++;
		prev = v;
	}
	const span = lastT - firstT || 0;
	const freq = span > 0 ? crossings / span : 0;
	return {
		min,
		max,
		mean,
		pp: max - min,
		freq,
		n
	};
}
function OscopeView({ proxy }) {
	const ws = useWsClient();
	const apiFetch = useApiFetch();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/oscope/${proxyId}/state`;
	const frameTopic = `/oscope/${proxyId}/frame`;
	const trigTopic = `/oscope/${proxyId}/trigger`;
	const controlTopic = `/oscope/${proxyId}/control`;
	const ctl = useServiceRequest(controlTopic, {
		timeoutMs: 8e3,
		errorField: "error"
	});
	const [state, setState] = useState(null);
	const [cursorsOn, setCursorsOn] = useState(false);
	const [picker, setPicker] = useState(false);
	const [now, setNow] = useState(Date.now() / 1e3);
	const stateRef = useRef(null);
	const bufs = useRef(/* @__PURE__ */ new Map());
	const lastFrame = useRef({
		t: Date.now() / 1e3,
		clientMs: performance.now()
	});
	const lastTrig = useRef(null);
	const canvasRef = useRef(null);
	const wrapRef = useRef(null);
	const cursors = useRef({
		a: .35,
		b: .65,
		drag: null
	});
	const view = useRef({
		vZoom: 1,
		vPan: 0,
		tPan: 0
	});
	const panDrag = useRef(null);
	const [, setViewNudge] = useState(0);
	const resetView = () => {
		view.current = {
			vZoom: 1,
			vPan: 0,
			tPan: 0
		};
		setViewNudge((n) => n + 1);
	};
	const send = useCallback((action, args = {}) => {
		ws.publish(controlTopic, {
			action,
			...args
		});
	}, [ws, controlTopic]);
	useEffect(() => {
		if (!proxyId) return;
		const offState = ws.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			const s = f.payload;
			setState(s);
			stateRef.current = s;
			const live = new Set((s.channels || []).map((c) => c.id));
			for (const id of [...bufs.current.keys()]) if (!live.has(id)) bufs.current.delete(id);
		});
		const offFrame = ws.subscribe(frameTopic, (f) => {
			if (f.method !== "message") return;
			const fr = f.payload;
			lastFrame.current = {
				t: fr.t,
				clientMs: performance.now()
			};
			const bufS = stateRef.current?.buffer_s ?? 20;
			for (const [cid, samples] of Object.entries(fr.ch || {})) {
				let b = bufs.current.get(cid);
				if (!b) {
					b = {
						t: [],
						v: []
					};
					bufs.current.set(cid, b);
				}
				for (const [t, v] of samples) {
					b.t.push(t);
					b.v.push(v);
				}
			}
			const cutoff = fr.t - bufS;
			for (const b of bufs.current.values()) pruneBuf(b, cutoff);
			if (fr.trig) lastTrig.current = fr.trig;
		});
		const offTrig = ws.subscribe(trigTopic, (f) => {
			if (f.method !== "message") return;
			lastTrig.current = f.payload;
		});
		return () => {
			offState();
			offFrame();
			offTrig();
		};
	}, [
		proxyId,
		stateTopic,
		frameTopic,
		trigTopic,
		ws
	]);
	useEffect(() => {
		let raf = 0;
		const draw = () => {
			raf = requestAnimationFrame(draw);
			const cv = canvasRef.current, s = stateRef.current;
			if (!cv || !s) return;
			const ctx = cv.getContext("2d");
			if (!ctx) return;
			const dpr = window.devicePixelRatio || 1;
			const cssW = cv.clientWidth, cssH = cv.clientHeight;
			if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
				cv.width = Math.round(cssW * dpr);
				cv.height = Math.round(cssH * dpr);
			}
			ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
			drawScope(ctx, cssW, cssH, s, bufs.current, lastFrame.current, lastTrig.current, cursorsOn ? cursors.current : null, view.current);
		};
		raf = requestAnimationFrame(draw);
		return () => cancelAnimationFrame(raf);
	}, [cursorsOn]);
	useEffect(() => {
		const id = setInterval(() => setNow(Date.now() / 1e3), 250);
		return () => clearInterval(id);
	}, []);
	const onCanvasDown = (e) => {
		if (!canvasRef.current) return;
		e.target.setPointerCapture(e.pointerId);
		if (cursorsOn) {
			const rect = canvasRef.current.getBoundingClientRect();
			const fx = (e.clientX - rect.left) / rect.width;
			const c = cursors.current;
			c.drag = Math.abs(fx - c.a) <= Math.abs(fx - c.b) ? "a" : "b";
			c[c.drag] = Math.min(1, Math.max(0, fx));
		} else panDrag.current = {
			x: e.clientX,
			y: e.clientY
		};
	};
	const onCanvasMove = (e) => {
		if (!canvasRef.current) return;
		if (cursorsOn) {
			const c = cursors.current;
			if (!c.drag) return;
			const rect = canvasRef.current.getBoundingClientRect();
			c[c.drag] = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
		} else if (panDrag.current) {
			const rect = canvasRef.current.getBoundingClientRect();
			const dx = e.clientX - panDrag.current.x, dy = e.clientY - panDrag.current.y;
			panDrag.current = {
				x: e.clientX,
				y: e.clientY
			};
			view.current.vPan += dy;
			const tb = (stateRef.current?.timebase_ms ?? 2e3) / 1e3;
			view.current.tPan = Math.max(0, view.current.tPan + dx / rect.width * tb);
		}
	};
	const onCanvasUp = () => {
		cursors.current.drag = null;
		panDrag.current = null;
	};
	const onCanvasWheel = (e) => {
		e.stopPropagation();
		const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
		view.current.vZoom = Math.min(50, Math.max(.05, view.current.vZoom * f));
	};
	const s = state;
	const channels = s?.channels ?? [];
	const trig = s?.trigger;
	const tbSec = (s?.timebase_ms ?? 2e3) / 1e3;
	const stats = useMemo(() => {
		const t1 = lastFrame.current.t, t0 = t1 - tbSec;
		return channels.map((c) => ({
			c,
			m: windowStats(bufs.current.get(c.id), t0, t1)
		}));
	}, [
		now,
		channels,
		tbSec
	]);
	const exportCsv = () => {
		const t1 = lastFrame.current.t, t0 = t1 - (s?.buffer_s ?? 20);
		const rows = [];
		const header = ["t", ...channels.map((c) => c.label || c.id)];
		rows.push(header.join(","));
		const cols = channels.map((c) => bufs.current.get(c.id) || {
			t: [],
			v: []
		});
		const GRID = 5e-4;
		const byKey = /* @__PURE__ */ new Map();
		for (let i = 0; i < cols.length; i++) for (let j = 0; j < cols[i].t.length; j++) {
			const t = cols[i].t[j];
			if (t < t0 || t > t1) continue;
			const key = Math.round(t / GRID);
			let r = byKey.get(key);
			if (!r) {
				r = {
					t,
					vals: new Array(cols.length).fill("")
				};
				byKey.set(key, r);
			}
			r.vals[i] = String(cols[i].v[j]);
		}
		for (const key of [...byKey.keys()].sort((a, b) => a - b)) {
			const r = byKey.get(key);
			rows.push([r.t.toFixed(4), ...r.vals].join(","));
		}
		const blob = new Blob([rows.join("\n")], { type: "text/csv" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = `oscope-${proxyId}-${Math.round(t1)}.csv`;
		a.click();
		URL.revokeObjectURL(a.href);
	};
	if (!s) return /* @__PURE__ */ jsx("div", {
		className: "p-3 text-xs text-slate-400",
		children: "Connecting to scope…"
	});
	const running = s.running;
	return /* @__PURE__ */ jsxs("div", {
		className: "flex flex-col gap-2 p-2 text-slate-200",
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex flex-wrap items-center gap-2",
				children: [
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} ${running ? "bg-emerald-600" : "bg-slate-700"}`,
						onClick: () => send("set_running", { running: !running }),
						children: [
							running ? /* @__PURE__ */ jsx(Square, { size: 13 }) : /* @__PURE__ */ jsx(Play, { size: 13 }),
							" ",
							running ? "Stop" : "Run"
						]
					}),
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} bg-amber-600 disabled:bg-slate-700`,
						disabled: !trig?.channel_id,
						title: trig?.channel_id ? "Arm single-shot" : "Set a trigger channel first",
						onClick: () => send("arm_single"),
						children: [/* @__PURE__ */ jsx(Zap, { size: 13 }), " Single"]
					}),
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} bg-slate-700`,
						onClick: () => send("clear"),
						children: [/* @__PURE__ */ jsx(Trash2, { size: 13 }), " Clear"]
					}),
					/* @__PURE__ */ jsx("span", { className: "mx-1 h-4 w-px bg-slate-700" }),
					/* @__PURE__ */ jsx("label", {
						className: "text-xs text-slate-400",
						children: "Time"
					}),
					/* @__PURE__ */ jsxs("select", {
						className: field,
						value: s.timebase_ms,
						onChange: (e) => send("set_timebase", { timebase_ms: Number(e.target.value) }),
						children: [TB_PRESETS.map((p) => /* @__PURE__ */ jsx("option", {
							value: p,
							children: p >= 1e3 ? `${p / 1e3}s` : `${p}ms`
						}, p)), !TB_PRESETS.includes(s.timebase_ms) && /* @__PURE__ */ jsxs("option", {
							value: s.timebase_ms,
							children: [s.timebase_ms, "ms"]
						})]
					}),
					/* @__PURE__ */ jsx("span", { className: "mx-1 h-4 w-px bg-slate-700" }),
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} ${cursorsOn ? "bg-sky-600" : "bg-slate-700"}`,
						onClick: () => setCursorsOn((x) => !x),
						children: [/* @__PURE__ */ jsx(Crosshair, { size: 13 }), " Cursors"]
					}),
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} bg-slate-700`,
						onClick: exportCsv,
						children: [/* @__PURE__ */ jsx(Download, { size: 13 }), " CSV"]
					}),
					/* @__PURE__ */ jsxs("button", {
						className: `${btn} bg-slate-700`,
						onClick: resetView,
						title: "Reset view. On the plot: drag = pan, wheel = vertical zoom, drag the bottom edge to resize.",
						children: [/* @__PURE__ */ jsx(RotateCcw, { size: 13 }), " Fit"]
					}),
					/* @__PURE__ */ jsxs("span", {
						className: "ml-auto text-xs text-slate-400",
						children: [s.mode.toUpperCase(), s.last_trigger ? ` · trig#${s.last_trigger.seq}` : ""]
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex flex-wrap items-center gap-2 rounded bg-slate-900/60 px-2 py-1",
				children: [
					/* @__PURE__ */ jsx("span", {
						className: "text-xs font-medium text-slate-400",
						children: "Trigger"
					}),
					/* @__PURE__ */ jsxs("select", {
						className: field,
						value: trig?.channel_id ?? "",
						onChange: (e) => send("set_trigger", { channel_id: e.target.value || null }),
						children: [/* @__PURE__ */ jsx("option", {
							value: "",
							children: "— off (roll) —"
						}), channels.map((c) => /* @__PURE__ */ jsx("option", {
							value: c.id,
							children: c.label || c.id
						}, c.id))]
					}),
					/* @__PURE__ */ jsxs("select", {
						className: field,
						value: trig?.edge,
						disabled: !trig?.channel_id,
						onChange: (e) => send("set_trigger", { edge: e.target.value }),
						children: [
							/* @__PURE__ */ jsx("option", {
								value: "rising",
								children: "↑ rising"
							}),
							/* @__PURE__ */ jsx("option", {
								value: "falling",
								children: "↓ falling"
							}),
							/* @__PURE__ */ jsx("option", {
								value: "either",
								children: "↕ either"
							})
						]
					}),
					/* @__PURE__ */ jsx("label", {
						className: "text-xs text-slate-400",
						children: "level"
					}),
					/* @__PURE__ */ jsx("input", {
						className: `${field} w-20`,
						type: "number",
						step: "0.1",
						value: trig?.level ?? 0,
						disabled: !trig?.channel_id,
						onChange: (e) => send("set_trigger", { level: Number(e.target.value) })
					}),
					/* @__PURE__ */ jsxs("select", {
						className: field,
						value: trig?.mode,
						disabled: !trig?.channel_id,
						onChange: (e) => send("set_trigger", { mode: e.target.value }),
						children: [
							/* @__PURE__ */ jsx("option", {
								value: "auto",
								children: "auto"
							}),
							/* @__PURE__ */ jsx("option", {
								value: "normal",
								children: "normal"
							}),
							/* @__PURE__ */ jsx("option", {
								value: "single",
								children: "single"
							})
						]
					}),
					/* @__PURE__ */ jsxs("label", {
						className: "flex items-center gap-1 text-xs text-slate-400",
						children: [/* @__PURE__ */ jsx("input", {
							type: "checkbox",
							checked: !!trig?.publish,
							disabled: !trig?.channel_id,
							onChange: (e) => send("set_trigger", { publish: e.target.checked })
						}), "publish"]
					})
				]
			}),
			/* @__PURE__ */ jsx("div", {
				ref: wrapRef,
				className: "relative h-[340px] min-h-[180px] w-full resize-y overflow-hidden rounded border border-slate-700 bg-slate-950",
				children: /* @__PURE__ */ jsx("canvas", {
					ref: canvasRef,
					className: "nowheel nodrag nopan h-full w-full touch-none cursor-crosshair",
					onPointerDown: onCanvasDown,
					onPointerMove: onCanvasMove,
					onPointerUp: onCanvasUp,
					onPointerLeave: onCanvasUp,
					onWheel: onCanvasWheel
				})
			}),
			stats.some((x) => x.m) && /* @__PURE__ */ jsx("div", {
				className: "grid grid-cols-2 gap-x-4 gap-y-0.5 rounded bg-slate-900/60 px-2 py-1 text-[11px] md:grid-cols-3",
				children: stats.filter((x) => x.m).map(({ c, m }) => /* @__PURE__ */ jsxs("div", {
					className: "flex items-center gap-1.5 truncate",
					children: [
						/* @__PURE__ */ jsx("span", {
							className: "inline-block h-2 w-2 rounded-sm",
							style: { background: c.color }
						}),
						/* @__PURE__ */ jsx("span", {
							className: "truncate text-slate-300",
							children: c.label || c.id
						}),
						/* @__PURE__ */ jsxs("span", {
							className: "ml-auto tabular-nums text-slate-400",
							children: [
								"μ",
								m.mean.toFixed(2),
								" pp",
								m.pp.toFixed(2),
								" ",
								m.freq >= .05 ? `${m.freq.toFixed(1)}Hz` : ""
							]
						})
					]
				}, c.id))
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex flex-col gap-1",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "flex items-center justify-between",
						children: [/* @__PURE__ */ jsx("span", {
							className: "text-xs font-medium text-slate-400",
							children: "Channels"
						}), /* @__PURE__ */ jsxs("button", {
							className: `${btn} bg-slate-700`,
							onClick: () => setPicker((x) => !x),
							children: [/* @__PURE__ */ jsx(Plus, { size: 13 }), " Add channel"]
						})]
					}),
					channels.length === 0 && /* @__PURE__ */ jsx("div", {
						className: "text-xs text-slate-500",
						children: "No channels — add one to start plotting."
					}),
					channels.map((c) => /* @__PURE__ */ jsxs("div", {
						className: "flex flex-wrap items-center gap-1.5 rounded bg-slate-900/60 px-2 py-1",
						children: [
							/* @__PURE__ */ jsx("input", {
								type: "color",
								value: c.color,
								className: "h-5 w-6 cursor-pointer rounded bg-transparent",
								onChange: (e) => send("set_channel", {
									id: c.id,
									color: e.target.value
								})
							}),
							/* @__PURE__ */ jsx("input", {
								className: `${field} w-24`,
								value: c.label,
								placeholder: c.id,
								onChange: (e) => send("set_channel", {
									id: c.id,
									label: e.target.value
								})
							}),
							/* @__PURE__ */ jsxs("span", {
								className: "max-w-[210px] truncate text-[10px] text-slate-500",
								title: `${c.source_topic} · ${c.field}`,
								children: [
									c.source_topic,
									" · ",
									c.field
								]
							}),
							/* @__PURE__ */ jsxs("select", {
								className: field,
								value: c.kind,
								onChange: (e) => send("set_channel", {
									id: c.id,
									kind: e.target.value
								}),
								children: [/* @__PURE__ */ jsx("option", {
									value: "analog",
									children: "analog"
								}), /* @__PURE__ */ jsx("option", {
									value: "digital",
									children: "digital"
								})]
							}),
							c.kind === "analog" && /* @__PURE__ */ jsxs(Fragment, { children: [/* @__PURE__ */ jsxs("label", {
								className: "flex items-center gap-1 text-[10px] text-slate-500",
								title: "auto-fit the trace to its range",
								children: [/* @__PURE__ */ jsx("input", {
									type: "checkbox",
									checked: c.auto,
									onChange: (e) => send("set_channel", {
										id: c.id,
										auto: e.target.checked
									})
								}), " auto"]
							}), !c.auto && /* @__PURE__ */ jsxs(Fragment, { children: [
								/* @__PURE__ */ jsx("label", {
									className: "text-[10px] text-slate-500",
									children: "×/div"
								}),
								/* @__PURE__ */ jsx("input", {
									className: `${field} w-16`,
									type: "number",
									step: "0.1",
									value: c.scale,
									onChange: (e) => send("set_channel", {
										id: c.id,
										scale: Number(e.target.value) || 1,
										auto: false
									})
								}),
								/* @__PURE__ */ jsx("label", {
									className: "text-[10px] text-slate-500",
									children: "off"
								}),
								/* @__PURE__ */ jsx("input", {
									className: `${field} w-16`,
									type: "number",
									step: "0.1",
									value: c.offset,
									onChange: (e) => send("set_channel", {
										id: c.id,
										offset: Number(e.target.value),
										auto: false
									})
								})
							] })] }),
							/* @__PURE__ */ jsxs("label", {
								className: "flex items-center gap-1 text-[10px] text-slate-500",
								children: [/* @__PURE__ */ jsx("input", {
									type: "checkbox",
									checked: c.enabled,
									onChange: (e) => send("set_channel", {
										id: c.id,
										enabled: e.target.checked
									})
								}), " on"]
							}),
							/* @__PURE__ */ jsx("button", {
								className: "ml-auto rounded p-1 text-slate-400 hover:text-rose-300",
								onClick: () => send("remove_channel", { id: c.id }),
								children: /* @__PURE__ */ jsx(X, { size: 13 })
							})
						]
					}, c.id))
				]
			}),
			picker && /* @__PURE__ */ jsx(SourcePicker, {
				apiFetch,
				ctl,
				onAdd: (a) => {
					send("add_channel", a);
					setPicker(false);
				},
				onClose: () => setPicker(false)
			})
		]
	});
}
function SourcePicker({ apiFetch, onAdd, onClose }) {
	const ws = useWsClient();
	const [found, setFound] = useState([]);
	const [loading, setLoading] = useState(true);
	const [mTopic, setMTopic] = useState("");
	const [mField, setMField] = useState("value");
	const [mKind, setMKind] = useState("analog");
	useEffect(() => {
		let alive = true;
		(async () => {
			try {
				const metas = await apiFetch("/v1/service-meta-list").catch(() => []);
				const proxies = await apiFetch("/v1/service-proxy-list").catch(() => []);
				const types = new Set((Array.isArray(metas) ? metas : []).filter((m) => Array.isArray(m?.implements) && m.implements.includes("signal_source")).map((m) => m.name ?? m.type).filter(Boolean));
				const typeOf = (p) => p?.type ?? p?.typeKey ?? (typeof p?.name === "string" ? p.name.split("-")[0] : void 0);
				const srcs = (Array.isArray(proxies) ? proxies : []).filter((p) => types.has(typeOf(p)));
				const out = [];
				for (const p of srcs) {
					const pid = p.id ?? p.name;
					const ptype = p.type ?? String(pid).split("-")[0];
					try {
						const rep = await requestOnce(ws, `/${ptype}/${pid}/control`, { action: "list_signals" }, 4e3);
						for (const sig of rep?.signals ?? []) out.push({
							...sig,
							src: String(pid)
						});
					} catch {}
				}
				if (alive) {
					setFound(out);
					setLoading(false);
				}
			} catch {
				if (alive) setLoading(false);
			}
		})();
		return () => {
			alive = false;
		};
	}, [apiFetch, ws]);
	return /* @__PURE__ */ jsx("div", {
		className: "fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4",
		onClick: onClose,
		children: /* @__PURE__ */ jsxs("div", {
			className: "max-h-[80vh] w-[460px] overflow-auto rounded-lg border border-slate-700 bg-slate-900 p-3 text-slate-200",
			onClick: (e) => e.stopPropagation(),
			children: [
				/* @__PURE__ */ jsxs("div", {
					className: "mb-2 flex items-center justify-between",
					children: [/* @__PURE__ */ jsx("span", {
						className: "text-sm font-medium",
						children: "Add channel"
					}), /* @__PURE__ */ jsx("button", {
						onClick: onClose,
						className: "text-slate-400 hover:text-slate-200",
						children: /* @__PURE__ */ jsx(X, { size: 16 })
					})]
				}),
				/* @__PURE__ */ jsx("div", {
					className: "mb-2 text-xs font-medium text-slate-400",
					children: "Discovered signals"
				}),
				loading && /* @__PURE__ */ jsx("div", {
					className: "text-xs text-slate-500",
					children: "Scanning signal sources…"
				}),
				!loading && found.length === 0 && /* @__PURE__ */ jsx("div", {
					className: "text-xs text-slate-500",
					children: "No signal_source services running. Use manual binding below (e.g. a raspi/arduino pin topic)."
				}),
				/* @__PURE__ */ jsx("div", {
					className: "flex flex-col gap-1",
					children: found.map((sig, i) => /* @__PURE__ */ jsxs("button", {
						className: "flex items-center gap-2 rounded bg-slate-800 px-2 py-1 text-left text-xs hover:bg-slate-700",
						onClick: () => onAdd({
							source_topic: sig.topic,
							field: sig.field,
							kind: sig.kind,
							label: sig.label
						}),
						children: [
							/* @__PURE__ */ jsx("span", {
								className: `rounded px-1 text-[10px] ${sig.kind === "digital" ? "bg-fuchsia-700" : "bg-sky-700"}`,
								children: sig.kind
							}),
							/* @__PURE__ */ jsx("span", {
								className: "truncate",
								children: sig.label
							}),
							/* @__PURE__ */ jsx("span", {
								className: "ml-auto truncate text-[10px] text-slate-500",
								children: sig.src
							})
						]
					}, i))
				}),
				/* @__PURE__ */ jsx("div", {
					className: "mt-3 mb-1 text-xs font-medium text-slate-400",
					children: "Manual binding (any topic)"
				}),
				/* @__PURE__ */ jsxs("div", {
					className: "flex flex-wrap items-center gap-1.5",
					children: [
						/* @__PURE__ */ jsx("input", {
							className: `${field} flex-1`,
							placeholder: "/arduino/arduino-1/pin/5",
							value: mTopic,
							onChange: (e) => setMTopic(e.target.value)
						}),
						/* @__PURE__ */ jsx("input", {
							className: `${field} w-24`,
							placeholder: "value",
							value: mField,
							onChange: (e) => setMField(e.target.value)
						}),
						/* @__PURE__ */ jsxs("select", {
							className: field,
							value: mKind,
							onChange: (e) => setMKind(e.target.value),
							children: [/* @__PURE__ */ jsx("option", {
								value: "analog",
								children: "analog"
							}), /* @__PURE__ */ jsx("option", {
								value: "digital",
								children: "digital"
							})]
						}),
						/* @__PURE__ */ jsx("button", {
							className: `${btn} bg-sky-600 disabled:bg-slate-700`,
							disabled: !mTopic,
							onClick: () => onAdd({
								source_topic: mTopic,
								field: mField || "value",
								kind: mKind
							}),
							children: "Add"
						})
					]
				})
			]
		})
	});
}
function requestOnce(ws, topic, payload, timeoutMs) {
	return new Promise((resolve, reject) => {
		const reply = `/reply/oscope-picker/${Math.random().toString(36).slice(2)}`;
		const timer = setTimeout(() => {
			off();
			reject(/* @__PURE__ */ new Error("timeout"));
		}, timeoutMs);
		const off = ws.subscribe(reply, (f) => {
			if (f.method !== "message") return;
			clearTimeout(timer);
			off();
			resolve(f.payload);
		});
		ws.publish(topic, {
			...payload,
			reply_to: reply
		});
	});
}
function drawScope(ctx, W, H, s, bufs, lastFrame, lastTrig, cursors, view) {
	ctx.clearRect(0, 0, W, H);
	ctx.fillStyle = "#020617";
	ctx.fillRect(0, 0, W, H);
	const padL = 8, padR = 8, padT = 8, padB = 8;
	const plotL = padL, plotR = W - padR, plotW = plotR - plotL;
	const plotT = padT, plotB = H - padB;
	const analogCh = s.channels.filter((c) => c.enabled && c.kind === "analog");
	const digitalCh = s.channels.filter((c) => c.enabled && c.kind === "digital");
	const rowH = 22, dGap = 6;
	const digTop = plotB - digitalCh.length * rowH;
	const aT = plotT, aB = digitalCh.length ? digTop - dGap : plotB;
	const aH = Math.max(20, aB - aT), aMid = (aT + aB) / 2;
	const divPx = aH / ANALOG_DIVS;
	const tbSec = s.timebase_ms / 1e3;
	let tEnd = lastFrame.t - view.tPan;
	const trigAlign = lastTrig && s.trigger.channel_id && s.trigger.mode !== "auto";
	let tStart;
	if (trigAlign) {
		tStart = lastTrig.t - tbSec * .3;
		tEnd = lastTrig.t + tbSec * .7;
	} else tStart = tEnd - tbSec;
	const applyView = (y) => aMid + (y - aMid) * view.vZoom + view.vPan;
	const xOf = (t) => plotL + (t - tStart) / (tEnd - tStart) * plotW;
	ctx.strokeStyle = "#1e293b";
	ctx.lineWidth = 1;
	ctx.beginPath();
	for (let i = 0; i <= 10; i++) {
		const x = plotL + plotW * i / 10;
		ctx.moveTo(x, aT);
		ctx.lineTo(x, aB);
	}
	for (let i = 0; i <= ANALOG_DIVS; i++) {
		const y = aT + aH * i / ANALOG_DIVS;
		ctx.moveTo(plotL, y);
		ctx.lineTo(plotR, y);
	}
	ctx.stroke();
	ctx.strokeStyle = "#334155";
	ctx.beginPath();
	ctx.moveTo(plotL, aMid);
	ctx.lineTo(plotR, aMid);
	ctx.stroke();
	const yMap = (c) => {
		if (c.auto) {
			const b = bufs.get(c.id);
			let mn = Infinity, mx = -Infinity;
			if (b) for (let i = 0; i < b.t.length; i++) {
				const t = b.t[i];
				if (t < tStart || t > tEnd) continue;
				const v = b.v[i];
				if (!Number.isFinite(v)) continue;
				if (v < mn) mn = v;
				if (v > mx) mx = v;
			}
			if (!isFinite(mn)) return () => aMid;
			let range = mx - mn;
			if (range < 1e-9) range = Math.max(1, Math.abs(mx));
			const upd = range / (ANALOG_DIVS * .75);
			const center = (mn + mx) / 2;
			return (v) => applyView(aMid - (v - center) / upd * divPx);
		}
		const scale = c.scale || 1;
		return (v) => applyView(aMid - (v / scale + c.offset) * divPx);
	};
	ctx.save();
	ctx.beginPath();
	ctx.rect(plotL, aT, plotW, aH);
	ctx.clip();
	for (const c of analogCh) {
		const b = bufs.get(c.id);
		if (!b || !b.t.length) continue;
		const yOf = yMap(c);
		ctx.strokeStyle = c.color;
		ctx.lineWidth = 1.4;
		ctx.beginPath();
		let started = false;
		let carry = null;
		for (let i = 0; i < b.t.length; i++) {
			if (b.t[i] < tStart - .05) {
				carry = b.v[i];
				continue;
			}
			const x = xOf(b.t[i]), y = yOf(b.v[i]);
			if (!started) {
				if (carry !== null && Number.isFinite(carry)) {
					ctx.moveTo(xOf(tStart), yOf(carry));
					ctx.lineTo(x, y);
				} else ctx.moveTo(x, y);
				started = true;
			} else ctx.lineTo(x, y);
			if (b.t[i] > tEnd) break;
		}
		if (!started && carry !== null && Number.isFinite(carry)) {
			const y = yOf(carry);
			ctx.moveTo(xOf(tStart), y);
			ctx.lineTo(xOf(tEnd), y);
		}
		ctx.stroke();
	}
	const trCh = analogCh.find((c) => c.id === s.trigger.channel_id);
	if (trCh) {
		const y = yMap(trCh)(s.trigger.level);
		ctx.strokeStyle = "#f59e0b";
		ctx.setLineDash([4, 3]);
		ctx.lineWidth = 1;
		ctx.beginPath();
		ctx.moveTo(plotL, y);
		ctx.lineTo(plotR, y);
		ctx.stroke();
		ctx.setLineDash([]);
		ctx.fillStyle = "#f59e0b";
		ctx.font = "10px monospace";
		ctx.fillText("T", plotR - 10, y - 2);
	}
	ctx.restore();
	if (trigAlign) {
		const x = xOf(lastTrig.t);
		ctx.strokeStyle = "#f59e0b";
		ctx.lineWidth = 1;
		ctx.beginPath();
		ctx.moveTo(x, plotT);
		ctx.lineTo(x, plotB);
		ctx.stroke();
	}
	ctx.font = "10px monospace";
	digitalCh.forEach((c, i) => {
		const rT = digTop + i * rowH, hi = rT + 3, lo = rT + rowH - 5;
		ctx.strokeStyle = "#0f172a";
		ctx.beginPath();
		ctx.moveTo(plotL, rT);
		ctx.lineTo(plotR, rT);
		ctx.stroke();
		const b = bufs.get(c.id);
		if (b && b.t.length) {
			ctx.strokeStyle = c.color;
			ctx.lineWidth = 1.5;
			ctx.beginPath();
			let started = false, py = 0, havePre = false;
			for (let j = 0; j < b.t.length; j++) {
				if (b.t[j] < tStart - .05) {
					py = b.v[j] >= .5 ? hi : lo;
					havePre = true;
					continue;
				}
				const x = xOf(b.t[j]);
				const y = b.v[j] >= .5 ? hi : lo;
				if (!started) {
					if (havePre) {
						ctx.moveTo(xOf(tStart), py);
						ctx.lineTo(x, py);
						ctx.lineTo(x, y);
					} else ctx.moveTo(x, y);
					started = true;
				} else {
					ctx.lineTo(x, py);
					ctx.lineTo(x, y);
				}
				py = y;
				if (b.t[j] > tEnd) break;
			}
			if (!started && havePre) {
				ctx.moveTo(xOf(tStart), py);
				ctx.lineTo(xOf(tEnd), py);
			}
			ctx.stroke();
		}
		ctx.fillStyle = c.color;
		ctx.fillText(c.label || c.id, 11, rT + 12);
	});
	if (cursors) {
		for (const key of ["a", "b"]) {
			const x = plotL + cursors[key] * plotW;
			ctx.strokeStyle = "#38bdf8";
			ctx.lineWidth = 1;
			ctx.setLineDash([2, 2]);
			ctx.beginPath();
			ctx.moveTo(x, plotT);
			ctx.lineTo(x, plotB);
			ctx.stroke();
			ctx.setLineDash([]);
		}
		const dt = Math.abs(cursors.b - cursors.a) * (tEnd - tStart);
		ctx.fillStyle = "#38bdf8";
		ctx.font = "11px monospace";
		const txt = `Δt ${(dt * 1e3).toFixed(1)}ms${dt > 0 ? `  ${(1 / dt).toFixed(1)}Hz` : ""}`;
		ctx.fillText(txt, 12, 20);
	}
	ctx.fillStyle = "#64748b";
	ctx.font = "10px monospace";
	ctx.fillText(`${s.timebase_ms >= 1e3 ? s.timebase_ms / 1e3 + "s" : s.timebase_ms + "ms"} / screen`, plotR - 90, plotB - 3);
	if (!s.running) {
		ctx.fillStyle = "#f87171";
		ctx.fillText("STOPPED", 12, plotB - 3);
	}
}
//#endregion
export { OscopeView as default };
