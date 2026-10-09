// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/google_chat/1.0.0/ui/View.tsx
var MAX_TURNS = 200;
function GoogleChatFullView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const spokenTopic = `/google_chat/${proxyId}/spoken`;
	const stateTopic = `/google_chat/${proxyId}/state`;
	const controlTopic = `/google_chat/${proxyId}/control`;
	const [log, setLog] = useState([]);
	const [state, setState] = useState(null);
	const [draft, setDraft] = useState("");
	const scrollRef = useRef(null);
	const keyCounterRef = useRef(0);
	useEffect(() => {
		if (!proxyId) return;
		const offSpoken = wsClient.subscribe(spokenTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload ?? {};
			if (typeof p.text !== "string") return;
			keyCounterRef.current += 1;
			const key = `${p.ts ?? Date.now() / 1e3}-${keyCounterRef.current}`;
			setLog((prev) => {
				const next = [...prev, {
					text: p.text,
					ts: p.ts ?? Date.now() / 1e3,
					key
				}];
				return next.length > MAX_TURNS ? next.slice(next.length - MAX_TURNS) : next;
			});
		});
		const offState = wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			setState(f.payload);
		});
		return () => {
			offSpoken();
			offState();
		};
	}, [
		proxyId,
		spokenTopic,
		stateTopic,
		wsClient
	]);
	useLayoutEffect(() => {
		const el = scrollRef.current;
		if (!el) return;
		el.scrollTop = el.scrollHeight;
	}, [log.length]);
	const serviceRunning = proxy.status === "running" || proxy.status === "starting";
	const post = useCallback((e) => {
		e?.preventDefault();
		const text = draft.trim();
		if (!text || !serviceRunning) return;
		wsClient.publish(controlTopic, {
			action: "speak",
			text
		});
		setDraft("");
	}, [
		draft,
		controlTopic,
		serviceRunning,
		wsClient
	]);
	return /* @__PURE__ */ jsxs("div", {
		className: "flex h-full min-h-[260px] min-w-[300px] flex-col gap-2 p-3 text-xs",
		onPointerDown: (e) => e.stopPropagation(),
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-baseline justify-between text-[10px] text-slate-500",
				children: [/* @__PURE__ */ jsxs("span", { children: ["google_chat-", proxyId] }), /* @__PURE__ */ jsx("span", {
					className: "flex items-baseline gap-2",
					children: state ? /* @__PURE__ */ jsx("span", {
						className: state.connected ? "text-emerald-400" : "text-rose-400",
						children: state.connected ? "● webhook configured" : "○ not configured"
					}) : /* @__PURE__ */ jsx("span", { children: serviceRunning ? "loading…" : "service not running" })
				})]
			}),
			/* @__PURE__ */ jsx("div", {
				className: "rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-300",
				children: "send-only — this integration can't receive messages from the space (webhook limitation, not a bug)"
			}),
			/* @__PURE__ */ jsx("div", {
				className: "relative min-h-0 flex-1 rounded border border-slate-800 bg-slate-950/60",
				children: /* @__PURE__ */ jsx("div", {
					ref: scrollRef,
					className: "nodrag nopan absolute inset-0 flex flex-col gap-1 overflow-y-auto p-2",
					onWheel: (e) => e.stopPropagation(),
					children: log.length === 0 ? /* @__PURE__ */ jsx("div", {
						className: "text-[11px] text-slate-600",
						children: serviceRunning ? "nothing posted yet — type below" : "start the google_chat service to connect"
					}) : log.map((t) => /* @__PURE__ */ jsx("div", {
						className: "max-w-[90%] whitespace-pre-wrap break-words rounded bg-teal-950/60 px-2 py-1 text-[11px] text-teal-100",
						children: t.text
					}, t.key))
				})
			}),
			/* @__PURE__ */ jsxs("form", {
				onSubmit: post,
				className: "flex items-end gap-2",
				children: [/* @__PURE__ */ jsx("textarea", {
					value: draft,
					onChange: (e) => setDraft(e.target.value),
					onClick: (e) => e.stopPropagation(),
					disabled: !serviceRunning,
					rows: 2,
					placeholder: serviceRunning ? "type here, Enter to post to the space" : "service not running",
					onKeyDown: (e) => {
						if (e.key === "Enter" && !e.shiftKey) {
							e.preventDefault();
							post();
						}
					},
					className: "nodrag nopan min-h-[44px] flex-1 resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1 font-sans text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none disabled:opacity-50"
				}), /* @__PURE__ */ jsx("button", {
					type: "submit",
					onClick: (e) => e.stopPropagation(),
					onPointerDown: (e) => e.stopPropagation(),
					disabled: !serviceRunning || draft.trim().length === 0,
					className: "nodrag nopan rounded bg-teal-600 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-teal-500 disabled:cursor-not-allowed disabled:opacity-40",
					children: "Post"
				})]
			})
		]
	});
}
//#endregion
export { GoogleChatFullView as default };
