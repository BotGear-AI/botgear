// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useMemo, useState } from "react";
import { useServiceRequest, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/network_scanner/1.0.0/ui/View.tsx
function timeAgo(ts) {
	if (!ts) return "never";
	const s = Math.max(0, Date.now() / 1e3 - ts);
	if (s < 60) return `${Math.round(s)}s ago`;
	if (s < 3600) return `${Math.round(s / 60)}m ago`;
	if (s < 86400) return `${Math.round(s / 3600)}h ago`;
	return `${Math.round(s / 86400)}d ago`;
}
function NetworkScannerView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/network_scanner/${proxyId}/state`;
	const controlTopic = `/network_scanner/${proxyId}/control`;
	const [state, setState] = useState({});
	const [showSettings, setShowSettings] = useState(false);
	const [showIgnored, setShowIgnored] = useState(false);
	const [editingMac, setEditingMac] = useState(null);
	const [editValue, setEditValue] = useState("");
	const scanNow = useServiceRequest(controlTopic, {
		replyPrefix: "netscan-scan",
		timeoutMs: 3e4
	});
	useEffect(() => {
		if (!proxyId) return;
		return wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (!p || typeof p !== "object") return;
			setState(p);
		});
	}, [
		proxyId,
		stateTopic,
		wsClient
	]);
	const sendAction = useCallback((action, args = {}) => {
		wsClient.publish(controlTopic, {
			action,
			...args
		});
	}, [wsClient, controlTopic]);
	const onScanNow = useCallback(() => {
		scanNow.request("scan_now");
	}, [scanNow]);
	const startEdit = useCallback((d) => {
		setEditingMac(d.mac);
		setEditValue(d.label ?? "");
	}, []);
	const commitEdit = useCallback(() => {
		if (editingMac) sendAction("set_label", {
			mac: editingMac,
			label: editValue.trim() || null
		});
		setEditingMac(null);
		setEditValue("");
	}, [
		editingMac,
		editValue,
		sendAction
	]);
	const devices = state.devices ?? [];
	const visible = useMemo(() => devices.filter((d) => showIgnored || !d.ignored), [devices, showIgnored]);
	return /* @__PURE__ */ jsxs("div", {
		className: "flex flex-col gap-3 p-3 text-sm text-slate-200",
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsxs("div", {
					className: "flex items-center gap-2",
					children: [/* @__PURE__ */ jsxs("span", {
						className: "text-[11px] text-slate-400",
						children: [
							/* @__PURE__ */ jsx("span", {
								className: "font-medium text-emerald-400",
								children: state.online ?? 0
							}),
							" online",
							" · ",
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-300",
								children: state.total ?? 0
							}),
							" known"
						]
					}), /* @__PURE__ */ jsxs("span", {
						className: "font-mono text-[10px] text-slate-500",
						children: ["last scan ", timeAgo(state.last_scan)]
					})]
				}), /* @__PURE__ */ jsxs("div", {
					className: "flex items-center gap-2",
					children: [/* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: () => setShowSettings((s) => !s),
						className: "text-[11px] text-slate-400 hover:text-slate-200",
						children: showSettings ? "Hide settings" : "Settings"
					}), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: onScanNow,
						disabled: scanNow.inFlight,
						className: "rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-sky-600 hover:text-sky-300 disabled:opacity-40",
						children: scanNow.inFlight ? "Scanning…" : "Scan now ↻"
					})]
				})]
			}),
			scanNow.error && /* @__PURE__ */ jsx("div", {
				className: "text-[11px] text-rose-300",
				children: scanNow.error
			}),
			showSettings && /* @__PURE__ */ jsx(SettingsPanel, {
				state,
				sendAction
			}),
			state.subnets && state.subnets.length > 0 && /* @__PURE__ */ jsx("div", {
				className: "flex flex-wrap gap-1",
				children: state.subnets.map((s) => /* @__PURE__ */ jsx("span", {
					className: "rounded bg-slate-800/70 px-1.5 py-0.5 font-mono text-[10px] text-slate-400",
					children: s
				}, s))
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsx("h3", {
					className: "text-[10px] uppercase tracking-wider text-slate-400",
					children: "Devices"
				}), /* @__PURE__ */ jsxs("label", {
					className: "flex items-center gap-1 text-[10px] text-slate-500",
					children: [/* @__PURE__ */ jsx("input", {
						type: "checkbox",
						checked: showIgnored,
						onChange: (e) => setShowIgnored(e.target.checked)
					}), "show ignored"]
				})]
			}),
			/* @__PURE__ */ jsxs("ul", {
				className: "space-y-1",
				children: [visible.length === 0 && /* @__PURE__ */ jsx("li", {
					className: "rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[11px] text-slate-500",
					children: "No devices yet — run a scan to build the roster."
				}), visible.map((d) => /* @__PURE__ */ jsxs("li", {
					className: `rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5 ${d.ignored ? "opacity-50" : ""}`,
					children: [/* @__PURE__ */ jsxs("div", {
						className: "flex items-center gap-2",
						children: [
							/* @__PURE__ */ jsx("span", {
								className: `h-2 w-2 shrink-0 rounded-full ${d.online ? "bg-emerald-500" : "bg-rose-500"}`,
								title: d.online ? "online" : `offline since ${timeAgo(d.offline_since)}`
							}),
							editingMac === d.mac ? /* @__PURE__ */ jsx("input", {
								autoFocus: true,
								value: editValue,
								onChange: (e) => setEditValue(e.target.value),
								onBlur: commitEdit,
								onKeyDown: (e) => {
									if (e.key === "Enter") commitEdit();
									if (e.key === "Escape") {
										setEditingMac(null);
										setEditValue("");
									}
								},
								placeholder: "label this device…",
								className: "flex-1 rounded border border-sky-700 bg-slate-950 px-1.5 py-0.5 text-[11px]"
							}) : /* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => startEdit(d),
								className: "flex-1 truncate text-left font-medium hover:text-sky-300",
								title: "click to label",
								children: d.display_name
							}),
							/* @__PURE__ */ jsx("span", {
								className: "font-mono text-[10px] text-slate-500",
								children: d.last_ip ?? "—"
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => sendAction("remove_device", { mac: d.mac }),
								className: "text-[10px] text-slate-500 hover:text-rose-300",
								title: "forget this device",
								children: "✕"
							})
						]
					}), /* @__PURE__ */ jsxs("div", {
						className: "mt-0.5 flex flex-wrap items-center gap-1 pl-4 text-[10px] text-slate-500",
						children: [
							/* @__PURE__ */ jsx("span", {
								className: "font-mono",
								children: d.mac
							}),
							d.vendor && /* @__PURE__ */ jsx("span", {
								className: "rounded bg-slate-800/70 px-1 py-0.5",
								children: d.vendor
							}),
							d.hostname && /* @__PURE__ */ jsx("span", {
								className: "rounded bg-slate-800/70 px-1 py-0.5",
								children: d.hostname
							}),
							d.mdns_name && d.mdns_name !== d.hostname && /* @__PURE__ */ jsx("span", {
								className: "rounded bg-slate-800/70 px-1 py-0.5",
								children: d.mdns_name
							}),
							d.interface && /* @__PURE__ */ jsx("span", { children: d.interface }),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => sendAction("set_label", {
									mac: d.mac,
									ignored: !d.ignored
								}),
								className: "ml-auto hover:text-slate-300",
								children: d.ignored ? "unignore" : "ignore"
							})
						]
					})]
				}, d.mac))]
			})
		]
	});
}
function SettingsPanel({ state, sendAction }) {
	const [interval, setIntervalVal] = useState(String(state.scan_interval_s ?? 60));
	const [alertTarget, setAlertTarget] = useState(state.alert_target ?? "");
	const [subnets, setSubnets] = useState("");
	const alertOn = state.alert_on ?? [
		"new",
		"offline",
		"online"
	];
	useEffect(() => setIntervalVal(String(state.scan_interval_s ?? 60)), [state.scan_interval_s]);
	useEffect(() => setAlertTarget(state.alert_target ?? ""), [state.alert_target]);
	const toggleAlertEvent = useCallback((event) => {
		sendAction("set_alert_on", { events: alertOn.includes(event) ? alertOn.filter((e) => e !== event) : [...alertOn, event] });
	}, [alertOn, sendAction]);
	return /* @__PURE__ */ jsxs("div", {
		className: "flex flex-col gap-2 rounded border border-slate-800 p-2 text-[11px]",
		children: [
			/* @__PURE__ */ jsxs("label", {
				className: "flex items-center gap-2",
				children: [/* @__PURE__ */ jsx("span", {
					className: "w-32 text-slate-400",
					children: "Scan interval (s)"
				}), /* @__PURE__ */ jsx("input", {
					value: interval,
					onChange: (e) => setIntervalVal(e.target.value),
					onBlur: () => sendAction("set_scan_interval", { seconds: Number(interval) || 60 }),
					className: "w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5"
				})]
			}),
			/* @__PURE__ */ jsxs("label", {
				className: "flex items-center gap-2",
				children: [/* @__PURE__ */ jsx("span", {
					className: "w-32 shrink-0 text-slate-400",
					children: "Alert target topic"
				}), /* @__PURE__ */ jsx("input", {
					value: alertTarget,
					onChange: (e) => setAlertTarget(e.target.value),
					onBlur: () => sendAction("set_alert_target", { topic: alertTarget.trim() || null }),
					placeholder: "/chat/chat-1/control",
					className: "flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono"
				})]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center gap-3",
				children: [/* @__PURE__ */ jsx("span", {
					className: "w-32 shrink-0 text-slate-400",
					children: "Alert on"
				}), [
					"new",
					"offline",
					"online"
				].map((event) => /* @__PURE__ */ jsxs("label", {
					className: "flex items-center gap-1",
					children: [/* @__PURE__ */ jsx("input", {
						type: "checkbox",
						checked: alertOn.includes(event),
						onChange: () => toggleAlertEvent(event)
					}), event]
				}, event))]
			}),
			/* @__PURE__ */ jsxs("label", {
				className: "flex items-start gap-2",
				children: [/* @__PURE__ */ jsx("span", {
					className: "w-32 shrink-0 text-slate-400",
					children: "Subnet overrides"
				}), /* @__PURE__ */ jsx("textarea", {
					value: subnets,
					onChange: (e) => setSubnets(e.target.value),
					onBlur: () => {
						const list = subnets.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
						if (list.length > 0) sendAction("set_subnets", { subnets: list });
					},
					placeholder: "192.168.1.0/24 (blank = auto-detect all local subnets)",
					rows: 2,
					className: "flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono"
				})]
			})
		]
	});
}
//#endregion
export { NetworkScannerView as default };
