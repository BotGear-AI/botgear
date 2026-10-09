// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/slack/1.0.0/ui/View.tsx
var MAX_TURNS = 200;
function SlackFullView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const inboxTopic = `/slack/${proxyId}/inbox`;
	const spokenTopic = `/slack/${proxyId}/spoken`;
	const stateTopic = `/slack/${proxyId}/state`;
	const controlTopic = `/slack/${proxyId}/control`;
	const [transcript, setTranscript] = useState([]);
	const [state, setState] = useState(null);
	const [draft, setDraft] = useState("");
	const [channelOverride, setChannelOverride] = useState("");
	const scrollRef = useRef(null);
	const keyCounterRef = useRef(0);
	const pushTurn = useCallback((from, text, ts, sender, channel) => {
		const trimmed = text.trim();
		if (!trimmed) return;
		keyCounterRef.current += 1;
		const key = `${ts}-${keyCounterRef.current}`;
		setTranscript((prev) => {
			const next = [...prev, {
				from,
				text: trimmed,
				ts,
				sender,
				channel,
				key
			}];
			return next.length > MAX_TURNS ? next.slice(next.length - MAX_TURNS) : next;
		});
	}, []);
	useEffect(() => {
		if (!proxyId) return;
		const offInbox = wsClient.subscribe(inboxTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload ?? {};
			if (typeof p.text !== "string") return;
			pushTurn("inbound", p.text, p.ts ?? Date.now() / 1e3, p.sender, p.channel);
		});
		const offSpoken = wsClient.subscribe(spokenTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload ?? {};
			if (typeof p.text !== "string") return;
			pushTurn("bot", p.text, p.ts ?? Date.now() / 1e3, null, p.channel);
		});
		const offState = wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			setState(f.payload);
		});
		return () => {
			offInbox();
			offSpoken();
			offState();
		};
	}, [
		proxyId,
		inboxTopic,
		spokenTopic,
		stateTopic,
		wsClient,
		pushTurn
	]);
	useLayoutEffect(() => {
		const el = scrollRef.current;
		if (!el) return;
		el.scrollTop = el.scrollHeight;
		const id = requestAnimationFrame(() => {
			el.scrollTop = el.scrollHeight;
		});
		return () => cancelAnimationFrame(id);
	}, [transcript.length]);
	const serviceRunning = proxy.status === "running" || proxy.status === "starting";
	const effectiveChannel = channelOverride.trim() || state?.default_channel || void 0;
	const simulateInbound = useCallback((e) => {
		e?.preventDefault();
		const text = draft.trim();
		if (!text || !serviceRunning) return;
		wsClient.publish(inboxTopic, {
			text,
			channel: effectiveChannel ?? null,
			ts: Date.now() / 1e3
		});
		setDraft("");
	}, [
		draft,
		inboxTopic,
		serviceRunning,
		wsClient,
		effectiveChannel
	]);
	const postAsBot = useCallback(() => {
		const text = draft.trim();
		if (!text || !serviceRunning) return;
		wsClient.publish(controlTopic, {
			action: "speak",
			text,
			channel: effectiveChannel
		});
		setDraft("");
	}, [
		draft,
		controlTopic,
		serviceRunning,
		wsClient,
		effectiveChannel
	]);
	const onKeyDown = useCallback((e) => {
		if (e.key === "Enter" && !e.shiftKey) {
			e.preventDefault();
			postAsBot();
		}
	}, [postAsBot]);
	const fmtTime = (ts) => {
		const d = /* @__PURE__ */ new Date(ts * 1e3);
		const pad = (n) => String(n).padStart(2, "0");
		return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
	};
	return /* @__PURE__ */ jsxs("div", {
		className: "flex h-full min-h-[300px] min-w-[320px] flex-col gap-2 p-3 text-xs",
		onPointerDown: (e) => e.stopPropagation(),
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-baseline justify-between text-[10px] text-slate-500",
				children: [/* @__PURE__ */ jsxs("span", { children: ["slack-", proxyId] }), /* @__PURE__ */ jsx("span", {
					className: "flex items-baseline gap-2",
					children: state ? /* @__PURE__ */ jsxs(Fragment, { children: [
						/* @__PURE__ */ jsx("span", {
							className: state.connected ? "text-emerald-400" : "text-rose-400",
							title: "Gateway connection status",
							children: state.connected ? "● connected" : "○ disconnected"
						}),
						/* @__PURE__ */ jsx("span", {
							className: state.listening ? "text-emerald-400" : "text-slate-500",
							children: state.listening ? "listening" : "idle"
						}),
						state.queued > 0 && /* @__PURE__ */ jsxs("span", {
							className: "text-amber-400",
							title: "Messages queued for the next listen() call",
							children: ["queued ", state.queued]
						})
					] }) : /* @__PURE__ */ jsx("span", { children: serviceRunning ? "loading…" : "service not running" })
				})]
			}),
			/* @__PURE__ */ jsx("div", {
				className: "relative min-h-0 flex-1 rounded border border-slate-800 bg-slate-950/60",
				children: /* @__PURE__ */ jsx("div", {
					ref: scrollRef,
					className: "nodrag nopan absolute inset-0 flex flex-col gap-1 overflow-y-auto p-2",
					onWheel: (e) => e.stopPropagation(),
					children: transcript.length === 0 ? /* @__PURE__ */ jsx("div", {
						className: "text-[11px] text-slate-600",
						children: serviceRunning ? "no messages yet — post below, or wait for a Slack message to arrive" : "start the slack service to connect"
					}) : transcript.map((t) => {
						const isInbound = t.from === "inbound";
						return /* @__PURE__ */ jsxs("div", {
							className: `flex flex-col ${isInbound ? "items-end" : "items-start"}`,
							children: [/* @__PURE__ */ jsxs("span", {
								className: "text-[9px] text-slate-600",
								children: [
									isInbound ? t.sender ?? "inbound" : "bot",
									t.channel ? ` · #${t.channel}` : "",
									" · ",
									fmtTime(t.ts)
								]
							}), /* @__PURE__ */ jsx("div", {
								className: `max-w-[85%] whitespace-pre-wrap break-words rounded px-2 py-1 text-[11px] ${isInbound ? "bg-sky-950/60 text-sky-100" : "bg-purple-950/60 text-purple-100"}`,
								children: t.text
							})]
						}, t.key);
					})
				})
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center gap-2 text-[10px] text-slate-500",
				children: [/* @__PURE__ */ jsx("span", { children: "channel" }), /* @__PURE__ */ jsx("input", {
					value: channelOverride,
					onChange: (e) => setChannelOverride(e.target.value),
					onClick: (e) => e.stopPropagation(),
					placeholder: state?.default_channel ?? "default_channel not set",
					className: "nodrag nopan w-40 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 font-mono text-[10px] text-slate-200 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none"
				})]
			}),
			/* @__PURE__ */ jsxs("form", {
				onSubmit: simulateInbound,
				className: "flex items-end gap-2",
				children: [/* @__PURE__ */ jsx("textarea", {
					value: draft,
					onChange: (e) => setDraft(e.target.value),
					onKeyDown,
					onClick: (e) => e.stopPropagation(),
					disabled: !serviceRunning,
					rows: 2,
					placeholder: serviceRunning ? "type here — Enter posts as bot, buttons below for either" : "service not running",
					className: "nodrag nopan min-h-[44px] flex-1 resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1 font-sans text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none disabled:opacity-50"
				}), /* @__PURE__ */ jsxs("div", {
					className: "flex flex-col gap-1",
					children: [/* @__PURE__ */ jsx("button", {
						type: "submit",
						onClick: (e) => e.stopPropagation(),
						onPointerDown: (e) => e.stopPropagation(),
						disabled: !serviceRunning || draft.trim().length === 0,
						title: "Publish to /inbox — simulates a Slack message arriving, doesn't touch the real platform",
						className: "nodrag nopan rounded bg-sky-700 px-3 py-1 text-[10px] font-medium text-white hover:bg-sky-600 disabled:cursor-not-allowed disabled:opacity-40",
						children: "Simulate inbound"
					}), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: (e) => {
							e.stopPropagation();
							postAsBot();
						},
						onPointerDown: (e) => e.stopPropagation(),
						disabled: !serviceRunning || draft.trim().length === 0,
						title: "Calls speak() — actually posts to Slack via this bot",
						className: "nodrag nopan rounded bg-purple-600 px-3 py-1 text-[10px] font-medium text-white hover:bg-purple-500 disabled:cursor-not-allowed disabled:opacity-40",
						children: "Post as bot"
					})]
				})]
			})
		]
	});
}
//#endregion
export { SlackFullView as default };
