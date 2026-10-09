// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useEffect, useState } from "react";
import { Panel, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/master_template/1.0.0/ui/View.tsx
function MasterTemplateView({ proxy }) {
	const proxyId = proxy.id ?? proxy.name ?? "";
	const type = (proxy.service_meta_id ?? "master_template@1.0.0").split("@")[0];
	const ws = useWsClient();
	const [state, setState] = useState({});
	useEffect(() => {
		if (!proxyId) return;
		return ws.subscribe(`/${type}/${proxyId}/state`, (f) => {
			if (f.method === "message" && f.payload && typeof f.payload === "object") setState(f.payload);
		});
	}, [
		ws,
		type,
		proxyId
	]);
	const sendAction = (action, args = {}) => {
		ws.publish(`/${type}/${proxyId}/control`, {
			action,
			...args
		});
	};
	return /* @__PURE__ */ jsxs(Panel, {
		title: `${type} — replace me`,
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["proxy: ", proxyId]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["message: ", state.message ?? "—"]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "text-slate-400",
				children: ["ticks: ", state.ticks ?? "—"]
			}),
			/* @__PURE__ */ jsx("button", {
				type: "button",
				className: "nodrag nopan w-fit rounded bg-sky-600 px-2 py-1 text-white hover:bg-sky-500",
				onClick: () => sendAction("ping"),
				onPointerDown: (e) => e.stopPropagation(),
				children: "send an action"
			})
		]
	});
}
//#endregion
export { MasterTemplateView as default };
