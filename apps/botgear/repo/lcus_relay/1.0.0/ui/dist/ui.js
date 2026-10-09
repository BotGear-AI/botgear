// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { createElement, forwardRef, useCallback, useEffect, useRef, useState } from "react";
import { useServiceRequest, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/lcus_relay/1.0.0/ui/node_modules/lucide-react/dist/esm/defaultAttributes.js
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
//#region ../botgear/repo/lcus_relay/1.0.0/ui/node_modules/lucide-react/dist/esm/createLucideIcon.js
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
//#region ../botgear/repo/lcus_relay/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/loader-2.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Loader2 = createLucideIcon("Loader2", [["path", {
	d: "M21 12a9 9 0 1 1-6.219-8.56",
	key: "13zald"
}]]);
//#endregion
//#region ../botgear/repo/lcus_relay/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/power.js
/**
* @license lucide-react v0.344.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Power = createLucideIcon("Power", [["path", {
	d: "M12 2v10",
	key: "mnfbl"
}], ["path", {
	d: "M18.4 6.6a9 9 0 1 1-12.77.04",
	key: "obofu9"
}]]);
//#endregion
//#region ../botgear/repo/lcus_relay/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/refresh-cw.js
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
//#region ../botgear/repo/lcus_relay/1.0.0/ui/View.tsx
var BAUD_OPTIONS = [
	2400,
	4800,
	9600,
	19200,
	38400,
	57600,
	115200
];
var CHANNEL_COUNT_OPTIONS = [
	1,
	2,
	4,
	8
];
function LcusRelayFullView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/lcus_relay/${proxyId}/state`;
	const controlTopic = `/lcus_relay/${proxyId}/control`;
	const [state, setState] = useState({});
	const [portDraft, setPortDraft] = useState("");
	const [baudDraft, setBaudDraft] = useState(9600);
	const [channelCountDraft, setChannelCountDraft] = useState(8);
	useEffect(() => {
		if (!proxyId) return;
		return wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			setState(f.payload);
		});
	}, [
		proxyId,
		stateTopic,
		wsClient
	]);
	useEffect(() => {
		if (state.connected) return;
		if (state.last_port && !portDraft) setPortDraft(state.last_port);
		if (state.last_baud) setBaudDraft(state.last_baud);
		if (typeof state.channel_count === "number") setChannelCountDraft(state.channel_count);
	}, [
		state.last_port,
		state.last_baud,
		state.channel_count,
		state.connected
	]);
	const sendAction = useCallback((payload) => {
		wsClient.publish(controlTopic, payload);
	}, [controlTopic, wsClient]);
	const connectRequest = useServiceRequest(controlTopic, {
		timeoutMs: 1e4,
		errorField: "last_error",
		replyPrefix: `lcus_relay-${proxyId}-connect`
	});
	const disconnectRequest = useServiceRequest(controlTopic, {
		timeoutMs: 5e3,
		errorField: "last_error",
		replyPrefix: `lcus_relay-${proxyId}-disconnect`
	});
	const queryRequest = useServiceRequest(controlTopic, {
		timeoutMs: 3e3,
		errorField: "last_error",
		replyPrefix: `lcus_relay-${proxyId}-query`
	});
	const onConnect = useCallback((e) => {
		e?.preventDefault();
		if (!portDraft || connectRequest.inFlight) return;
		connectRequest.request("connect", {
			port: portDraft,
			baudrate: baudDraft,
			channel_count: channelCountDraft
		});
	}, [
		portDraft,
		baudDraft,
		channelCountDraft,
		connectRequest
	]);
	const onDisconnect = useCallback(() => {
		if (disconnectRequest.inFlight) return;
		disconnectRequest.request("disconnect");
	}, [disconnectRequest]);
	const onRefreshPorts = useCallback(() => sendAction({ action: "list_ports" }), [sendAction]);
	const onToggleAutoConnect = useCallback((enabled) => sendAction({
		action: "set_auto_connect",
		enabled
	}), [sendAction]);
	const onQueryStatus = useCallback(() => {
		if (queryRequest.inFlight) return;
		queryRequest.request("query_status");
	}, [queryRequest]);
	const wasConnectedRef = useRef(false);
	useEffect(() => {
		if (state.connected && !wasConnectedRef.current) {
			connectRequest.clearError();
			disconnectRequest.clearError();
		}
		wasConnectedRef.current = !!state.connected;
	}, [state.connected]);
	const connected = !!state.connected;
	const retrying = !!state.retrying;
	const channels = state.channels ?? CHANNEL_COUNT_OPTIONS_FALLBACK(state.channel_count);
	const relays = state.relays ?? {};
	const error = connectRequest.error ?? disconnectRequest.error ?? queryRequest.error ?? state.last_error;
	const ports = state.ports ?? [];
	const portDevices = new Set(ports.map((p) => p.device));
	return /* @__PURE__ */ jsxs("div", {
		className: "flex min-w-[420px] flex-col gap-3 p-3 text-xs",
		onPointerDown: (e) => e.stopPropagation(),
		children: [/* @__PURE__ */ jsxs(Section, {
			title: "Connection",
			children: [/* @__PURE__ */ jsxs("form", {
				onSubmit: onConnect,
				className: "flex flex-wrap items-end gap-2",
				children: [
					/* @__PURE__ */ jsxs("label", {
						className: "flex flex-col",
						children: [/* @__PURE__ */ jsx("span", {
							className: "mb-0.5 text-[10px] uppercase tracking-wider text-slate-500",
							children: "port"
						}), /* @__PURE__ */ jsxs("div", {
							className: "flex items-center gap-1",
							children: [
								/* @__PURE__ */ jsx("input", {
									type: "text",
									value: portDraft,
									onChange: (e) => setPortDraft(e.target.value),
									disabled: connected,
									placeholder: "/dev/ttyUSB0",
									onPointerDown: (e) => e.stopPropagation(),
									className: "nodrag nopan w-40 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none disabled:opacity-50"
								}),
								/* @__PURE__ */ jsxs("select", {
									value: portDevices.has(portDraft) ? portDraft : "",
									onChange: (e) => {
										if (e.target.value) setPortDraft(e.target.value);
									},
									disabled: connected,
									title: "Detected ports — picking one fills the text field",
									onPointerDown: (e) => e.stopPropagation(),
									className: "nodrag nopan w-8 rounded border border-slate-700 bg-slate-950 px-1 py-1 text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none disabled:opacity-50",
									children: [/* @__PURE__ */ jsx("option", {
										value: "",
										children: "▾"
									}), ports.map((p) => {
										const ownedByOther = (p.holders ?? []).filter((h) => h.proxy_id !== proxyId).length > 0;
										return /* @__PURE__ */ jsxs("option", {
											value: p.device,
											disabled: ownedByOther,
											children: [
												p.device,
												p.description ? `  — ${p.description}` : "",
												ownedByOther ? "  (in use)" : ""
											]
										}, p.device);
									})]
								}),
								/* @__PURE__ */ jsx("button", {
									type: "button",
									onClick: onRefreshPorts,
									onPointerDown: (e) => e.stopPropagation(),
									title: "Refresh port list",
									className: "nodrag nopan rounded border border-slate-700 p-1 hover:border-slate-500",
									children: /* @__PURE__ */ jsx(RefreshCw, { className: "h-3 w-3" })
								})
							]
						})]
					}),
					/* @__PURE__ */ jsxs("label", {
						className: "flex flex-col",
						children: [/* @__PURE__ */ jsx("span", {
							className: "mb-0.5 text-[10px] uppercase tracking-wider text-slate-500",
							children: "baud"
						}), /* @__PURE__ */ jsx("select", {
							value: baudDraft,
							onChange: (e) => setBaudDraft(Number(e.target.value)),
							disabled: connected,
							onPointerDown: (e) => e.stopPropagation(),
							className: "nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-1 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50",
							children: BAUD_OPTIONS.map((b) => /* @__PURE__ */ jsx("option", {
								value: b,
								children: b
							}, b))
						})]
					}),
					/* @__PURE__ */ jsxs("label", {
						className: "flex flex-col",
						children: [/* @__PURE__ */ jsx("span", {
							className: "mb-0.5 text-[10px] uppercase tracking-wider text-slate-500",
							children: "module"
						}), /* @__PURE__ */ jsx("select", {
							value: channelCountDraft,
							onChange: (e) => {
								const n = Number(e.target.value);
								setChannelCountDraft(n);
								if (connected) sendAction({
									action: "set_channel_count",
									channel_count: n
								});
							},
							onPointerDown: (e) => e.stopPropagation(),
							className: "nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-1 py-1 font-mono text-[11px] text-slate-100",
							children: CHANNEL_COUNT_OPTIONS.map((n) => /* @__PURE__ */ jsxs("option", {
								value: n,
								children: ["LCUS-", n]
							}, n))
						})]
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "ml-auto flex items-center gap-2",
						children: [
							/* @__PURE__ */ jsxs("label", {
								className: "flex items-center gap-1.5 font-mono text-[10px] text-slate-400",
								onPointerDown: (e) => e.stopPropagation(),
								children: [/* @__PURE__ */ jsx("input", {
									type: "checkbox",
									checked: !!state.autoreconnect,
									onChange: (e) => onToggleAutoConnect(e.target.checked),
									className: "nodrag nopan accent-emerald-500"
								}), "auto-connect"]
							}),
							/* @__PURE__ */ jsxs("span", {
								className: "flex items-center gap-1.5 font-mono text-[10px]",
								children: [/* @__PURE__ */ jsx("span", { className: `inline-block h-2.5 w-2.5 rounded-full ${connected ? "bg-emerald-400 shadow-[0_0_6px_1px] shadow-emerald-500/60" : retrying ? "animate-pulse bg-amber-400" : "bg-slate-600"}` }), /* @__PURE__ */ jsx("span", {
									className: connected ? "text-emerald-300" : retrying ? "text-amber-300" : "text-slate-500",
									children: connected ? "connected" : retrying ? "reconnecting…" : "offline"
								})]
							}),
							!connected ? /* @__PURE__ */ jsxs("button", {
								type: "submit",
								disabled: !portDraft || connectRequest.inFlight,
								"aria-busy": connectRequest.inFlight,
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40",
								children: [connectRequest.inFlight && /* @__PURE__ */ jsx(Loader2, { className: "h-3 w-3 animate-spin" }), connectRequest.inFlight ? "Connecting…" : "Connect"]
							}) : /* @__PURE__ */ jsxs("button", {
								type: "button",
								onClick: onDisconnect,
								disabled: disconnectRequest.inFlight,
								"aria-busy": disconnectRequest.inFlight,
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan inline-flex items-center gap-1.5 rounded border border-slate-700 px-3 py-1 text-[11px] hover:border-rose-500 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-40",
								children: [disconnectRequest.inFlight && /* @__PURE__ */ jsx(Loader2, { className: "h-3 w-3 animate-spin" }), disconnectRequest.inFlight ? "Disconnecting…" : "Disconnect"]
							})
						]
					})
				]
			}), error && /* @__PURE__ */ jsxs("div", {
				className: "mt-2 truncate font-mono text-[10px] text-rose-300",
				title: error,
				children: ["error: ", error]
			})]
		}), /* @__PURE__ */ jsx(Section, {
			title: "Relays",
			children: !connected ? /* @__PURE__ */ jsx("div", {
				className: "rounded border border-slate-800 bg-slate-950/70 p-2 text-slate-500",
				children: "Connect to switch relays."
			}) : /* @__PURE__ */ jsxs("div", {
				className: "flex flex-col gap-2",
				children: [
					/* @__PURE__ */ jsx("div", {
						className: "grid grid-cols-4 gap-2",
						children: channels.map((ch) => /* @__PURE__ */ jsx(RelayButton, {
							channel: ch,
							on: !!relays[String(ch)],
							onToggle: () => sendAction({
								action: "relay_toggle",
								channel: ch
							})
						}, ch))
					}),
					/* @__PURE__ */ jsx(SpacingControl, {
						valueMs: state.command_spacing_ms ?? 30,
						onCommit: (ms) => sendAction({
							action: "set_command_spacing",
							command_spacing_ms: ms
						})
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "flex items-center gap-2",
						children: [
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => sendAction({
									action: "relay_all",
									on: true
								}),
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan rounded bg-emerald-700 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-emerald-600",
								children: "All on"
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => sendAction({
									action: "relay_all",
									on: false
								}),
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan rounded bg-slate-700 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-slate-600",
								children: "All off"
							}),
							/* @__PURE__ */ jsxs("button", {
								type: "button",
								onClick: onQueryStatus,
								disabled: queryRequest.inFlight,
								"aria-busy": queryRequest.inFlight,
								title: "Ask the board for its live relay state (best-effort — not every clone board answers)",
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan ml-auto inline-flex items-center gap-1.5 rounded border border-slate-700 px-2 py-1 text-[11px] hover:border-slate-500 disabled:opacity-40",
								children: [queryRequest.inFlight ? /* @__PURE__ */ jsx(Loader2, { className: "h-3 w-3 animate-spin" }) : /* @__PURE__ */ jsx(RefreshCw, { className: "h-3 w-3" }), "query hardware"]
							})
						]
					})
				]
			})
		})]
	});
}
function CHANNEL_COUNT_OPTIONS_FALLBACK(channelCount) {
	const n = channelCount ?? 8;
	return Array.from({ length: n }, (_, i) => i + 1);
}
function SpacingControl({ valueMs, onCommit }) {
	const [draft, setDraft] = useState(String(valueMs));
	useEffect(() => {
		setDraft(String(valueMs));
	}, [valueMs]);
	return /* @__PURE__ */ jsx("div", {
		className: "flex items-center gap-2 text-[10px] text-slate-500",
		children: /* @__PURE__ */ jsxs("label", {
			className: "flex items-center gap-1.5",
			onPointerDown: (e) => e.stopPropagation(),
			children: [
				/* @__PURE__ */ jsx("span", {
					className: "uppercase tracking-wider",
					children: "spacing"
				}),
				/* @__PURE__ */ jsx("input", {
					type: "number",
					min: 0,
					max: 1e3,
					step: 5,
					value: draft,
					onChange: (e) => setDraft(e.target.value),
					onBlur: () => onCommit(Math.max(0, Math.min(1e3, Number(draft) || 0))),
					onPointerDown: (e) => e.stopPropagation(),
					className: "nodrag nopan w-16 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-slate-100"
				}),
				/* @__PURE__ */ jsx("span", { children: "ms between All on/off commands" })
			]
		})
	});
}
function RelayButton({ channel, on, onToggle }) {
	return /* @__PURE__ */ jsxs("button", {
		type: "button",
		onClick: onToggle,
		onPointerDown: (e) => e.stopPropagation(),
		className: `nodrag nopan flex flex-col items-center gap-1 rounded border px-2 py-2 font-mono text-[11px] transition-colors ${on ? "border-emerald-500 bg-emerald-600/20 text-emerald-300" : "border-slate-700 bg-slate-950/70 text-slate-400 hover:border-slate-500"}`,
		children: [
			/* @__PURE__ */ jsx(Power, { className: `h-4 w-4 ${on ? "text-emerald-400" : "text-slate-600"}` }),
			/* @__PURE__ */ jsxs("span", {
				className: "uppercase tracking-wider",
				children: ["CH ", channel]
			}),
			/* @__PURE__ */ jsx("span", {
				className: on ? "text-emerald-300" : "text-slate-500",
				children: on ? "ON" : "OFF"
			})
		]
	});
}
function Section({ title, children }) {
	return /* @__PURE__ */ jsxs("section", {
		className: "rounded border border-slate-800 bg-slate-900/40 p-3",
		children: [/* @__PURE__ */ jsx("h3", {
			className: "mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400",
			children: title
		}), children]
	});
}
//#endregion
export { LcusRelayFullView as default };
