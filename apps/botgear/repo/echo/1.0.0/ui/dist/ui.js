// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useEffect, useState } from "react";
import { Panel, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/echo/1.0.0/ui/View.tsx
function EchoView({ proxy }) {
	const proxyId = proxy?.id ?? proxy?.name ?? "echo";
	const topic = `/rlx/spike/${proxyId}`;
	const ws = useWsClient();
	const [clicks, setClicks] = useState(0);
	const [roundtrips, setRoundtrips] = useState(0);
	useEffect(() => {
		return ws.subscribe(topic, (f) => {
			if (f.method === "message") setRoundtrips((n) => n + 1);
		});
	}, [ws, topic]);
	const ping = () => {
		setClicks((c) => c + 1);
		ws.publish(topic, { ping: clicks + 1 });
	};
	return /* @__PURE__ */ jsxs(Panel, {
		title: "modular ui bundle (built via vite lib)",
		children: [
			/* @__PURE__ */ jsx("div", {
				className: "font-mono text-emerald-300",
				children: "✓ compiled View.tsx → ui.js (jsx-runtime + externals)"
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["proxy: ", proxyId]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["host React hooks: clicks = ", clicks]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["bus round-trips seen: ", roundtrips]
			}),
			/* @__PURE__ */ jsx("button", {
				className: "nodrag nopan w-fit rounded bg-sky-600 px-2 py-1 text-white hover:bg-sky-500",
				onClick: ping,
				onPointerDown: (e) => e.stopPropagation(),
				children: "ping bus (publish → subscribe round-trip)"
			})
		]
	});
}
//#endregion
export { EchoView as default };
