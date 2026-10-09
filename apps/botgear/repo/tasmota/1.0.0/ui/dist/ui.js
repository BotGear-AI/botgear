// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useState } from "react";
import { useServiceRequest, useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/tasmota/1.0.0/ui/View.tsx
function TasmotaView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/tasmota/${proxyId}/state`;
	const controlTopic = `/tasmota/${proxyId}/control`;
	const [state, setState] = useState({});
	const [candidates, setCandidates] = useState(null);
	const [manualHost, setManualHost] = useState("");
	const [manualUser, setManualUser] = useState("");
	const [manualPass, setManualPass] = useState("");
	const [showManual, setShowManual] = useState(false);
	const scan = useServiceRequest(controlTopic, {
		replyPrefix: "tasmota-scan",
		timeoutMs: 15e3
	});
	const connect = useServiceRequest(controlTopic, {
		replyPrefix: "tasmota-connect",
		timeoutMs: 8e3
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
	const onScan = useCallback(async () => {
		setCandidates(null);
		const reply = await scan.request("scan");
		setCandidates(reply?.candidates ?? []);
	}, [scan]);
	const onConnectHost = useCallback(async (host, username, password) => {
		await connect.request("connect", {
			host,
			username: username || void 0,
			password: password || void 0
		});
	}, [connect]);
	const onManualSubmit = useCallback((e) => {
		e?.preventDefault();
		const host = manualHost.trim();
		if (!host) return;
		onConnectHost(host, manualUser.trim(), manualPass);
	}, [
		manualHost,
		manualUser,
		manualPass,
		onConnectHost
	]);
	const onReconfigure = useCallback(() => {
		sendAction("disconnect");
		setCandidates(null);
		setShowManual(false);
	}, [sendAction]);
	const relays = state.relays ?? [];
	return /* @__PURE__ */ jsx("div", {
		className: "flex flex-col gap-3 p-3 text-sm text-slate-200",
		children: !Boolean(state.host) ? /* @__PURE__ */ jsxs(Fragment, { children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsx("h3", {
					className: "text-[10px] uppercase tracking-wider text-slate-400",
					children: "Discover Tasmota devices"
				}), /* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: () => void onScan(),
					disabled: scan.inFlight,
					className: "rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-sky-600 hover:text-sky-300 disabled:opacity-40",
					children: scan.inFlight ? "Scanning…" : "Scan network ↻"
				})]
			}),
			scan.error && /* @__PURE__ */ jsx("div", {
				className: "text-[11px] text-rose-300",
				children: scan.error
			}),
			candidates !== null && candidates.length === 0 && !scan.error && /* @__PURE__ */ jsx("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[11px] text-slate-500",
				children: "No unauthenticated Tasmota devices found on this subnet. Password-protected devices won't show up here — add them manually below."
			}),
			candidates && candidates.length > 0 && /* @__PURE__ */ jsx("ul", {
				className: "space-y-1",
				children: candidates.map((c) => /* @__PURE__ */ jsxs("li", {
					className: "flex items-center gap-2 rounded border border-slate-800 bg-slate-950/40 px-2 py-1",
					children: [/* @__PURE__ */ jsxs("span", {
						className: "flex-1 truncate",
						children: [/* @__PURE__ */ jsx("span", {
							className: "font-medium",
							children: c.friendly_name || c.host
						}), /* @__PURE__ */ jsx("span", {
							className: "ml-1 font-mono text-[10px] text-slate-500",
							children: c.host
						})]
					}), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: () => void onConnectHost(c.host),
						disabled: connect.inFlight,
						className: "rounded bg-emerald-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:opacity-40",
						children: connect.inFlight ? "…" : "Connect"
					})]
				}, c.host))
			}),
			/* @__PURE__ */ jsx("button", {
				type: "button",
				onClick: () => setShowManual((s) => !s),
				className: "self-start text-[11px] text-slate-400 hover:text-slate-200",
				children: showManual ? "Hide manual entry" : "Enter host manually"
			}),
			showManual && /* @__PURE__ */ jsxs("form", {
				onSubmit: onManualSubmit,
				className: "flex flex-col gap-1.5 rounded border border-slate-800 p-2",
				children: [
					/* @__PURE__ */ jsx("input", {
						value: manualHost,
						onChange: (e) => setManualHost(e.target.value),
						placeholder: "IP or hostname, e.g. 192.168.0.50",
						className: "rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "flex gap-1.5",
						children: [/* @__PURE__ */ jsx("input", {
							value: manualUser,
							onChange: (e) => setManualUser(e.target.value),
							placeholder: "username (if set)",
							className: "w-1/2 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
						}), /* @__PURE__ */ jsx("input", {
							value: manualPass,
							onChange: (e) => setManualPass(e.target.value),
							placeholder: "password (if set)",
							type: "password",
							className: "w-1/2 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
						})]
					}),
					connect.error && /* @__PURE__ */ jsx("div", {
						className: "text-[11px] text-rose-300",
						children: connect.error
					}),
					/* @__PURE__ */ jsx("button", {
						type: "submit",
						disabled: connect.inFlight || !manualHost.trim(),
						className: "self-start rounded bg-sky-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-40",
						children: connect.inFlight ? "Connecting…" : "Connect"
					})
				]
			})
		] }) : /* @__PURE__ */ jsxs(Fragment, { children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsxs("div", {
					className: "flex items-center gap-1.5",
					children: [
						/* @__PURE__ */ jsx("span", {
							className: `h-2 w-2 rounded-full ${state.reachable ? "bg-emerald-500" : "bg-rose-500"}`,
							title: state.reachable ? "reachable" : "unreachable"
						}),
						/* @__PURE__ */ jsx("span", {
							className: "font-medium",
							children: state.friendly_name || state.host
						}),
						/* @__PURE__ */ jsx("span", {
							className: "font-mono text-[10px] text-slate-500",
							children: state.host
						})
					]
				}), /* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: onReconfigure,
					className: "text-[11px] text-slate-400 hover:text-slate-200",
					children: "Reconfigure"
				})]
			}),
			!state.reachable && /* @__PURE__ */ jsx("div", {
				className: "text-[11px] text-rose-300",
				children: "Not responding — will keep retrying every poll."
			}),
			/* @__PURE__ */ jsxs("ul", {
				className: "space-y-1",
				children: [relays.length === 0 && /* @__PURE__ */ jsx("li", {
					className: "text-[11px] text-slate-500",
					children: "No relay state yet — waiting on the first poll."
				}), relays.map((r) => /* @__PURE__ */ jsxs("li", {
					className: "flex items-center justify-between rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5",
					children: [/* @__PURE__ */ jsx("span", { children: r.label || `Relay ${r.index}` }), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: () => sendAction("power_set", {
							relay: r.index,
							on: !r.on
						}),
						className: `w-14 rounded px-2 py-0.5 text-[11px] font-medium text-white ${r.on ? "bg-emerald-600 hover:bg-emerald-500" : "bg-slate-700 hover:bg-slate-600"}`,
						children: r.on ? "ON" : "OFF"
					})]
				}, r.index))]
			})
		] })
	});
}
//#endregion
export { TasmotaView as default };
