// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { createElement, forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/defaultAttributes.js
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
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/createLucideIcon.js
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
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/alert-triangle.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var AlertTriangle = createLucideIcon("AlertTriangle", [
	["path", {
		d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z",
		key: "c3ski4"
	}],
	["path", {
		d: "M12 9v4",
		key: "juzpu7"
	}],
	["path", {
		d: "M12 17h.01",
		key: "p32p05"
	}]
]);
//#endregion
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/chevron-down.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var ChevronDown = createLucideIcon("ChevronDown", [["path", {
	d: "m6 9 6 6 6-6",
	key: "qrunsl"
}]]);
//#endregion
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/chevron-right.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var ChevronRight = createLucideIcon("ChevronRight", [["path", {
	d: "m9 18 6-6-6-6",
	key: "mthhwq"
}]]);
//#endregion
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/play.js
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
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/refresh-cw.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var RefreshCw = createLucideIcon("RefreshCw", [
	["path", {
		d: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8",
		key: "v9h5vc"
	}],
	["path", {
		d: "M21 3v5h-5",
		key: "1q7to0"
	}],
	["path", {
		d: "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16",
		key: "3uifl3"
	}],
	["path", {
		d: "M8 16H3v5",
		key: "1cv678"
	}]
]);
//#endregion
//#region ../botgear/repo/agent/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/square.js
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
//#region ../botgear/repo/agent/1.0.0/ui/View.tsx
var btn = "inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40";
var field = "w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-xs text-slate-200";
var STATUS_TONE = {
	running: "text-sky-300",
	completed: "text-emerald-300",
	partial: "text-amber-300",
	failed_checks: "text-rose-300",
	error: "text-rose-300",
	cancelled: "text-slate-400"
};
function ago(iso) {
	if (!iso) return "";
	const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1e3);
	return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : new Date(iso).toLocaleString();
}
function eventLine(e) {
	const d = e.data || {};
	switch (e.type) {
		case "tool_call": return `tool  ${String(d.tool ?? "")}`;
		case "tool_result": return `  ↳ ${d.is_error ? "error: " : ""}${String(d.content ?? "").slice(0, 160)}`;
		case "command": return `run   ${String(d.command ?? "")}`;
		case "file_change": return `write ${String(d.path ?? "")}`;
		case "message": return `say   ${String(d.text ?? "").slice(0, 200)}`;
		case "usage": return `usage $${Number(d.total_cost_usd ?? 0).toFixed(3)}`;
		case "error": return `ERROR ${String(d.message ?? "")}`;
		case "completed": return "done";
		default: return `${e.type} ${JSON.stringify(d).slice(0, 120)}`;
	}
}
function AgentView({ proxy }) {
	const ws = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const root = `/agent/${proxyId}`;
	const [state, setState] = useState(null);
	const [workflows, setWorkflows] = useState([]);
	const [runs, setRuns] = useState([]);
	const [inputs, setInputs] = useState({});
	const [followed, setFollowed] = useState(null);
	const [events, setEvents] = useState([]);
	const [open, setOpen] = useState({});
	const [error, setError] = useState(null);
	const counter = useRef(0);
	const dispatch = useCallback((action, args = {}) => {
		const replyTo = `/cli/reply/agent-${proxyId}-${++counter.current}-${Date.now()}`;
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => {
				off();
				reject(/* @__PURE__ */ new Error(`${action}: no reply`));
			}, 3e4);
			const off = ws.subscribe(replyTo, (f) => {
				if (f.method !== "message") return;
				clearTimeout(timer);
				off();
				const p = f.payload;
				if (p && typeof p === "object" && "error" in p && p.error) reject(new Error(p.error));
				else resolve(p);
			});
			ws.publish(`${root}/control`, {
				action,
				...args,
				reply_to: replyTo
			});
		});
	}, [
		ws,
		root,
		proxyId
	]);
	useEffect(() => ws.subscribe(`${root}/state`, (f) => {
		if (f.method === "message" && f.payload) setState(f.payload);
	}), [ws, root]);
	const refresh = useCallback(async () => {
		try {
			setWorkflows(await dispatch("list_workflows"));
			setRuns(await dispatch("list_runs", { limit: 20 }));
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		}
	}, [dispatch]);
	useEffect(() => {
		refresh();
	}, [refresh]);
	useEffect(() => {
		refresh();
	}, [(state?.active_runs ?? []).map((r) => r.run_id).join(","), refresh]);
	const target = followed ?? state?.active_runs?.[0]?.run_id ?? null;
	useEffect(() => {
		setEvents([]);
		if (!target) return;
		return ws.subscribe(`${root}/runs/${target}/events`, (f) => {
			if (f.method === "message" && f.payload) setEvents((prev) => [...prev.slice(-199), f.payload]);
		});
	}, [
		ws,
		root,
		target
	]);
	const start = async (wf) => {
		setError(null);
		const given = Object.fromEntries(Object.entries(inputs[wf.name] ?? {}).filter(([, v]) => v !== ""));
		try {
			const r = await dispatch("start_run", {
				workflow: wf.name,
				inputs: given
			});
			setFollowed(r.run_id);
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		}
	};
	const toggleRun = async (runId) => {
		if (open[runId] !== void 0) {
			setOpen(({ [runId]: _, ...rest }) => rest);
			return;
		}
		setOpen((o) => ({
			...o,
			[runId]: null
		}));
		try {
			const r = await dispatch("get_run", { run_id: runId });
			setOpen((o) => ({
				...o,
				[runId]: r
			}));
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		}
	};
	const finished = useMemo(() => runs.filter((r) => r.status !== "running"), [runs]);
	const cloudBlocked = state && state.residency !== "cloud_ok" && state.default_agent === "claude_code";
	return /* @__PURE__ */ jsxs("div", {
		className: "flex h-full flex-col gap-3 overflow-auto p-3 text-xs text-slate-200",
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex flex-wrap items-center gap-x-4 gap-y-1 rounded border border-slate-800 bg-slate-900/60 px-2 py-1.5",
				children: [
					/* @__PURE__ */ jsxs("span", { children: ["agent ", /* @__PURE__ */ jsx("span", {
						className: "font-mono text-slate-300",
						children: state?.default_agent ?? "…"
					})] }),
					/* @__PURE__ */ jsxs("span", { children: ["residency ", /* @__PURE__ */ jsx("span", {
						className: "font-mono text-slate-300",
						children: state?.residency ?? "…"
					})] }),
					/* @__PURE__ */ jsxs("span", { children: ["MCP key ", /* @__PURE__ */ jsx("span", {
						className: state?.mcp_key_set ? "text-emerald-300" : "text-amber-300",
						children: state?.mcp_key_set ? "set" : "not set"
					})] }),
					/* @__PURE__ */ jsx("span", {
						className: "truncate text-slate-500",
						title: state?.mcp_endpoint,
						children: state?.mcp_endpoint
					}),
					state?.announce_topic && /* @__PURE__ */ jsxs("span", {
						className: "text-slate-500",
						children: ["announces on ", state.announce_topic]
					}),
					/* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: () => void refresh(),
						className: `${btn} ml-auto text-slate-400 hover:text-slate-200`,
						title: "Refresh",
						children: /* @__PURE__ */ jsx(RefreshCw, { size: 12 })
					})
				]
			}),
			(cloudBlocked || state && !state.mcp_key_set || state?.halted) && /* @__PURE__ */ jsxs("div", {
				className: "flex items-start gap-1.5 rounded border border-amber-800 bg-amber-950/40 px-2 py-1.5 text-amber-200",
				children: [/* @__PURE__ */ jsx(AlertTriangle, {
					size: 13,
					className: "mt-0.5 shrink-0"
				}), /* @__PURE__ */ jsxs("span", { children: [
					state?.halted && "Runtime is HALTED — runs are refused until you resume. ",
					cloudBlocked && "Claude Code sends the robot's state to the model: set residency to cloud_ok in this service's config to allow it. ",
					state && !state.mcp_key_set && "Set mcp_api_key (an API client's secret) so runs can reach the robot."
				] })]
			}),
			error && /* @__PURE__ */ jsx("div", {
				className: "rounded border border-rose-800 bg-rose-950/40 px-2 py-1 text-rose-200",
				children: error
			}),
			/* @__PURE__ */ jsxs("section", {
				className: "space-y-1.5",
				children: [
					/* @__PURE__ */ jsx("h3", {
						className: "text-[11px] font-semibold uppercase tracking-wider text-slate-500",
						children: "Workflows"
					}),
					workflows.map((wf) => {
						const manual = Object.entries(wf.inputs ?? {}).filter(([, s]) => !s.auto);
						return /* @__PURE__ */ jsxs("div", {
							className: "rounded border border-slate-800 bg-slate-950/40 p-2",
							children: [/* @__PURE__ */ jsxs("div", {
								className: "flex items-start gap-2",
								children: [/* @__PURE__ */ jsxs("div", {
									className: "min-w-0 flex-1",
									children: [
										/* @__PURE__ */ jsx("div", {
											className: "font-mono text-slate-100",
											children: wf.name
										}),
										/* @__PURE__ */ jsx("div", {
											className: "text-slate-400",
											children: wf.error ?? wf.description
										}),
										wf.mcp && Object.keys(wf.mcp).length > 0 && /* @__PURE__ */ jsxs("div", {
											className: "mt-0.5 text-[10px] text-slate-500",
											children: ["live tools: ", Object.entries(wf.mcp).map(([s, t]) => `${s}: ${t.join(", ")}`).join(" · ")]
										})
									]
								}), /* @__PURE__ */ jsxs("button", {
									type: "button",
									disabled: !!wf.error || !!state?.halted,
									onClick: () => void start(wf),
									className: `${btn} shrink-0 bg-emerald-600 font-medium text-white hover:bg-emerald-500`,
									children: [/* @__PURE__ */ jsx(Play, { size: 11 }), " Run"]
								})]
							}), manual.length > 0 && /* @__PURE__ */ jsx("div", {
								className: "mt-1.5 grid gap-1 sm:grid-cols-2",
								children: manual.map(([name, spec]) => /* @__PURE__ */ jsxs("label", {
									className: "space-y-0.5",
									children: [/* @__PURE__ */ jsxs("span", {
										className: "text-[10px] text-slate-400",
										children: [name, spec.required ? " *" : ""]
									}), /* @__PURE__ */ jsx("input", {
										className: field,
										value: inputs[wf.name]?.[name] ?? "",
										placeholder: spec.description,
										onChange: (e) => setInputs((p) => ({
											...p,
											[wf.name]: {
												...p[wf.name] ?? {},
												[name]: e.target.value
											}
										}))
									})]
								}, name))
							})]
						}, wf.name);
					}),
					workflows.length === 0 && /* @__PURE__ */ jsx("div", {
						className: "text-slate-500",
						children: "no workflows"
					})
				]
			}),
			(state?.active_runs ?? []).length > 0 && /* @__PURE__ */ jsxs("section", {
				className: "space-y-1.5",
				children: [/* @__PURE__ */ jsx("h3", {
					className: "text-[11px] font-semibold uppercase tracking-wider text-slate-500",
					children: "Running"
				}), state.active_runs.map((r) => /* @__PURE__ */ jsxs("div", {
					onClick: () => setFollowed(r.run_id),
					className: `cursor-pointer rounded border p-2 ${target === r.run_id ? "border-sky-700" : "border-slate-800"} bg-slate-950/40`,
					children: [/* @__PURE__ */ jsxs("div", {
						className: "flex items-center gap-2",
						children: [
							/* @__PURE__ */ jsx("span", { className: "inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" }),
							/* @__PURE__ */ jsx("span", {
								className: "font-mono",
								children: r.workflow
							}),
							/* @__PURE__ */ jsx("span", {
								className: "font-mono text-[10px] text-slate-500",
								children: r.run_id
							}),
							/* @__PURE__ */ jsxs("span", {
								className: "text-slate-400",
								children: [r.phase, r.phase_detail ? ` · ${r.phase_detail}` : ""]
							}),
							/* @__PURE__ */ jsxs("span", {
								className: "ml-auto text-slate-500",
								children: [
									r.tool_calls,
									" tool calls · started ",
									ago(r.started_at)
								]
							}),
							/* @__PURE__ */ jsxs("button", {
								type: "button",
								onClick: (e) => {
									e.stopPropagation();
									dispatch("cancel_run", { run_id: r.run_id });
								},
								className: `${btn} border border-rose-900 text-rose-300 hover:bg-rose-950/50`,
								children: [/* @__PURE__ */ jsx(Square, { size: 11 }), " Cancel"]
							})
						]
					}), r.last_message && /* @__PURE__ */ jsx("div", {
						className: "mt-1 truncate text-slate-400",
						children: r.last_message
					})]
				}, r.run_id))]
			}),
			target && /* @__PURE__ */ jsxs("section", {
				className: "space-y-1",
				children: [/* @__PURE__ */ jsxs("h3", {
					className: "text-[11px] font-semibold uppercase tracking-wider text-slate-500",
					children: ["Live events · ", target]
				}), /* @__PURE__ */ jsx("pre", {
					className: "max-h-48 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] text-slate-300",
					children: events.length ? events.map(eventLine).join("\n") : "waiting for events…"
				})]
			}),
			/* @__PURE__ */ jsxs("section", {
				className: "space-y-1",
				children: [
					/* @__PURE__ */ jsx("h3", {
						className: "text-[11px] font-semibold uppercase tracking-wider text-slate-500",
						children: "Recent runs"
					}),
					finished.map((r) => {
						const detail = open[r.run_id];
						return /* @__PURE__ */ jsxs("div", {
							className: "rounded border border-slate-800 bg-slate-950/40",
							children: [/* @__PURE__ */ jsxs("button", {
								type: "button",
								onClick: () => void toggleRun(r.run_id),
								className: "flex w-full items-center gap-2 px-2 py-1 text-left",
								children: [
									detail !== void 0 ? /* @__PURE__ */ jsx(ChevronDown, { size: 12 }) : /* @__PURE__ */ jsx(ChevronRight, { size: 12 }),
									/* @__PURE__ */ jsx("span", {
										className: "font-mono",
										children: r.workflow
									}),
									/* @__PURE__ */ jsx("span", {
										className: STATUS_TONE[r.status] ?? "text-slate-300",
										children: r.status
									}),
									r.checks_total ? /* @__PURE__ */ jsxs("span", {
										className: "text-slate-500",
										children: [
											"checks ",
											r.checks_passed,
											"/",
											r.checks_total
										]
									}) : null,
									r.cost_usd != null && /* @__PURE__ */ jsxs("span", {
										className: "text-slate-500",
										children: ["$", r.cost_usd.toFixed(3)]
									}),
									/* @__PURE__ */ jsx("span", {
										className: "ml-auto text-slate-500",
										children: ago(r.ended_at)
									})
								]
							}), detail !== void 0 && /* @__PURE__ */ jsx("div", {
								className: "space-y-1 border-t border-slate-800 px-2 py-1.5",
								children: detail === null ? /* @__PURE__ */ jsx("div", {
									className: "text-slate-500",
									children: "loading…"
								}) : /* @__PURE__ */ jsxs(Fragment, { children: [
									detail.error && /* @__PURE__ */ jsx("div", {
										className: "whitespace-pre-wrap text-rose-300",
										children: detail.error
									}),
									Object.entries(detail.deliverables ?? {}).map(([path, text]) => /* @__PURE__ */ jsxs("div", { children: [/* @__PURE__ */ jsx("div", {
										className: "font-mono text-[10px] text-slate-500",
										children: path
									}), /* @__PURE__ */ jsx("pre", {
										className: "max-h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 text-[11px] text-slate-200",
										children: text
									})] }, path)),
									/* @__PURE__ */ jsxs("div", {
										className: "font-mono text-[10px] text-slate-500",
										children: [
											r.run_id,
											" · ",
											detail.agent,
											" · ",
											detail.duration_s,
											"s"
										]
									})
								] })
							})]
						}, r.run_id);
					}),
					finished.length === 0 && /* @__PURE__ */ jsx("div", {
						className: "text-slate-500",
						children: "no runs yet"
					})
				]
			})
		]
	});
}
//#endregion
export { AgentView as default };
